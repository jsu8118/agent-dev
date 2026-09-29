"""Lab 05 - Approvals that wait: a run parked for a manager, decided from another process, resumed later.

Objective
    Give the support agent a refund gate: refunds above the agent's limit raise ApprovalRequired, which parks the
    run durably (status waiting_approval, no lease, no model call while it waits).  Decide the approval from a
    second RunStore on the same database file - the way an approval UI, a chat bot or a CLI would - and let any
    worker resume the run with a plain run(): the runtime answers the settled approval before anything else
    (and you see what happened when it did not).  Then the rejected path, a timeout sweeper that escalates and
    then expires the approval (with an escalation behind what the customer is told), and the alternative the
    first course used: stop and ask, with a read-only toolset.

Concepts
    ApprovalRequired and the approvals table, durable waits (hours or days, no process holding state), deciding
    from another process, idempotent decisions, the approved action as an at-most-once effect, answering settled
    approvals before the loop continues, rejected approvals as tool errors the model must explain, SLA sweepers
    (escalate, then expire - store.expire(): expiry is not a rejection), and the comparison with stop-and-ask /
    tool_choice patterns and Managed Agents' tool confirmations.

Run
    python advanced/day1_durable_agents/labs/05_approvals_that_wait.py

What to observe
    * status=waiting_approval with lease_owner=None; a second run() call returns immediately without a model call.
    * The approval UI (another RunStore instance) lists the parked run, sees the action, decides; the run goes
      back to pending.
    * Before the runtime answered settled approvals itself, run() on a decided run executed the gated tool
      again and parked the run on a NEW approval; today run() issues refund RF-7001 approved_by ops.manager, once.
    * The rejected run: the tool result says who declined and why, and the reply tells the customer honestly.
    * The sweeper: approval.escalated after 4 h, store.expire() after 2 days (status "expired"), and the reply
      names an escalation that really exists (ESC-4101) - "overdue", not "refused".
"""
# test: expect=waiting_approval
# test: expect=could not be approved
# test: expect=approvals now: ['approved', 'pending']
# test: expect=approved by ops.manager

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import DurableRunner, RunStore
from kestrel.support_agent import run_support_agent
from kestrel.support_tools import TOOLS, SupportDesk
from labkit import get_client, header, is_mock, step, wrap
from labkit.data import memory_db

import _day1 as d1

READ_ONLY = [t for t in TOOLS if t["name"] in ("get_customer_profile", "get_order", "get_rma", "get_invoice",
                                                "search_knowledge_base", "escalate_to_human")]


def worker(store, client, db, name: str, runner_cls=DurableRunner):
    desk = SupportDesk(d1.APPROVALS_EMAIL, db=db, ticket_ref=d1.APPROVALS_TICKET)
    return d1.support_runner(store, client, execute=d1.gated_executor(desk, db), worker=name, system=d1.APPROVALS_SYSTEM,
                             runner_cls=runner_cls)


def park(store, client, db, run_id: str):
    run = store.create("support", input=d1.approvals_input(), run_id=run_id)
    return run, worker(store, client, db, "worker-a").run(run.id)


# ---------------------------------------------------------------------------- the runtime before the fix
class RunnerBeforeTheFix(DurableRunner):
    """DurableRunner as it was before it answered settled approvals itself: run() rebuilt the transcript, found
    the gated tool call without a result, and executed it again - without the approval."""

    def _answer_decided(self, run_id: str) -> None:
        pass


# ---------------------------------------------------------------------------- the SLA sweeper
def sweep_approvals(store: RunStore, *, now: dt.datetime, escalate_after: dt.timedelta, expire_after: dt.timedelta,
                    by: str = "sla-sweeper") -> list[str]:
    """A cron job every few minutes: escalate approvals nobody picked up, expire the ones nobody decided."""
    actions = []
    for run in store.list(status="waiting_approval"):
        escalated = {e["approval_id"] for e in store.events(run.id, types=("approval.escalated",))}
        for a in store.approvals(run.id):
            if a["status"] != "pending":
                continue
            age = now - dt.datetime.fromisoformat(a["requested_at"])
            if age >= expire_after:
                store.expire(a["approval_id"], by=by, note=f"no decision within {expire_after.days} days")
                actions.append(f"{run.id}: expired after {age.days} days -> run back in the queue")
            elif age >= escalate_after and a["approval_id"] not in escalated:
                store.append(run.id, "approval.escalated", {"approval_id": a["approval_id"], "from": a["action"].get("queue"),
                                                            "to": "finance_director", "after_s": age.total_seconds()})
                actions.append(f"{run.id}: escalated to finance_director after {age.total_seconds() / 3600:.0f} h")
    return actions


