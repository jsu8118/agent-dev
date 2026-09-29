"""Exercise 9 starter - a dead-letter queue for the recall swarm's work queue.

The WorkQueue in labs/_day4.py marks a task 'dead' after its last attempt (or at once for a permanent failure) and
keeps only its last error. Build a dead-letter queue an on-call engineer can work with:

  1. a `dead_letters` table - task_id, kind, payload, attempts, error_class, first_error, last_error, errors (the
     JSON history of every failed attempt), poison (the same error every time), dead_at;
  2. `fail_classified(task_id, owner, error, error_class)` - 'transient' errors are retried until max_attempts,
     'permanent' ones are dead-lettered at once; a lease that expires on the last attempt dead-letters too (`reap`);
  3. `requeue(task_id, by, note)` - a human puts a dead letter back: attempts reset, history kept, an audit entry;
  4. `on_dead(callback)` - called with the dead-letter row whenever a task is dead-lettered (page someone).

The harness below needs no model: a deterministic worker plans units straight against the desk, with a flaky
scheduling service, a poison task and a worker that dies. Run it, implement the TODOs, run it again.

Run: python advanced/day4_orchestration_at_scale/exercises/ex09_dead_letter_queue.py
Solution: advanced/day4_orchestration_at_scale/solutions/ex09_dead_letter_queue.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import Crash  # noqa: E402
from labkit import header, step  # noqa: E402

import _day4 as d4  # noqa: E402

LEASE_S = 0.05
TASKS = [  # task id, payload, what the harness does to it
    ("unit:KP250-2608-0002", {"serials": ["KP250-2608-0002"]}, "healthy"),
    ("unit:KP250-2608-0099", {"serials": ["KP250-2608-0099"]}, "poison: the serial does not exist"),
    ("unit:KC2-2608-0001", {"serials": ["KC2-2608-0001"]}, "flaky: the scheduling service times out twice"),
    ("unit:KP250-2608-0004", {"serials": ["KP250-2608-0004"]}, "crash: the worker dies on its first attempt"),
    ("unit:KP100-2608-0002", {"serials": ["KP100-2608-0002"]}, "flaky: the scheduling service times out on every call"),
]


class DeadLetterQueue(d4.WorkQueue):
    """TODO: a dead-letter queue on top of the lab queue (see the module docstring)."""

    def fail_classified(self, task_id: str, owner: str, error: str, *, error_class: str) -> str:
        raise NotImplementedError("fail_classified(): transient -> retry until max_attempts; permanent -> dead letter now")

    def dead_letter_rows(self) -> list[dict]:
        raise NotImplementedError("dead_letter_rows(): the dead_letters table, with the error history")

    def requeue(self, task_id: str, *, by: str, note: str) -> bool:
        raise NotImplementedError("requeue(): put a dead letter back, keep its history, record who and why")

    def on_dead(self, callback) -> None:
        raise NotImplementedError("on_dead(): register a callback for every new dead letter")


def classify(exc: Exception) -> str:
    """Timeouts and outages are worth retrying; an identifier the desk does not know is not."""
    return "permanent" if "not found" in str(exc) else "transient"


def make_worker(desk: d4.RecallDesk):
    """A deterministic stand-in for a unit worker: the same tool sequence, no model."""
    crashed: set[str] = set()

    def process(task: d4.Task) -> dict:
        serial = task.payload["serials"][0]
        if task.task_id == "unit:KP250-2608-0004" and task.task_id not in crashed:
            crashed.add(task.task_id)
            raise Crash("worker process died")
        u = desk.run("get_unit", {"serial": serial})
        slots = desk.run("find_engineer_slots", {"region": u["region"], "skill": u["skill_required"], "not_after": u["remedy_by"]})
        parts = desk.run("check_parts", {"kit_sku": u["kit_sku"], "region": u["region"]})
        warehouse = next(w for w in [parts["preferred_warehouse"]] + parts["alternatives"] if parts["stock"].get(w, 0) > 0)
        reservation = desk.run("reserve_kit", {"kit_sku": u["kit_sku"], "warehouse": warehouse, "serial": serial})
        booking = desk.run("book_slot", {"slot_id": slots["slots"][0]["slot_id"], "serial": serial})
        return {"serial": serial, "status": "scheduled", "reservation_id": reservation["reservation_id"],
                "slot_id": booking["slot_id"]}

    return process


def make_desk() -> d4.RecallDesk:
    calls = {"KC2-2608-0001": 0}

    def scheduling(tool_input: dict) -> None:
        if tool_input.get("skill") == "controller_firmware":          # the KC-2 unit: two timeouts, then fine
            calls["KC2-2608-0001"] += 1
            if calls["KC2-2608-0001"] <= 2:
                raise d4.ToolFailure("scheduling service timeout")
        if tool_input.get("not_after") == "2026-09-23" and tool_input.get("region") == "US-EAST" \
                and tool_input.get("skill") == "seal_replacement":    # the safety seal unit: an outage that does not end
            raise d4.ToolFailure("scheduling service timeout")

    return d4.RecallDesk(faults={"find_engineer_slots": scheduling})


def drive(queue: d4.WorkQueue, desk: d4.RecallDesk, *, fail, log=print) -> None:
    """Claim, process, and on failure call `fail(task, error, error_class)`; a crash is left to the lease and reap()."""
    process = make_worker(desk)
    for task_id, payload, _ in TASKS:
        queue.enqueue(task_id, "unit_plan", payload, max_attempts=3)
    while True:
        task = queue.claim("worker-1", lease_s=LEASE_S)
        if task is None:
            time.sleep(LEASE_S * 1.5)
            if not queue.reap():
                return
            continue
        try:
            result = process(task)
        except Crash as exc:
            log(f"    {task.task_id} attempt {task.attempts}: {exc} - the lease will expire")
            continue
        except d4.ToolFailure as exc:
            error_class = classify(exc)
            status = fail(task, str(exc), error_class)
            log(f"    {task.task_id} attempt {task.attempts}: {exc} ({error_class}) -> {status}")
            continue
        queue.complete(task.task_id, "worker-1", result)
        log(f"    {task.task_id} attempt {task.attempts}: done")


def main() -> None:
    header("Exercise 9 - a dead-letter queue (starter)")
    step(1, "The lab queue: dead letters with only the last error")
    queue = d4.WorkQueue(d4.fresh_db("ex09_starter.db"))
    drive(queue, make_desk(), fail=lambda t, e, c: queue.fail(t.task_id, "worker-1", e, retry=c == "transient"))
    for t in queue.dead_letters():
        print(f"  dead: {t.task_id} after {t.attempts} attempt(s) - last error only: {t.error}")

    step(2, "Your DeadLetterQueue")
    dlq = DeadLetterQueue(d4.fresh_db("ex09_dlq.db"))
    todo = []
    for name, call in (("on_dead", lambda: dlq.on_dead(lambda row: None)),
                       ("fail_classified", lambda: dlq.fail_classified("x", "worker-1", "e", error_class="transient")),
                       ("dead_letter_rows", dlq.dead_letter_rows),
                       ("requeue", lambda: dlq.requeue("x", by="ops", note="n"))):
        try:
            call()
        except NotImplementedError as exc:
            todo.append(str(exc))
        except Exception:
            pass
    for item in todo:
        print(f"  TODO: {item}")
    if not todo:
        print("  all four methods exist - compare your output with the solution")


if __name__ == "__main__":
    main()
