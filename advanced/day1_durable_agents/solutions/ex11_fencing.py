"""Solution to exercise 11 - fence the zombie: writes are conditional on holding the lease.

A lease tells workers who SHOULD be running a run; it cannot stop a worker that does not know it lost it (a GC
pause, a slow tool, a network partition). Fencing moves the check to the place the damage happens - the
write. Every write the worker makes carries its identity, and the store accepts it only if that identity still
holds the lease, in the same statement:

    INSERT INTO events (...) SELECT ... WHERE EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND lease_owner = ?)

The runtime's own check - heartbeat() before every model call, LeaseLost when it fails - stops the zombie too,
but only after the write it made on returning from its slow tool; the fence refuses that write, so the log never
holds a second answer to the same tool call. What fencing cannot do: undo what the zombie did OUTSIDE the store
while it believed it held the lease -
the read it repeated, or a write to another system. That is what idempotency keys are for (the zombie and the
new owner answer the same tool_use, so they carry the same key), and why a downstream system you control should
accept a fencing token (a lease version) too.

No line of advanced/lib/durable.py changes: FencedStore subclasses RunStore.
Run: python advanced/day1_durable_agents/solutions/ex11_fencing.py
"""
# test: expect=stopped: LeaseLost: worker-a may not write
# test: expect=3/3 checks passed

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import LeaseLost, RunStore  # noqa: E402
from labkit import get_client, header, is_mock, step  # noqa: E402


STARTER = Path(__file__).resolve().parents[1] / "exercises" / "ex11_fencing.py"


def load_starter():
    spec = importlib.util.spec_from_file_location("ex11_starter", STARTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


starter = load_starter()


def now_iso() -> str:
    """The event timestamp format RunStore uses (UTC, milliseconds)."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class FencedStore(RunStore):
    """A RunStore opened by ONE worker: its writes to a run succeed only while that worker holds the lease."""

    UNFENCED = ("run.created",)                          # a run is created before anyone can lease it

    def __init__(self, path, *, owner: str) -> None:
        super().__init__(path)
        self.owner = owner

    def append(self, run_id: str, type: str, payload: dict | None = None) -> int:
        if type in self.UNFENCED:
            return super().append(run_id, type, payload)
        cur = self._connect().execute(
            "INSERT INTO events (run_id, type, payload, at) SELECT ?, ?, ?, ? "
            "WHERE EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND lease_owner = ?)",
            (run_id, type, json.dumps(payload or {}, default=str), now_iso(), run_id, self.owner))
        if cur.rowcount != 1:
            raise LeaseLost(f"{self.owner} may not write {type} to {run_id}: the lease is "
                            f"{self.get(run_id).lease_owner or 'free'}")
        return int(cur.lastrowid)

    def set_status(self, run_id: str, status: str, *, result=None, error: str | None = None) -> None:
        cur = self._connect().execute(
            "UPDATE runs SET status = ?, result = COALESCE(?, result), error = ?, updated_at = ? "
            "WHERE run_id = ? AND lease_owner = ?",
            (status, json.dumps(result) if result is not None else None, error, now_iso(), run_id, self.owner))
        if cur.rowcount != 1:
            raise LeaseLost(f"{self.owner} may not set {run_id} to {status}: it no longer holds the lease")
        self.append(run_id, "run.status", {"status": status, "error": error})


def main() -> None:
    client = get_client()
    header("Exercise 11 - fencing a zombie worker (solution)")
    if is_mock():
        print("[mock] the stand-in model drives the run; the threads, the lease expiry and the fenced writes are real.")

    step(1, "Unfenced (the starter): the zombie writes after the takeover")
    before = starter.false_takeover(client, starter.FencedStore, "ex11_unfenced")
    for key, value in before.items():
        print(f"  {key:<18} {value}")

    step(2, "Fenced: the same takeover")
    after = starter.false_takeover(client, FencedStore, "ex11_fenced")
    for key, value in after.items():
        print(f"  {key:<18} {value}")

    checks = {"worker-a stopped at its first write after the takeover":
              after["worker-a"].startswith("stopped: LeaseLost: worker-a may not"),
              "one get_order result and three model responses": after["get_order results"] == 1
              and after["log"].startswith("model.response=3"),
              "worker-b completed the run": after["final status"] == "completed"}
    print()
    for name, ok in checks.items():
        print(f"  check: {name}: {'OK' if ok else 'FAILED'}")
    print(f"\n{sum(checks.values())}/{len(checks)} checks passed")
    print("(the zombie's slow get_order still ran to the end: fencing stops the writes, not the work already in "
          "progress - which is why the tools behind it must be idempotent too)")


if __name__ == "__main__":
    main()
