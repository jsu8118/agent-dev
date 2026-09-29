"""Lab 01 - Durable coordinator: a coordinator run that dispatches 11 units through a work queue, dies half-way, and resumes.

Objective
    Build the recall swarm's spine: a coordinator agent running on Day 1's `DurableRunner`, a SQLite work queue
    with claims and leases, and worker agents that are durable runs themselves. Kill the coordinator after five
    unit tasks and a worker in the middle of a unit, then resume both from their logs and the queue - nothing is
    repeated, nothing is lost, and the campaign summary is the same one a crash-free run would have produced.

Concepts
    durable coordinator, work queue (enqueue / claim / complete), leases and dead-worker reaping, at-least-once
    delivery with idempotent tasks, in-flight effects across a crash, two levels of durability (coordinator run,
    unit runs), replayed vs executed tool calls, priorities from risk class

Run
    python advanced/day4_orchestration_at_scale/labs/01_durable_coordinator.py [--crash-after 5]

What to observe
    * Step 2: the coordinator dies inside `dispatch_units`; the queue already holds 5 done tasks and the log holds
      a `tool.started` with no `tool.result` - an in-flight effect.
    * Step 2: a worker dies after its second tool call; its lease expires, the supervisor re-queues the task, and
      the retry resumes the worker's own run with 2 tool results replayed from its log.
    * Step 3: a new coordinator process resumes the same run id: 1 tool result replayed, `dispatch_units` re-executed
      against the queue (5 tasks skipped, the rest done), then `collect_results` and the summary.
    * Step 4: the coordinator's event log and the per-unit runs, with attempts and replayed calls.
"""
# test: expect=resumed
# test: expect=queue statistics

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import Crash, DurableRunner, RunStore, ToolContext  # noqa: E402
from labkit import MODEL, get_client, header, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402

RUN_ID = "coordinator:RC-2026-03"
PRIORITY = {"safety": 2, "production": 1, "standard": 0}
LEASE_S = 0.05                      # short on purpose so a dead worker's lease expires within the lab


class Dispatcher:
    """The coordinator's `dispatch_units` tool: enqueue batches, then drain the queue with in-process workers.

    Everything here is idempotent by construction: a batch already in the queue is not enqueued twice, a task
    already done is never claimed again, and a unit run resumed under the same run id replays its own log."""

    def __init__(self, queue: d4.WorkQueue, store: RunStore, client, desk: d4.RecallDesk, *, owner: str,
                 crash_after_tasks: int | None = None, worker_crashes: dict[str, int] | None = None) -> None:
        self.queue, self.store, self.client, self.desk, self.owner = queue, store, client, desk, owner
        self.crash_after_tasks = crash_after_tasks
        self.worker_crashes = dict(worker_crashes or {})
        self.completed_now = 0

    def dispatch(self, batches: list[dict]) -> dict:
        new = 0
        for b in batches:
            serials_ = list(b.get("serials") or [])
            priority = max(PRIORITY[d4.unit(s)["risk_class"]] for s in serials_)
            new += self.queue.enqueue("batch:" + "+".join(serials_), "unit_plan", {"serials": serials_, "brief": b.get("brief")},
                                      priority=priority)
        print(f"    dispatch_units: {new} new batch task(s) enqueued, {len(batches) - new} already known; queue={self.queue.stats()}")
        self.drain()
        return {"enqueued": new, **self.queue.stats()}

    def drain(self) -> None:
        while True:
            task = self.queue.claim(self.owner, lease_s=LEASE_S)
            if task is None:
                return
            serials_ = task.payload["serials"]
            worker_id = f"{self.owner}/w{task.attempts}"
            crash_calls = self.worker_crashes.pop(task.task_id, None)
            try:
                result = d4.run_unit_worker(self.client, self.store, self.desk, serials_, run_id=f"unit:{task.task_id}",
                                            worker=worker_id, brief=task.payload.get("brief"), crash_after_calls=crash_calls)
            except Crash as exc:
                print(f"    !! worker {worker_id} died on {task.task_id} (attempt {task.attempts}): {exc}")
                time.sleep(LEASE_S * 1.5)                           # the lease runs out ...
                reaped = self.queue.reap()                           # ... and the supervisor's sweep re-queues the task
                print(f"    supervisor: lease expired -> re-queued {reaped}; queue={self.queue.stats()}")
                continue
            self.queue.complete(task.task_id, self.owner, {"plans": result.plans, "turns": result.outcome.turns,
                                                           "replayed_tools": result.outcome.replayed_tools,
                                                           "executed_tools": result.outcome.executed_tools})
            self.completed_now += 1
            statuses = ", ".join(f"{p['serial']} {p['status']}" for p in result.plans)
            resumed = f" (resumed: {result.outcome.replayed_tools} tool results replayed)" if result.outcome.replayed_tools else ""
            print(f"    task {task.task_id} done by {worker_id}: {statuses}{resumed}")
            if self.crash_after_tasks is not None and self.completed_now >= self.crash_after_tasks:
                raise Crash(f"coordinator process died after {self.completed_now} unit tasks")


