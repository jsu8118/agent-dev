"""Exercise 9 starter - cancel a durable run, wherever it is.

An operator (or the customer: "please ignore my last email") must be able to cancel a run from any process:
before a worker picks it up, while it waits for an approval, and while a worker is in the middle of it. The
shipped runtime has a 'cancelled' status but nothing that sets it or respects it. Write, WITHOUT editing
advanced/lib/durable.py (subclass or wrap it):

  * cancel(store, run_id, by=, reason=) -> status: durable, idempotent, callable from any process;
  * CancellableRunner(DurableRunner): a worker that stops at its next checkpoint once a cancel is requested -
    before every model call and around every tool call - and records what had already happened.

This starter runs the four scenarios with the do-nothing versions below and prints what happens now.
Run: python advanced/day1_durable_agents/exercises/ex09_cancel_run.py
Solution: advanced/day1_durable_agents/solutions/ex09_cancel_run.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import DurableRunner, RunStore  # noqa: E402
from kestrel.support_tools import SupportDesk  # noqa: E402
from labkit import get_client, header  # noqa: E402
from labkit.data import memory_db  # noqa: E402

import _day1 as d1  # noqa: E402


def cancel(store: RunStore, run_id: str, *, by: str, reason: str) -> str:
    """TODO: record the request durably, finish the cancel at once when no live worker holds the run (close its
    pending approvals, mark it cancelled), and return the run's status. Cancelling twice, or cancelling a
    finished run, must change nothing."""
    return store.get(run_id).status


class CancellableRunner(DurableRunner):
    """TODO: stop at the next checkpoint once a cancel was requested (hint: the runner calls self._maybe_crash at
    'after_model', 'before_tool' and 'after_tool', and self.client.beta.messages.create before every turn)."""


# ---------------------------------------------------------------------------- the scenarios (keep these)
def refund_worker(store, client, db, name: str, *, delay_s: float = 0.0):
    """The support agent on T-1206 (a $663 refund within the agent's limit); get_rma can be slowed down."""
    ticket = d1.ticket("T-1206")
    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    tools = d1.desk_executor(desk)

    def execute(tool, tool_input, ctx):
        if tool == "get_rma":
            time.sleep(delay_s)
        return tools(tool, tool_input, ctx)

    return d1.support_runner(store, client, execute=execute, worker=name, runner_cls=CancellableRunner)


def approval_worker(store, client, db, name: str):
    desk = SupportDesk(d1.APPROVALS_EMAIL, db=db, ticket_ref=d1.APPROVALS_TICKET)
    return d1.support_runner(store, client, execute=d1.gated_executor(desk, db), worker=name,
                             system=d1.APPROVALS_SYSTEM, runner_cls=CancellableRunner)


def scenario_pending(client) -> dict:
    store, db = d1.fresh_store("ex09_pending"), memory_db()
    run = store.create("support", input=d1.run_input(d1.ticket("T-1206")), run_id="cancel-pending")
    status = cancel(RunStore(store.path), run.id, by="ops.lead", reason="duplicate of T-1199")
    calls = d1.model_calls()
    outcome = refund_worker(store, client, db, "worker-a").run(run.id)
    return {"cancel() returned": status, "worker outcome": outcome.status,
            "model calls after the cancel": d1.model_calls() - calls, "refunds": len(d1.refunds(db))}


def scenario_waiting(client) -> dict:
    store, db = d1.fresh_store("ex09_waiting"), memory_db()
    run = store.create("support", input=d1.approvals_input(), run_id="cancel-waiting")
    approval_worker(store, client, db, "worker-a").run(run.id)
    status = cancel(RunStore(store.path), run.id, by="travis.greer (customer)", reason="customer withdrew the request")
    outcome = approval_worker(store, client, db, "worker-b").run(run.id)     # a worker picks it up again
    return {"cancel() returned": status, "worker outcome": outcome.status,
            "approvals": [a["status"] for a in store.approvals(run.id)], "refunds": len(d1.refunds(db))}


def scenario_running(client) -> dict:
    store, db = d1.fresh_store("ex09_running"), memory_db()
    run = store.create("support", input=d1.run_input(d1.ticket("T-1206")), run_id="cancel-running")
    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(
        outcome=refund_worker(store, client, db, "worker-a", delay_s=0.8).run(run.id)))
    thread.start()
    time.sleep(0.4)                                            # worker-a is inside its slow get_rma now
    status = cancel(RunStore(store.path), run.id, by="ops.lead", reason="customer asked to hold the refund")
    thread.join()
    return {"cancel() returned": status, "worker outcome": result["outcome"].status,
            "tools run": d1.tool_calls(store, run.id), "refunds": len(d1.refunds(db))}


def scenario_idempotent(client) -> dict:
    store, db = d1.fresh_store("ex09_idempotent"), memory_db()
    done = store.create("support", input=d1.run_input(d1.ticket("T-1206")), run_id="cancel-done")
    refund_worker(store, client, db, "worker-a").run(done.id)
    other = store.create("support", input=d1.run_input(d1.ticket("T-1206")), run_id="cancel-twice")
    first = cancel(store, other.id, by="ops.lead", reason="test")
    second = cancel(store, other.id, by="ops.lead", reason="test again")
    return {"cancel(completed run)": cancel(store, done.id, by="ops.lead", reason="too late"),
            "cancel, cancel again": [first, second],
            "cancel events logged": len(store.events(other.id, types=("run.cancel_requested",)))}


SCENARIOS = [("a. cancelled before any worker picked it up", scenario_pending),
             ("b. cancelled while waiting for an approval", scenario_waiting),
             ("c. cancelled while a worker is inside a slow tool", scenario_running),
             ("d. idempotence: a finished run, and cancelling twice", scenario_idempotent)]


def main() -> None:
    client = get_client()
    header("Exercise 9 - cancel(run_id) (starter)")
    for title, fn in SCENARIOS:
        print(f"\n{title}")
        for key, value in fn(client).items():
            print(f"  {key:<30} {value}")
    print("\nTODO: nothing above was cancelled - (a) and (c) issued the refund, (b) still waits for a manager.")
    print("TODO: implement cancel() and CancellableRunner so that (a) makes no model call, (b) closes the approval")
    print("      and issues no refund, (c) stops after get_rma with no refund, (d) changes nothing twice.")


if __name__ == "__main__":
    main()