# ---------------------------------------------------------------------------- the steps
def step_park(store, client, db):
    run, outcome = park(store, client, db, "sd-T-1207-approve")
    record = store.get(run.id)
    print(f"worker-a: {d1.outcome_line(outcome)}")
    print(f"store: status={record.status} lease_owner={record.lease_owner} refunds={d1.refunds(db)}")
    if not store.approvals(run.id):
        print("the model never attempted the refund, so nothing is waiting (live: the model took a different path "
              "than the stand-in - rerun the lab)")
        return None
    approval = store.approvals(run.id)[0]
    print(f"approval: status={approval['status']} action={d1.short(approval['action'], 120)}")
    before = d1.model_calls()
    again = worker(store, client, db, "worker-c").run(run.id)
    print(f"\nanother worker picks it up meanwhile: {d1.outcome_line(again)} - model calls made: "
          f"{d1.model_calls() - before}")
    print(wrap("The run holds no thread, no process and no lease while it waits: it is a row with "
               "status=waiting_approval and a pending approval. Any worker that finds it returns at once. It can "
               "wait an hour or a week at the same cost: nothing."))
    return run


def step_decide(store, run):
    ui = RunStore(store.path)                       # the approval UI: another process opening the same file
    waiting = ui.list(status="waiting_approval")
    print(f"approval UI sees {len(waiting)} run(s) waiting: {[r.id for r in waiting]}")
    pending = [a for a in ui.approvals(run.id) if a["status"] == "pending"][0]
    action = pending["action"]
    print(f"  {action['summary']} - tool {action['name']} input {d1.short(action['input'], 80)}")
    decided = ui.decide(pending["approval_id"], approved=True, by="ops.manager", note="inspection report checked")
    print(f"  ops.manager approves -> run status {decided.status} (back in the queue)")
    twice = ui.decide(pending["approval_id"], approved=True, by="someone.else")
    print(f"  a second click on 'approve' -> status {twice.status}, decided_by still "
          f"{ui.approvals(run.id)[0]['decided_by']}: deciding is idempotent")


def step_resume(store, client, db, run):
    old_store, old_db = d1.fresh_store("05_before_the_fix"), memory_db()
    old_run, parked = park(old_store, client, old_db, "sd-T-1207-before")
    if parked.approval_id is None:
        print("the copy did not park (live: the model took a different path) - skipping the before-the-fix demo")
    else:
        old_store.decide(parked.approval_id, approved=True, by="ops.manager")
        outcome = worker(old_store, client, old_db, "worker-b", RunnerBeforeTheFix).run(old_run.id)
        print("before the runtime answered decisions itself, a worker calling run() on the decided run (a copy):")
        print(f"  {d1.outcome_line(outcome)} | approvals now: {[a['status'] for a in old_store.approvals(old_run.id)]} "
              f"| refunds: {d1.refunds(old_db)}")
        print(wrap("It rebuilt the transcript, found issue_refund without a result and executed it again - without "
                   "the approval, so the gate asked again: the manager's decision was ignored and a second approval "
                   "waited in the UI. Every queue consumer had to route decided runs to a separate resume call, and "
                   "the one that forgot did this."))

    print("\ntoday, any worker calls run(); the runtime answers the settled approval before the loop continues:")
    outcome = worker(store, client, db, "worker-b").run(run.id)
    print(f"  worker-b: {d1.outcome_line(outcome)}")
    print("  reply:\n" + wrap(outcome.reply, indent="    "))
    print(f"  refunds: {d1.refunds(db)}")
    print(wrap("executed_tools=0 and replayed_tools=3: the approved refund ran before the loop, as an at-most-once "
               "effect under its own key (<run>:<tool_use>:approved), and was logged as the tool result; the loop "
               "then replayed it with the other two. The log since the request:"))
    seq = [e["seq"] for e in store.events(run.id, types=("approval.requested",))][0]
    d1.print_log(store, run.id, since_seq=seq - 1)


def step_reject(store, client, db):
    run, outcome = park(store, client, db, "sd-T-1207-reject")
    print(f"worker-a: {d1.outcome_line(outcome)}")
    if outcome.approval_id is None:
        print("nothing to reject (live: the model took a different path)")
        return
    store.decide(outcome.approval_id, approved=False, by="ops.manager",
                 note="the unit shows signs of installation; refund on hold pending a second inspection")
    outcome = worker(store, client, db, "worker-b").run(run.id)
    print(f"worker-b: {d1.outcome_line(outcome)}")
    result = store.events(run.id, types=("tool.result",))[-1]
    print(f"tool result the model saw: {d1.short(result['content'], 150)}")
    print("reply:\n" + wrap(outcome.reply))
    print(f"refunds: {d1.refunds(db)}")


