"""Lab 02 - Work queues and shared state: two worker threads, one campaign record, no double work.

Objective
    Run the recall's unit tasks from two worker threads that claim from the same SQLite queue and update the same
    campaign record. Watch three kinds of shared state protected by three mechanisms - a versioned record
    (compare-and-set), a queue row (an atomic claim), a calendar slot (the system of record says no) - then prove
    that at-least-once delivery cannot make a unit run twice, and read the campaign totals from an append-only
    results table instead of trusting counters.

Concepts
    optimistic concurrency / versioning, compare-and-set, lost updates, atomic and optimistic claims, the system
    of record as arbiter, leases and redelivery, idempotent claims (queue + durable run id), append-only results,
    merge policies, invariants that hold under any interleaving

Run
    python advanced/day4_orchestration_at_scale/labs/02_work_queues_and_shared_state.py

What to observe
    * Step 1: worker B's write with a stale version is rejected (0 rows) and succeeds after re-reading - units_done
      ends at 2, not 1 (the lost update it would have been); B's claim of a task A already took fails; B's booking
      of the slot A already booked is refused by the desk and B takes the next one.
    * Step 2: under real threads the first two record writes are made to collide (a barrier), so there is always at
      least one conflict; per-thread counts VARY from run to run - the invariants printed below them do not.
    * Step 2: 11 tasks each claimed exactly once, the record at version 12 = 1 + 11 writes, no slot booked twice -
      and, varying from run to run, the booking attempts the desk refused because the other thread was first.
    * Step 3: a redelivered task costs zero model calls: the queue de-duplicates producers, and the durable run id
      de-duplicates consumers even when the queue is bypassed.
    * Step 4: totals recomputed from the results table equal the campaign record - the record is a cache, the
      results table is the truth.
"""
# test: expect=claimed exactly once: True
# test: expect=record version 12 = 1 + 11 writes

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import LEDGER, get_client, header, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402

RECORD_KEY = "campaign:RC-2026-03"


def apply_plan(plans: list[dict], cost_usd: float):
    """The pure function a worker applies to the campaign record: pure, so that a retry after a conflict is safe."""
    def fn(data: dict) -> dict:
        data["units_done"] = data.get("units_done", 0) + len(plans)
        kits = dict(data.get("kits", {}))
        for p in plans:
            if p.get("warehouse"):
                kits[p["warehouse"]] = kits.get(p["warehouse"], 0) + 1
        data["kits"] = kits
        data["model_cost_usd"] = round(data.get("model_cost_usd", 0.0) + cost_usd, 6)
        statuses = dict(data.get("statuses", {}))
        for p in plans:
            statuses[p["status"]] = statuses.get(p["status"], 0) + 1
        data["statuses"] = statuses
        return data
    return fn