def make_coordinator(store: RunStore, client, dispatcher: Dispatcher, desk: d4.RecallDesk, *, worker: str) -> DurableRunner:
    def execute(name: str, tool_input: dict, ctx: ToolContext):
        if name == "list_affected_units":
            return desk.run(name, {})
        if name == "dispatch_units":
            with ctx.effect() as eff:
                if eff.done:
                    return eff.stored
                if eff.in_flight:
                    print("    dispatch_units: effect in flight from a previous worker - the queue is the system of record, "
                          "so re-running it only processes what is not done yet")
                return eff.commit(dispatcher.dispatch(tool_input.get("batches") or []))
        if name == "collect_results":
            plans = [p for r in dispatcher.queue.results() for p in r["payload"]["plans"]]
            return {"plans": plans, **dispatcher.queue.stats()}
        return {"error": f"unknown tool {name}"}

    return DurableRunner(store, client, model=MODEL, system=d4.coordinator_system(batch="customer", brief="short"),
                         tools=d4.COORDINATOR_TOOLS, execute=execute, max_turns=8, worker=worker,
                         create_kwargs={"cache_control": {"type": "ephemeral"}})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--crash-after", type=int, default=5, help="kill the coordinator after this many unit tasks")
    args = parser.parse_args()

    header("Lab 01 - Durable coordinator, work queue, crash and resume")
    d4.mock_note("the coordinator and the workers are rule-based stand-ins that read real tool results; the queue, leases, "
                 "crashes, replay and resumption are the real code (advanced/lib/durable.py + _day4.WorkQueue).")
    client = get_client()
    db = d4.fresh_db("lab01.db")
    store, queue, desk = RunStore(db), d4.WorkQueue(db), d4.RecallDesk()

    step(1, "The campaign: 11 affected units, 7 customers, 3 remedies - and the queue that will carry them")
    rows = [[u["serial_number"], u["customer_id"], u["customer"][:26], u["region"], u["risk_class"], u["remedy"]] for u in d4.units()]
    print(d4.table(rows, ["serial", "customer", "name", "region", "risk", "remedy"]))
    print("\n  The queue is one SQLite table. A claim is ONE atomic statement - no read-then-write race between workers:")
    print("    UPDATE tasks SET status='claimed', owner=?, lease_until=?, attempts=attempts+1, version=version+1")
    print("      WHERE task_id = (SELECT task_id FROM tasks WHERE status='queued' OR (status='claimed' AND lease_until < ?)")
    print("                       ORDER BY priority DESC, created_at, task_id LIMIT 1) RETURNING *")
    print("  Priority comes from the risk class (safety > production > standard); a claim expires with its lease.")

    step(2, f"First coordinator process: dies after {args.crash_after} unit tasks; one worker dies mid-unit")
    run = store.create("campaign", input={"message": "Run recall campaign RC-2026-03: plan the remedy for every affected unit."},
                       run_id=RUN_ID)
    dispatcher = Dispatcher(queue, store, client, desk, owner="coordinator-1", crash_after_tasks=args.crash_after,
                            worker_crashes={"batch:KP250-2608-0004": 2})
    try:
        make_coordinator(store, client, dispatcher, desk, worker="coordinator-1").run(run.id)
        print("  (no crash - raise --crash-after to see one)")
    except Crash as exc:
        print(f"\n  !! {exc}")
    events = store.events(run.id)
    print(f"  run status={store.get(run.id).status} lease_owner={store.get(run.id).lease_owner} "
          f"events={len(events)} last={events[-1]['type']} ({events[-1].get('name', '')})")
    print(f"  queue statistics after the crash: {queue.stats()}")

    step(3, "A second coordinator process resumes the same run id")
    dispatcher2 = Dispatcher(queue, store, client, desk, owner="coordinator-2")
    outcome = make_coordinator(store, client, dispatcher2, desk, worker="coordinator-2").run(run.id)
    print(f"\n  resumed run: status={outcome.status} turns={outcome.turns} replayed_tools={outcome.replayed_tools} "
          f"executed_tools={outcome.executed_tools}; unit tasks done in this process: {dispatcher2.completed_now}")
    print("\n  Campaign summary (the coordinator's final message):")
    print(wrap(outcome.reply, "    "))

    step(4, "What the logs say: the coordinator's events, and every unit run")
    kinds = [e["type"] + (f"({e['name']})" if e.get("name") else "") for e in store.events(run.id)]
    print("  coordinator log: " + " -> ".join(kinds))
    rows = []
    for task in queue.tasks():
        r = store.get(f"unit:{task.task_id}")
        payload = (task.result or {})
        rows.append([task.task_id.replace("batch:", ""), task.status, task.attempts, r.status, payload.get("turns", "-"),
                     payload.get("replayed_tools", "-"), payload.get("executed_tools", "-")])
    print(d4.table(rows, ["task", "queue status", "attempts", "run status", "turns", "replayed", "executed"]))
    print(f"\n  queue statistics: {queue.stats()}; results rows (one per completed attempt): {len(queue.results(latest_only=False))}")
    print("  Two levels of durability: the coordinator's run resumed without re-generating turns 1-2, and the worker that")
    print("  died resumed ITS run with its first tool results replayed - the queue only tells the supervisor whom to retry.")


if __name__ == "__main__":
    main()
