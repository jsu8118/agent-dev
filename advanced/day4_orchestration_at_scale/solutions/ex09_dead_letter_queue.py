"""Solution to exercise 9 - a dead-letter queue: error history, error classes, poison detection, requeue, alerts.

A dead letter is a task the system gave up on. What makes a dead-letter queue useful is not the table but what it
lets a person do at 3 a.m.: see every attempt's error (not just the last), know whether retrying can help (the error
class, and whether every attempt failed the same way), put the task back once the cause is fixed without losing its
history, and be told when the queue grows instead of finding out from a customer.

Run: python advanced/day4_orchestration_at_scale/solutions/ex09_dead_letter_queue.py
"""
# test: expect=PAGE
# test: expect=requeued by ops.oncall

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "labs"))
sys.path.insert(0, str(HERE.parents[1] / "exercises"))

from labkit import header, step  # noqa: E402

import _day4 as d4  # noqa: E402
import ex09_dead_letter_queue as starter  # noqa: E402

DLQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_errors (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, attempt INTEGER, error_class TEXT, error TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS dead_letters (
    task_id TEXT PRIMARY KEY, kind TEXT, payload TEXT, attempts INTEGER, error_class TEXT, first_error TEXT,
    last_error TEXT, errors TEXT, poison INTEGER, dead_at TEXT, requeued_by TEXT, requeue_note TEXT, requeued_at TEXT);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class DeadLetterQueue(d4.WorkQueue):
    def __init__(self, path) -> None:
        super().__init__(path)
        self._conn().executescript(DLQ_SCHEMA)
        self._callbacks = []

    def on_dead(self, callback) -> None:
        self._callbacks.append(callback)

    # ------------------------------------------------------------------ failures
    def _record(self, task: d4.Task, error: str, error_class: str) -> None:
        self._conn().execute("INSERT INTO task_errors (task_id, attempt, error_class, error, at) VALUES (?,?,?,?,?)",
                             (task.task_id, task.attempts, error_class, error, _now()))

    def _dead_letter(self, task: d4.Task, error_class: str) -> dict:
        errors = [dict(r) for r in self._conn().execute(
            "SELECT attempt, error_class, error FROM task_errors WHERE task_id = ? ORDER BY seq", (task.task_id,))]
        messages = [e["error"] for e in errors]
        poison = len(messages) > 1 and len(set(messages)) == 1          # every attempt failed the same way
        self._conn().execute("UPDATE tasks SET status = 'dead', owner = NULL, lease_until = NULL, error = ?, "
                             "version = version + 1, updated_at = ? WHERE task_id = ?", (messages[-1], _now(), task.task_id))
        self._conn().execute(
            "INSERT OR REPLACE INTO dead_letters (task_id, kind, payload, attempts, error_class, first_error, last_error, "
            "errors, poison, dead_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task.task_id, task.kind, json.dumps(task.payload), task.attempts, error_class, messages[0], messages[-1],
             json.dumps(errors), int(poison), _now()))
        row = next(r for r in self.dead_letter_rows() if r["task_id"] == task.task_id)
        for callback in self._callbacks:
            callback(row)
        return row

    def fail_classified(self, task_id: str, owner: str, error: str, *, error_class: str) -> str:
        task = self.get(task_id)
        if task.status != "claimed" or task.owner != owner:
            return "ignored (the caller no longer holds the claim)"      # a zombie's late report changes nothing
        self._record(task, error, error_class)
        if error_class == "permanent" or task.attempts >= task.max_attempts:
            self._dead_letter(task, error_class)
            return "dead"
        self._conn().execute("UPDATE tasks SET status = 'queued', owner = NULL, lease_until = NULL, error = ?, "
                             "version = version + 1, updated_at = ? WHERE task_id = ?", (error, _now(), task_id))
        return "queued"

    def reap(self, *, now: float | None = None) -> list[str]:
        """Expired leases are transient failures too: re-queue, or dead-letter on the last attempt - with history."""
        import time
        now = time.time() if now is None else now
        rows = self._conn().execute("SELECT * FROM tasks WHERE status = 'claimed' AND lease_until < ?", (now,)).fetchall()
        out = []
        for row in rows:
            task = d4.Task.from_row(row)
            self._record(task, f"lease expired (owner {task.owner})", "transient")
            if task.attempts >= task.max_attempts:
                self._dead_letter(task, "transient")
            else:
                self._conn().execute("UPDATE tasks SET status = 'queued', owner = NULL, lease_until = NULL, "
                                     "version = version + 1 WHERE task_id = ?", (task.task_id,))
            out.append(task.task_id)
        return out

    # ------------------------------------------------------------------ people
    def dead_letter_rows(self) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM dead_letters WHERE requeued_at IS NULL ORDER BY dead_at").fetchall()
        return [{**dict(r), "errors": json.loads(r["errors"]), "payload": json.loads(r["payload"])} for r in rows]

    def requeue(self, task_id: str, *, by: str, note: str) -> bool:
        cur = self._conn().execute("UPDATE dead_letters SET requeued_by = ?, requeue_note = ?, requeued_at = ? "
                                   "WHERE task_id = ? AND requeued_at IS NULL", (by, note, _now(), task_id))
        if cur.rowcount != 1:
            return False
        self._conn().execute("UPDATE tasks SET status = 'queued', attempts = 0, owner = NULL, lease_until = NULL, "
                             "error = NULL, version = version + 1, updated_at = ? WHERE task_id = ?", (_now(), task_id))
        self._conn().execute("INSERT INTO task_errors (task_id, attempt, error_class, error, at) VALUES (?,?,?,?,?)",
                             (task_id, 0, "requeue", f"requeued by {by}: {note}", _now()))
        return True

    def history(self, task_id: str) -> list[dict]:
        return [dict(r) for r in self._conn().execute(
            "SELECT attempt, error_class, error FROM task_errors WHERE task_id = ? ORDER BY seq", (task_id,))]


