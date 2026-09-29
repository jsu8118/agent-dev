"""Solution to exercise 12 - snapshots: the latest snapshot plus the tail of the log, byte-identical.

A snapshot is the folded state of the log at one sequence number: the messages array, the tool results by
tool_use_id, the turn count and the replayed count - exactly what DurableRunner.rebuild() returns. Rebuilding
then means: load the latest snapshot, fold only the events after its seq, apply the same final step. Three rules
keep it correct:
1. Snapshot the FOLD's state, taken from the log itself at a known seq - never the worker's in-memory
   variables, which may be ahead of the log (a response received but not yet logged).
2. Store the state as exact text; the byte-identity check below is the test that catches a normalising column.
3. Snapshots are a cache of the log, never a replacement: the log stays complete, so replay, forks and audits
   still work, and a missing or corrupt snapshot only costs a full rebuild.

No line of advanced/lib/durable.py changes: SnapshotStore subclasses RunStore (an extra table in the same file),
SnapshottingRunner subclasses DurableRunner and reuses its _close_tool_round.
Run: python advanced/day1_durable_agents/solutions/ex12_snapshots.py
"""
# test: expect=byte-identical to the full rebuild: True
# test: expect=snapshots stored: 4

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import DurableRunner, RunStore  # noqa: E402
from labkit import get_client, header, is_mock, step  # noqa: E402

import _day1 as d1  # noqa: E402

STARTER = Path(__file__).resolve().parents[1] / "exercises" / "ex12_snapshots.py"
EVERY = 10


def load_starter():
    spec = importlib.util.spec_from_file_location("ex12_starter", STARTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


starter = load_starter()


class SnapshotStore(RunStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self._connect().execute("CREATE TABLE IF NOT EXISTS snapshots (run_id TEXT, seq INTEGER, turns INTEGER, "
                                "replayed INTEGER, messages TEXT, results TEXT, PRIMARY KEY (run_id, seq))")
        self.rows_read = 0

    def events(self, run_id: str, *, types=None) -> list[dict]:
        out = super().events(run_id, types=types)
        self.rows_read += len(out)
        return out

    def events_after(self, run_id: str, seq: int) -> list[dict]:
        rows = self._connect().execute("SELECT seq, type, payload, at FROM events WHERE run_id = ? AND seq > ? "
                                       "ORDER BY seq", (run_id, seq)).fetchall()
        self.rows_read += len(rows)
        return [{"seq": r["seq"], "type": r["type"], "at": r["at"], **json.loads(r["payload"])} for r in rows]

    def save_snapshot(self, run_id: str, seq: int, state: tuple) -> None:
        messages, results, turns, replayed = state
        self._connect().execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                                (run_id, seq, turns, replayed, json.dumps(messages), json.dumps(results)))

    def latest_snapshot(self, run_id: str) -> dict | None:
        row = self._connect().execute("SELECT * FROM snapshots WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                                      (run_id,)).fetchone()
        self.rows_read += 1
        return dict(row) if row else None

    def snapshot_bytes(self, run_id: str) -> tuple[int, int]:
        rows = self._connect().execute("SELECT LENGTH(messages) + LENGTH(results) FROM snapshots WHERE run_id = ?",
                                       (run_id,)).fetchall()
        return len(rows), sum(r[0] for r in rows)


class SnapshottingRunner(DurableRunner):
    def run(self, run_id: str):
        self._current = run_id
        return super().run(run_id)

    def _maybe_crash(self, point: str, turn: int) -> None:
        if point == "after_model" and turn % EVERY == 0:          # the response is logged: fold and save
            state, seq = self.fold(self._current)
            self.store.save_snapshot(self._current, seq, state)
        super()._maybe_crash(point, turn)

    def rebuild(self, run_id: str):
        return self.fold(run_id)[0]

    def fold(self, run_id: str) -> tuple[tuple, int]:
        """DurableRunner.rebuild's fold, started from the latest snapshot instead of the first event."""
        snap = self.store.latest_snapshot(run_id)
        if snap is None:
            messages = [{"role": "user", "content": self.store.get(run_id).input.get("message", "")}]
            results: dict = {}
            turns = replayed = seq = 0
        else:
            messages, results = json.loads(snap["messages"]), json.loads(snap["results"])
            turns, replayed, seq = snap["turns"], snap["replayed"], snap["seq"]
        for event in self.store.events_after(run_id, seq):
            seq = event["seq"]
            if event["type"] == "model.response":
                replayed += self._close_tool_round(messages, results)
                messages.append({"role": "assistant", "content": event["content"]})
                turns += 1
            elif event["type"] == "tool.result":
                results[event["tool_use_id"]] = {"content": event["content"], "is_error": bool(event.get("is_error"))}
            elif event["type"] == "user.message":
                replayed += self._close_tool_round(messages, results)
                messages.append({"role": "user", "content": event["content"]})
        replayed += self._close_tool_round(messages, results)
        return (messages, results, turns, replayed), seq


def same_state(a: tuple, b: tuple) -> bool:
    return (d1.transcript_bytes(a[0]) == d1.transcript_bytes(b[0]) and json.dumps(a[1], sort_keys=True)
            == json.dumps(b[1], sort_keys=True) and a[2:] == b[2:])


def main() -> None:
    client = get_client()
    header("Exercise 12 - snapshots and a tail rebuild (solution)")
    if is_mock():
        print("[mock] the stand-in model checks one location per turn (a live model may batch its calls); the snapshots and the fold are real code.")

    step(1, "Full rebuild (the starter): DurableRunner reads every event")
    full = starter.crash_and_resume(client, starter.SnapshotStore, starter.SnapshottingRunner, "ex12_full")
    for key in ("events at the crash", "rows read by rebuild()", "resumed", "resumed request extends the crashed one"):
        print(f"  {key:<42} {full[key]}")

    step(2, f"Snapshots every {EVERY} turns: the latest snapshot plus the tail")
    snap = starter.crash_and_resume(client, SnapshotStore, SnapshottingRunner, "ex12_snapshots")
    for key in ("events at the crash", "rows read by rebuild()", "resumed", "resumed request extends the crashed one"):
        print(f"  {key:<42} {snap[key]}")
    store: SnapshotStore = snap["store"]
    count, size = store.snapshot_bytes(snap["run_id"])
    log_bytes = sum(len(json.dumps(e, default=str)) for e in RunStore(store.path).events(snap["run_id"]))
    print(f"  snapshots stored: {count} ({size:,} bytes) next to a log of {log_bytes:,} bytes")

    step(3, "Byte-identity: the snapshot rebuild against DurableRunner.rebuild on the same log")
    print(f"  at the crash (turn 38):  byte-identical to the full rebuild: {same_state(snap['state'], snap['reference'])}")
    checker = starter.auditor(store, client, None, "checker", SnapshottingRunner)
    final_snap, final_full = checker.rebuild(snap["run_id"]), DurableRunner.rebuild(checker, snap["run_id"])
    print(f"  after the resumed run:   byte-identical to the full rebuild: {same_state(final_snap, final_full)}")
    print(f"  turns={final_full[2]} results={len(final_full[1])} replayed={final_full[3]}; "
          f"reply: {d1.short(snap['reply'], 80)}")
    print("\nSnapshots pay off when rebuilds read thousands of events; for a 41-turn run both paths take "
          "milliseconds. Snapshotting often is a storage bill that grows with the square of the turns (exercise 3).")


if __name__ == "__main__":
    main()