def scripted_interleavings(desk: d4.RecallDesk) -> None:
    demo = d4.WorkQueue(d4.fresh_db("lab02_demo.db"))
    record = d4.SharedRecord(demo, "demo:record", {"units_done": 0})
    va, da = record.read()
    vb, db_ = record.read()
    print(f"  Record: A reads version {va} {da}; B reads version {vb} {db_}")
    da["units_done"] += 1
    ok_a = record.write(da, expected_version=va)
    print(f"  A writes units_done={da['units_done']} expecting version {va} -> {'ok' if ok_a else 'conflict'} "
          f"(record now version {record.read()[0]})")
    db_["units_done"] += 1
    ok_b = record.write(db_, expected_version=vb)
    print(f"  B writes units_done={db_['units_done']} expecting version {vb} -> {'ok' if ok_b else 'CONFLICT: 0 rows updated'}")
    version, _ = record.update(lambda d: {**d, "units_done": d["units_done"] + 1})
    print(f"  B re-reads and re-applies its change (a pure function) -> version {version}, units_done="
          f"{record.read()[1]['units_done']} (a blind overwrite would have left 1: the lost update)")

    demo.enqueue("demo:claim", "unit_plan", {"serials": ["KP250-2608-0002"]})
    t = demo.get("demo:claim")
    print(f"\n  Queue row: A and B both read task {t.task_id} as queued at version {t.version}.")
    print(f"  A claim_specific(expected_version={t.version}) -> {demo.claim_specific(t.task_id, 'A', expected_version=t.version)}")
    print(f"  B claim_specific(expected_version={t.version}) -> {demo.claim_specific(t.task_id, 'B', expected_version=t.version)}"
          "  (the row changed under B; B moves on to another task)")

    search = desk.run("find_engineer_slots", {"region": "US-EAST", "skill": "seal_replacement", "not_after": "2026-09-23"})
    first, second = search["slots"][0]["slot_id"], search["slots"][1]["slot_id"]
    print(f"\n  Calendar slot: A and B both searched and both saw {first} free.")
    print(f"  A books {first} for KP250-2608-0006 -> {desk.run('book_slot', {'slot_id': first, 'serial': 'KP250-2608-0006'})['booking_id']}")
    try:
        desk.run("book_slot", {"slot_id": first, "serial": "KP250-2608-0007"})
    except d4.ToolFailure as exc:
        print(f"  B books {first} for KP250-2608-0007 -> refused: {exc}")
    print(f"  B takes the next slot from its search, {second} -> "
          f"{desk.run('book_slot', {'slot_id': second, 'serial': 'KP250-2608-0007'})['booking_id']}")
    print("  Three kinds of shared state, three guards: a version on the record, an atomic claim on the queue row, a")
    print("  uniqueness check in the system of record. None of them is a prompt.")


def worker_thread(name: str, db: Path, desk: d4.RecallDesk, stats: dict, lock: threading.Lock,
                  barrier: threading.Barrier) -> None:
    client = get_client()
    store, queue = RunStore(db), d4.WorkQueue(db)
    record = d4.SharedRecord(queue, RECORD_KEY)
    first = {"pending": True}

    def collide_once() -> None:               # between read and write: make the two threads' first writes collide
        if first["pending"]:
            first["pending"] = False
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass

    while True:
        task = queue.claim(name, lease_s=30)
        if task is None:
            return
        meter = d4.Meter()
        result = d4.run_unit_worker(client, store, desk, task.payload["serials"], run_id=f"unit:{task.task_id}", worker=name)
        meter.add_run("worker", store, result.run_id)
        _, conflicts = record.update(apply_plan(result.plans, meter.total().cost), between=collide_once)
        queue.complete(task.task_id, name, {"plans": result.plans, "cost_usd": meter.total().cost})
        with lock:
            stats["claims"][name] += 1
            stats["conflicts"] += conflicts


