"""Lab 02 - Work queues and shared state: two worker threads, one campaign record, no double work.

Objective
    Run the recall's unit tasks from two worker threads that claim from the same SQLite queue and update the same
    campaign record. See optimistic concurrency (compare-and-set on a version) turn a lost update into a retried
    one, see a claim race resolved by the database rather than by luck, prove that at-least-once delivery cannot
    make a unit run twice, and read the campaign totals from an append-only results table instead of trusting
    counters.

Concepts
    optimistic concurrency / versioning, compare-and-set, lost updates, atomic claims, leases and redelivery,
    idempotent claims (queue + durable run id), append-only results, aggregation and merge policies, invariants
    that hold under any interleaving

Run
    python advanced/day4_orchestration_at_scale/labs/02_work_queues_and_shared_state.py

What to observe
    * Step 1: worker B's write with a stale version is rejected (0 rows) and succeeds after re-reading - the record
      ends at units_done=2, not 1 (the lost update it would have been).
    * Step 2: after both threads finish, every invariant holds: 11 tasks done, each claimed exactly once, the record
      at version 12 = 1 + 11 writes, 11 reservations and 11 distinct booked slots on the desk.
    * Step 2: per-thread claim counts and the number of version conflicts VARY from run to run - the invariants do not.
    * Step 3: a redelivered task costs zero model calls: the queue de-duplicates producers, and the durable run id
      de-duplicates consumers even when the queue is bypassed.
    * Step 4: totals recomputed from the results table equal the campaign record - the record is a cache, the
      results table is the truth.
"""
# test: expect=claimed exactly once
# test: expect=version 12

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import LEDGER, get_client, header, step  # noqa: E402

import _day4 as d4  # noqa: E402

PRIORITY = {"safety": 2, "production": 1, "standard": 0}
RECORD_KEY = "campaign:RC-2026-03"


def apply_plan(plans: list[dict], cost_usd: float):
    """The pure function a worker applies to the campaign record: pure so that a retry after a conflict is safe."""
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