def main() -> None:
    header("Exercise 9 - a dead-letter queue (solution)")
    dlq = DeadLetterQueue(d4.fresh_db("ex09_solution.db"))
    pages = []
    dlq.on_dead(lambda row: pages.append(row) or print(f"    PAGE on-call: {row['task_id']} dead-lettered "
                                                       f"({row['error_class']}{', poison' if row['poison'] else ''}): {row['last_error']}"))
    desk = starter.make_desk()

    step(1, "The same traffic as the starter, through the dead-letter queue")
    starter.drive(dlq, desk, fail=lambda t, e, c: dlq.fail_classified(t.task_id, "worker-1", e, error_class=c))
    print(f"  queue: {dlq.stats()}")

    step(2, "What the on-call engineer sees")
    for row in dlq.dead_letter_rows():
        print(f"  {row['task_id']}: {row['attempts']} attempt(s), class {row['error_class']}, poison={bool(row['poison'])}")
        for e in row["errors"]:
            print(f"    attempt {e['attempt']}: [{e['error_class']}] {e['error']}")
    print("  The poison flag says every attempt failed identically: retrying without a fix only burns attempts.")
    print("  The permanent one needs data fixed (the serial does not exist), not a retry.")
    print(f"  late report from a worker that lost its claim: "
          f"{dlq.fail_classified('unit:KP250-2608-0002', 'worker-9', 'timeout', error_class='transient')}")

    step(3, "The scheduling outage is fixed; a person requeues the task - history kept")
    desk.faults.pop("find_engineer_slots")
    ok = dlq.requeue("unit:KP100-2608-0002", by="ops.oncall", note="calendar API restored at 14:05")
    print(f"  requeue(unit:KP100-2608-0002) -> {ok}; requeue(unit:KP250-2608-0003) -> "
          f"{dlq.requeue('unit:KP250-2608-0003', by='ops.oncall', note='x')} (not a dead letter)")
    starter.drive(dlq, desk, fail=lambda t, e, c: dlq.fail_classified(t.task_id, "worker-1", e, error_class=c))
    print(f"  queue: {dlq.stats()}; still dead: {[r['task_id'] for r in dlq.dead_letter_rows()]}")
    for e in dlq.history("unit:KP100-2608-0002"):
        print(f"    history: attempt {e['attempt']} [{e['error_class']}] {e['error']}")
    print(f"  pages sent: {len(pages)}. The requeued task was requeued by ops.oncall and then booked on its first new attempt.")


if __name__ == "__main__":
    main()
