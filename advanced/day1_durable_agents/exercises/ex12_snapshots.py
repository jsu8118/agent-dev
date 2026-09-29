"""Exercise 12 starter - snapshots: rebuild a long run from its latest snapshot plus the tail of the log.

rebuild() folds a run's whole event log into the messages array. For a 5-turn support run that is nothing; for
a 41-turn inventory audit it is ~120 events, and for a coordinator that runs for weeks (Day 4, Day 7) it is
thousands. Add snapshots WITHOUT editing advanced/lib/durable.py:

  * SnapshotStore(RunStore): a `snapshots` table in the same database (run_id, seq, the folded state), with
    save_snapshot(), latest_snapshot() and events_after(run_id, seq) - and a count of the rows it read;
  * SnapshottingRunner(DurableRunner): save a snapshot every 10 turns (hint: the runner calls
    self._maybe_crash("after_model", turn) right after it logs each response), and rebuild() from the latest
    snapshot plus the events after it.

The result must be byte-identical to the full rebuild - same messages, same results, same counters - and a run
resumed from a snapshot must still send the crashed worker's exact bytes. This starter runs the audit, crashes
the worker after turn 38's tool result, resumes it, and measures the full rebuild.
Run: python advanced/day1_durable_agents/exercises/ex12_snapshots.py
Solution: advanced/day1_durable_agents/solutions/ex12_snapshots.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import Crash, DurableRunner, RunStore  # noqa: E402
from labkit import get_client, header  # noqa: E402
from labkit.data import memory_db  # noqa: E402

import _day1 as d1  # noqa: E402

CRASH = ("after_tool", 38)


class SnapshotStore(RunStore):
    """TODO: a snapshots table, save_snapshot(), latest_snapshot(), events_after(), and rows_read."""

    rows_read = 0


class SnapshottingRunner(DurableRunner):
    """TODO: snapshot every 10 turns; rebuild from the latest snapshot plus the events after it."""


# ---------------------------------------------------------------------------- the harness (keep this)
def auditor(store, client, db, name: str, runner_cls, **kw):
    return d1.support_runner(store, client, execute=d1.stock_executor(db), worker=name, system=d1.STOCK_AUDIT_SYSTEM,
                             tools=[d1.COUNT_STOCK], runner_cls=runner_cls, max_turns=60, **kw)


def crash_and_resume(client, store_cls, runner_cls, store_name: str) -> dict:
    """Run the 40-location audit, crash after turn 38's tool, resume on another worker; measure the rebuild."""
    d1.fresh_store(store_name)                                    # wipe the file, then open it with store_cls
    store, db = store_cls(d1.store_path(store_name)), memory_db()
    run = store.create("audit", input=d1.stock_audit_input(db, 40), run_id="stock-audit-40")
    seen_a, seen_b = d1.RecordingClient(client), d1.RecordingClient(client)
    try:
        auditor(store, seen_a, db, "worker-a", runner_cls, crash_at=CRASH).run(run.id)
    except Crash:
        pass
    if len(store.events(run.id, types=("model.response",))) < CRASH[1]:
        raise SystemExit("the audit finished in fewer turns than the crash point (live: the model batched its tool "
                         "calls instead of one per turn); rerun, or lower CRASH")
    last_before = seen_a.requests[-1]
    resumer = auditor(store, seen_b, db, "worker-b", runner_cls)
    events = len(store.events(run.id))
    store.rows_read = 0
    state = resumer.rebuild(run.id)
    rows = store.rows_read or events                              # the starter's store counts nothing: all events
    reference = DurableRunner.rebuild(resumer, run.id)            # the full fold of the same log, for comparison
    outcome = resumer.run(run.id)
    first_after = seen_b.requests[0]
    n = len(last_before["messages"])
    return {"store": store, "run_id": run.id, "state": state, "reference": reference, "events at the crash": events,
            "rows read by rebuild()": rows, "resumed": d1.outcome_line(outcome),
            "resumed request extends the crashed one": first_after["messages"][:n] == last_before["messages"],
            "reply": outcome.reply}


def main() -> None:
    client = get_client()
    header("Exercise 12 - snapshots and a tail rebuild (starter)")
    result = crash_and_resume(client, SnapshotStore, SnapshottingRunner, "ex12_starter")
    for key in ("events at the crash", "rows read by rebuild()", "resumed", "resumed request extends the crashed one"):
        print(f"  {key:<42} {result[key]}")
    print(f"  reply: {d1.short(result['reply'], 110)}")
    print("\nTODO: rebuild() read every event in the log. Implement SnapshotStore and SnapshottingRunner so that the")
    print("      rebuild reads one snapshot row plus the ~26 events after it, and prove it is byte-identical to the")
    print("      full rebuild (DurableRunner.rebuild) - messages, results, turns and replayed count.")


if __name__ == "__main__":
    main()