def worker_thread(name: str, db: Path, desk: d4.RecallDesk, stats: dict, lock: threading.Lock) -> None:
    client = get_client()
    store, queue = RunStore(db), d4.WorkQueue(db)
    record = d4.SharedRecord(queue, RECORD_KEY)
    while True:
        task = queue.claim(name, lease_s=30)
        if task is None:
            return
        meter = d4.Meter()
        result = d4.run_unit_worker(client, store, desk, task.payload["serials"], run_id=f"unit:{task.task_id}", worker=name)
        meter.add_run("worker", store, result.run_id)
        _, conflicts = record.update(apply_plan(result.plans, meter.total().cost))
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
    store, queue, desk = RunStore(db), d4.WorkQueue(db), d4.RecallDesk()
    record = d4.SharedRecord(queue, RECORD_KEY, {"units_done": 0, "kits": {}, "model_cost_usd": 0.0, "statuses": {}})

    step(1, "Optimistic concurrency, scripted: two workers, one record, one stale write")
    va, da = record.read()
    vb, db_ = record.read()
    print(f"  A reads version {va}: {da}\n  B reads version {vb}: {db_}")
    da["units_done"] += 1
    print(f"  A writes units_done={da['units_done']} expecting version {va} -> {'ok' if record.write(da, expected_version=va) else 'conflict'}"
          f" (record now version {record.read()[0]})")
    db_["units_done"] += 1
    ok = record.write(db_, expected_version=vb)
    print(f"  B writes units_done={db_['units_done']} expecting version {vb} -> {'ok' if ok else 'CONFLICT: 0 rows updated'}")
    print("  B re-reads and re-applies its own change (the update function is pure, so replaying it is safe):")
    version, conflicts = record.update(lambda d: {**d, "units_done": d["units_done"] + 1})
    print(f"  -> version {version}, units_done={record.read()[1]['units_done']} (a plain overwrite would have left 1: the lost update)")
    record.update(lambda d: {**d, "units_done": 0})                    # reset for the real run
    queue.enqueue("demo:claim-race", "unit_plan", {"serials": ["KP250-2608-0002"]})
    t = queue.get("demo:claim-race")
    print(f"\n  Claim race on task {t.task_id} (version {t.version}): A and B both read it as queued.")
    print(f"  A claim_specific(expected_version={t.version}) -> {queue.claim_specific(t.task_id, 'A', expected_version=t.version)}")
    print(f"  B claim_specific(expected_version={t.version}) -> {queue.claim_specific(t.task_id, 'B', expected_version=t.version)}  "
          "(the row changed under B; B moves on to the next task)")
    queue.complete("demo:claim-race", "A", {"plans": [], "cost_usd": 0.0})
    queue._conn().execute("DELETE FROM tasks WHERE task_id = 'demo:claim-race'")
    queue._conn().execute("DELETE FROM results WHERE task_id = 'demo:claim-race'")

    step(2, "Two worker threads drain 11 unit tasks and update the campaign record under compare-and-set")
    for u in d4.units():
        queue.enqueue(f"unit:{u['serial_number']}", "unit_plan", {"serials": [u["serial_number"]]},
                      priority=PRIORITY[u["risk_class"]])
    version_before = record.read()[0]
    stats = {"claims": Counter(), "conflicts": 0}
    lock = threading.Lock()
    threads = [threading.Thread(target=worker_thread, args=(name, db, desk, stats, lock), name=name)
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
    print(f"  per-thread claims: {dict(stats['claims'])}; version conflicts on the record: {stats['conflicts']} (both vary run to run)")
    print("\n  Invariants (hold under ANY interleaving):")
    print(f"  * queue statistics: {queue.stats()}")
    once = all(t.attempts == 1 and t.status == "done" for t in tasks)
    print(f"  * {len(tasks)} tasks claimed exactly once: {once} (attempts={sorted({t.attempts for t in tasks})})")
    print(f"  * record version {version} = {version_before} + {len(tasks)} writes (one compare-and-set per task, retries included)")
    print(f"  * record: units_done={data['units_done']} statuses={dict(sorted(data['statuses'].items()))} "
          f"kits={dict(sorted(data['kits'].items()))}")
    slots = {b["slot_id"] for b in desk.bookings.values()}
    print(f"  * desk: {len(desk.reservations)} kit reservations, {len(desk.bookings)} bookings on {len(slots)} distinct slots "
          f"(no slot booked twice); stock left {desk.stock}")

    step(3, "At-least-once delivery: a redelivered task must cost nothing")
    print(f"  producer redelivers unit:KP250-2608-0002 -> enqueue returns {queue.enqueue('unit:KP250-2608-0002', 'unit_plan', {'serials': ['KP250-2608-0002']})}"
          " (primary key: the task already exists)")
    calls_before = LEDGER.total_calls
    replay = d4.run_unit_worker(client, store, desk, ["KP250-2608-0002"], run_id="unit:unit:KP250-2608-0002", worker="worker-C")
    print(f"  consumer bypasses the queue and re-runs the unit's run id -> status={replay.outcome.status}, "
          f"model calls made: {LEDGER.total_calls - calls_before} (the durable run is already completed; its reply is returned)")
    queue.enqueue("demo:lease", "unit_plan", {"serials": ["KP250-2608-0003"]})
    first = queue.claim("worker-A", lease_s=0.05)
    second = queue.claim("worker-B", lease_s=0.05)
    print(f"  redelivery while a lease is live: A holds {first.task_id}; B's claim returns {second} - nothing else is queued")
    time.sleep(0.08)
    reaped = queue.reap()
    third = queue.claim("worker-B", lease_s=30)
    print(f"  after A's lease expired: reap() re-queued {reaped}; B claims it as attempt {third.attempts}")
    queue.complete("demo:lease", "worker-B", {"plans": [], "cost_usd": 0.0})

    step(4, "Append-only results and the merge policy")
    rows = queue.results(latest_only=False)
    real = [r for r in rows if r["task_id"].startswith("unit:")]
    print(f"  results table: {len(rows)} rows, one per completed attempt; e.g. {real[0]['task_id']} attempt {real[0]['attempt']} "
          f"by {real[0]['owner']} -> {real[0]['payload']['plans'][0]['status']}")
    recomputed: dict = {"units_done": 0, "kits": {}, "statuses": {}, "model_cost_usd": 0.0}
    for r in queue.results():
        if r["task_id"].startswith("unit:"):
            recomputed = apply_plan(r["payload"]["plans"], r["payload"]["cost_usd"])(recomputed)
    agree = all(recomputed[k] == data[k] for k in ("units_done", "kits", "statuses"))
    print(f"  recomputed from results: units_done={recomputed['units_done']} kits={dict(sorted(recomputed['kits'].items()))} "
          f"-> agrees with the record: {agree}")
    print("  Merge policy: plans - the latest completed attempt of a task wins; counters - never incremented in place, always")
    print("  recomputed from results; side effects - idempotent per serial on the desk, so a retried attempt cannot double-book.")


if __name__ == "__main__":
    main()
