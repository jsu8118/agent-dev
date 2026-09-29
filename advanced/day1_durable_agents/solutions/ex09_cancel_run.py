"""Solution to exercise 9 - cancel(run_id): a durable request, honoured at the next checkpoint.

Design, in four decisions:
1. A cancel is an EVENT first (run.cancel_requested), then a status. Any process can append it; the status
   changes only under the run's lease, so a cancel never races a worker for the run's state.
2. If no live worker holds the run (pending, waiting for an approval, or abandoned by a dead worker),
   cancel() takes the lease itself and finishes the job: pending approvals are closed as rejected with the
   reason, a run.cancelled event lists the tools that had already run, and the status becomes cancelled.
3. A worker that holds the run checks for the request at every checkpoint - before each model call (a wrapped
   client), at the runner's own hook points after the model, before and after each tool (_maybe_crash), and
   before run() answers a settled approval (an approved action executes there) - and stops there. A tool already
   running is not interrupted: its effect happens or not, and the log says so.
4. Cancel is not undo. Effects that completed stay completed; run.cancelled names them so a person (or a
   saga's compensations) can decide. Cancelling twice, or cancelling a finished run, changes nothing.

No line of advanced/lib/durable.py changes: the solution subclasses DurableRunner and wraps its client.
Run: python advanced/day1_durable_agents/solutions/ex09_cancel_run.py
"""
# test: expect=4/4 checks passed
# test: expect=run.cancelled

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import DurableRunner, Outcome, RunStore  # noqa: E402
from labkit import get_client, header, is_mock, step  # noqa: E402

import _day1 as d1  # noqa: E402

TERMINAL = ("completed", "failed", "cancelled")
STARTER = Path(__file__).resolve().parents[1] / "exercises" / "ex09_cancel_run.py"


class RunCancelled(Exception):
    """Raised at a checkpoint once the run has a cancel request."""


def cancel_request(store: RunStore, run_id: str) -> dict | None:
    requests = store.events(run_id, types=("run.cancel_requested",))
    return requests[0] if requests else None


def finish_cancel(store: RunStore, run_id: str) -> None:
    """Called only while holding the run's lease."""
    if store.get(run_id).status in TERMINAL:
        return                                               # it finished first: the cancel arrived too late
    request = cancel_request(store, run_id)
    for approval in store.approvals(run_id):
        if approval["status"] == "pending":
            store.decide(approval["approval_id"], approved=False, by=request["by"],
                         note=f"run cancelled: {request['reason']}")
    completed = [e["name"] for e in store.events(run_id, types=("tool.result",))]
    store.append(run_id, "run.cancelled", {"by": request["by"], "reason": request["reason"],
                                           "completed_tools": completed})
    store.set_status(run_id, "cancelled", error=f"cancelled by {request['by']}: {request['reason']}")


def cancel(store: RunStore, run_id: str, *, by: str, reason: str) -> str:
    run = store.get(run_id)
    if run.status in TERMINAL:
        return run.status                                    # idempotent: nothing left to cancel
    if cancel_request(store, run_id) is None:
        store.append(run_id, "run.cancel_requested", {"by": by, "reason": reason})
    holder = f"canceller:{by}"
    if store.acquire(run_id, holder, ttl_s=30.0):            # no live worker: finish the cancel now
        try:
            finish_cancel(store, run_id)
        finally:
            store.release(run_id, holder)
    return store.get(run_id).status                          # 'running': the worker stops at its next checkpoint


class _CheckedClient:
    """The runner's client with a checkpoint before every model call: a cancelled run spends no more tokens."""

    def __init__(self, client, runner: "CancellableRunner") -> None:
        self._client, self._runner = client, runner
        self.beta = self

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self._runner.checkpoint()
        return self._client.beta.messages.create(**kwargs)


class CancellableRunner(DurableRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.client = _CheckedClient(self.client, self)
        self._current: str | None = None

    def checkpoint(self) -> None:
        if self._current is not None and cancel_request(self.store, self._current) is not None:
            raise RunCancelled(self._current)

    def _maybe_crash(self, point: str, turn: int) -> None:  # the runner's hook points: after_model, before/after_tool
        super()._maybe_crash(point, turn)
        self.checkpoint()

    def run(self, run_id: str) -> Outcome:
        self._current = run_id
        try:
            if cancel_request(self.store, run_id) is not None:
                raise RunCancelled(run_id)
            return super().run(run_id)
        except RunCancelled:
            return self._honour_cancel(run_id)
        finally:
            self._current = None

    def _answer_decided(self, run_id: str) -> None:        # run() executes an approved action here: check first
        self.checkpoint()
        super()._answer_decided(run_id)

    def _honour_cancel(self, run_id: str) -> Outcome:
        if self.store.acquire(run_id, self.worker, self.lease_ttl_s):
            try:
                finish_cancel(self.store, run_id)
            finally:
                self.store.release(run_id, self.worker)
        run = self.store.get(run_id)
        return Outcome(run_id, run.status, reply=(run.result or {}).get("reply", ""))


def load_starter():
    spec = importlib.util.spec_from_file_location("ex09_starter", STARTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                      # dataclasses and pickling look the module up by name
    spec.loader.exec_module(module)
    module.cancel, module.CancellableRunner = cancel, CancellableRunner      # run the starter's scenarios on these
    return module


CHECKS = {
    "a": lambda r: r["worker outcome"] == "cancelled" and r["model calls after the cancel"] == 0 and r["refunds"] == 0,
    "b": lambda r: r["worker outcome"] == "cancelled" and r["approvals"] == ["rejected"] and r["refunds"] == 0,
    "c": lambda r: r["worker outcome"] == "cancelled" and "issue_refund" not in r["tools run"] and r["refunds"] == 0,
    "d": lambda r: r["cancel(completed run)"] == "completed" and r["cancel, cancel again"] == ["cancelled", "cancelled"]
                   and r["cancel events logged"] == 1,
}


def main() -> None:
    client = get_client()
    header("Exercise 9 - cancel(run_id) (solution)")
    if is_mock():
        print("[mock] the stand-in model drives the support runs; cancel(), the checkpoints and the log are the real code.")
    starter = load_starter()
    passed = 0
    for number, (title, fn) in enumerate(starter.SCENARIOS, 1):
        step(number, title)
        result = fn(client)
        for key, value in result.items():
            print(f"  {key:<30} {value}")
        ok = CHECKS[title[0]](result)
        passed += ok
        print(f"  check: {'OK' if ok else 'FAILED'}")

    step(5, "The log of scenario c: requested by one process, honoured by another at its next checkpoint")
    store = RunStore(d1.store_path("ex09_running"))
    d1.print_log(store, "cancel-running", since_seq=3)
    print(f"\n{passed}/{len(CHECKS)} checks passed")


if __name__ == "__main__":
    main()