def step_sweeper(store, client, db):
    run, parked = park(store, client, db, "sd-T-1207-overdue")
    if parked.approval_id is None:
        print("nothing to sweep (live: the model took a different path)")
        return
    requested = dt.datetime.fromisoformat(store.approvals(run.id)[0]["requested_at"])
    ladder = dict(escalate_after=dt.timedelta(hours=4), expire_after=dt.timedelta(days=2))
    for label, later in (("10 minutes", dt.timedelta(minutes=10)), ("4 hours", dt.timedelta(hours=4)),
                         ("1 day", dt.timedelta(days=1)), ("2 days", dt.timedelta(days=2))):
        actions = sweep_approvals(store, now=requested + later, **ladder)
        print(f"  sweep at +{label:<10} -> {actions or ['nothing to do']}")
    print(f"  run status now: {store.get(run.id).status}")
    outcome = worker(store, client, db, "worker-b").run(run.id)
    print(f"worker-b: {d1.outcome_line(outcome)}  tools after the expiry: "
          f"{d1.tool_calls(store, run.id)[d1.tool_calls(store, run.id).index('issue_refund') + 1:]}")
    print("reply:\n" + wrap(outcome.reply))
    escalations = [dict(r) for r in db.execute("SELECT escalation_id, queue, priority FROM escalations")]
    print(f"escalations in the system of record: {escalations}")
    print("events:")
    d1.print_log(store, run.id, types=("approval.requested", "approval.escalated", "approval.decided", "run.status"))
    print(wrap("The ladder is policy, not runtime: the store keeps requested_at, the sweeper applies the SLA, and "
               "store.expire() settles the approval with its own status - expiry is not a refusal, nobody said "
               "no. The run resumes with a tool error that says the request was not decided in time, which the "
               "model answers by escalating - so the follow-up the customer is promised is a record with an owner "
               "and an SLA, not a sentence. A late approval after the expiry is a NEW decision on a new approval - "
               "the old one stays closed, which is the audit trail finance wants."))


def step_stop_and_ask(client):
    desk = SupportDesk(d1.APPROVALS_EMAIL, db=memory_db(), ticket_ref=d1.APPROVALS_TICKET)
    result = run_support_agent(client, d1.APPROVALS_MESSAGE, d1.APPROVALS_EMAIL, desk=desk,
                               ticket_ref=d1.APPROVALS_TICKET, tools=READ_ONLY)
    print(f"in-memory loop with a read-only toolset: turns={result.turns} tools={[c['name'] for c in result.tool_calls]}")
    print("reply:\n" + wrap(result.reply))
    if is_mock():
        print("[mock] the first course's stand-in does not escalate here, so nothing records the promise; live, Claude "
              "may call escalate_to_human (it is in the read-only set). Either way the run ends.")
    print(wrap("Stop-and-ask ends the conversation: the customer is told a colleague will follow up, and the "
               "manager's approval later starts a new conversation with none of this context. The durable wait "
               "keeps the run - transcript, tool results, cache prefix, thinking blocks - and resumes it. The "
               "tool_choice variants (force a 'request_approval' tool, or tool_choice none while waiting) shape "
               "what the model may do next; they do not park anything, and forcing a tool is a 400 on Claude "
               "Opus 5.5 and Claude Fable 5.1. Managed Agents sessions have the same wait built in for hosted tools: "
               "an always_ask permission policy idles the session with stop_reason requires_action until a "
               "user.tool_confirmation arrives (Day 4)."))


def main() -> None:
    client = get_client()
    header("Lab 05 - Approvals that wait")
    if is_mock():
        print("[mock] the stand-in model follows the refund flow and reports the approval outcome it is given; "
              "the parking, the decision from another RunStore, the resumption and the sweeper are real.")
    store = d1.fresh_store("05_approvals")
    db = memory_db()

    step(1, "A refund above the agent's limit parks the run")
    run = step_park(store, client, db)
    if run is None:
        return

    step(2, "Decided from another process")
    step_decide(store, run)

    step(3, "Resumed by another worker: run() answers the decision first")
    step_resume(store, client, db, run)

    step(4, "The rejected path")
    step_reject(store, client, memory_db())

    step(5, "Nobody decides: escalate, then expire, on a schedule")
    step_sweeper(store, client, memory_db())

    step(6, "The alternative: stop and ask (read-only toolset)")
    step_stop_and_ask(client)


if __name__ == "__main__":
    main()