def main() -> None:
    header("Lab 02 - Work queues and shared state")
    d4.mock_note("the workers are rule-based stand-ins reading real tool results; the queue, the versioned record, the "
                 "threads and the desk are real. Thread timing is real too, so per-thread counts vary between runs.")
    client = get_client()
    db = d4.fresh_db("lab02.db")
    store, queue = RunStore(db), d4.WorkQueue(db)
    record = d4.SharedRecord(queue, RECORD_KEY, {"units_done": 0, "kits": {}, "model_cost_usd": 0.0, "statuses": {}})

    step(1, "Scripted interleavings: a stale write, a lost claim race, a double booking refused")
    scripted_interleavings(d4.RecallDesk())

    step(2, "Two worker threads drain 11 unit tasks and update the campaign record under compare-and-set")
    desk = d4.RecallDesk()
    for u in d4.units():
        queue.enqueue(f"unit:{u['serial_number']}", "unit_plan", {"serials": [u["serial_number"]]},
                      priority=d4.PRIORITY[u["risk_class"]])
    version_before = record.read()[0]
    stats = {"claims": Counter(), "conflicts": 0}
    lock, barrier = threading.Lock(), threading.Barrier(2)
    threads = [threading.Thread(target=worker_thread, args=(name, db, desk, stats, lock, barrier), name=name)
               for name in ("worker-A", "worker-B")]
    started = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.perf_counter() - started
    tasks = queue.tasks()
    version, data = record.read()
    print(f"  finished in {elapsed:.1f}s wall-clock (varies)")
    print(f"  per-thread claims: {dict(sorted(stats['claims'].items()))}; record conflicts retried: {stats['conflicts']} "
          "(both vary; the first write of each thread was made to collide, so conflicts >= 1)")
    print("\n  Invariants (hold under ANY interleaving):")
    print(f"  * queue: {queue.stats()}")
    once = all(t.attempts == 1 and t.status == "done" for t in tasks)
    print(f"  * {len(tasks)} tasks claimed exactly once: {once}")
    print(f"  * record version {version} = {version_before} + {len(tasks)} writes (one successful compare-and-set per task; "
          "retries do not add versions)")
    print(f"  * record: units_done={data['units_done']}, kits={dict(sorted(data['kits'].items()))} - one reservation per unit")
    slots = {b["slot_id"] for b in desk.bookings.values()}
    print(f"  * no slot booked twice: {len(slots) == len(desk.bookings)}")
    refused = sum(1 for t in tasks for e in store.events(f"unit:{t.task_id}", types=("tool.result",))
                  if e["name"] == "book_slot" and "no longer free" in e["content"])
    print(f"  Varies with the interleaving: {len(desk.bookings)} bookings, the desk refused {refused} booking attempt(s) "
          f"because the other thread got there first, statuses {dict(sorted(data['statuses'].items()))}. A unit whose "
          "three attempts were all refused is escalated as pending_schedule - never double-booked.")

    step(3, "At-least-once delivery: a redelivered task must cost nothing")
    again = queue.enqueue("unit:KP250-2608-0002", "unit_plan", {"serials": ["KP250-2608-0002"]})
    print(f"  producer redelivers unit:KP250-2608-0002 -> enqueue returns {again} (primary key: the task already exists)")
    calls_before = LEDGER.total_calls
    replay = d4.run_unit_worker(client, store, desk, ["KP250-2608-0002"], run_id="unit:unit:KP250-2608-0002", worker="worker-C")
    print(f"  consumer bypasses the queue and re-runs the unit's run id -> status={replay.outcome.status}, "
          f"model calls made: {LEDGER.total_calls - calls_before} (the durable run is complete; its reply is returned)")
    demo = d4.WorkQueue(d4.fresh_db("lab02_lease.db"))
    demo.enqueue("demo:lease", "unit_plan", {"serials": ["KP250-2608-0003"]})
    first = demo.claim("worker-A", lease_s=0.05)
    second = demo.claim("worker-B", lease_s=0.05)
    print(f"  redelivery while a lease is live: A holds {first.task_id}; B's claim returns {second} - nothing else is queued")
    time.sleep(0.08)
    reaped = demo.reap()
    third = demo.claim("worker-B", lease_s=30)
    print(f"  after A's lease expired: reap() re-queued {reaped}; B claims it as attempt {third.attempts}")
    late = demo.complete("demo:lease", "worker-A", {"plans": []})
    print(f"  A wakes up and tries to complete it -> {late} (A no longer owns it; only B's result will count)")

    step(4, "Append-only results and the merge policy")
    rows = queue.results(latest_only=False)
    eu = next(r for r in rows if r["task_id"] == "unit:KP100-2608-0001")
    print(f"  results table: {len(rows)} rows, one per completed attempt; e.g. {eu['task_id']} attempt {eu['attempt']} "
          f"-> {eu['payload']['plans'][0]['status']} at {eu['payload']['plans'][0]['warehouse']}")
    recomputed: dict = {"units_done": 0, "kits": {}, "statuses": {}, "model_cost_usd": 0.0}
    for r in queue.results():
        recomputed = apply_plan(r["payload"]["plans"], r["payload"]["cost_usd"])(recomputed)
    agree = all(recomputed[k] == data[k] for k in ("units_done", "kits", "statuses"))
    print(f"  recomputed from results: units_done={recomputed['units_done']} kits={dict(sorted(recomputed['kits'].items()))} "
          f"-> agrees with the record: {agree}")
    print(wrap("Merge policy: plans - the latest completed attempt of a task wins; counters - never trusted on their own, "
               "always recomputable from the results; side effects - idempotent per serial on the desk, so a retried "
               "attempt cannot double-book.", "  "))


if __name__ == "__main__":
    main()
