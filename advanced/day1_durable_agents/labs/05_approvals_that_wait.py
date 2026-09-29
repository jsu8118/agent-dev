"""Lab 05 - Approvals that wait: a run parked for a manager, decided from another process, resumed later.

Objective
    Give the support agent a refund gate: refunds above the agent's limit raise ApprovalRequired, which parks the
    run durably (status waiting_approval, no lease, no model call while it waits).  Decide the approval from a
    second RunStore on the same database file - the way an approval UI, a Slack bot or a CLI would - and watch a
    worker resume the same run: the refund is issued with the approver's name on it and the customer gets the
    reply that was pending.  Then the rejected path, a timeout-and-escalation sweeper, and the alternative the
    first course used: stop and ask, with a read-only toolset.

Concepts
    ApprovalRequired and the approvals table, durable waits (hours or days, no process holding state), deciding
    from another process, idempotent decisions, resume_after_decision, rejected approvals as tool errors the model
    must explain, SLA sweepers (escalate, then decline), and the comparison with stop-and-ask / tool_choice
    patterns and Managed Agents' tool confirmations.

Run
    python advanced/day1_durable_agents/labs/05_approvals_that_wait.py

What to observe
    * status=waiting_approval with lease_owner=None; a second run() call returns immediately without a model call.
    * The approval UI (another RunStore instance) lists the parked run, sees the action, decides; the run goes
      back to pending and the next worker finishes it: refund RF-7001 approved_by ops.manager.
    * The rejected run: the tool result says who declined and why, and the reply tells the customer honestly.
    * The sweeper: approval.escalated after 4 h, auto-declined after 2 days, and the customer told it is
      overdue - not that it failed.
"""
# test: expect=waiting_approval
# test: expect=could not be approved

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import ApprovalRequired, RunStore
from kestrel import policy
from kestrel.support_agent import SYSTEM_PROMPT, run_support_agent
from kestrel.support_tools import TOOLS, SupportDesk, ToolError
from labkit import get_client, header, is_mock, mock_api, step, wrap
from labkit.data import memory_db

import _day1 as d1

SYSTEM = SYSTEM_PROMPT + "\n<adv_day1_approvals>\n"      # the marker the lab's stand-in policy answers to
EMAIL = "travis.greer@midlandoil.example"
MESSAGE = ("Subject: Re: Refund for returned KP-250-X (T-1207)\n\nFollowing up on RMA-7001: the figure of $9,188.50 "
           "is confirmed on our side, so please go ahead and process the refund.\n\nTravis Greer, Midland Oil Services")
READ_ONLY = [t for t in TOOLS if t["name"] in ("get_customer_profile", "get_order", "get_rma", "get_invoice",
                                                "search_knowledge_base", "escalate_to_human")]


# ---------------------------------------------------------------------------- the gate
def approved_refund(db, rma: dict, amount: float, reason: str, approved_by: str) -> dict:
    """The refund path a person's approval unlocks. The agent's own limit stays in the tool; this path records
    who approved (dual control - Day 5 makes it a capability)."""
    refund_id = f"RF-{rma['rma_id'][4:]}"
    db.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?,?,?)",
               (refund_id, rma["order_id"], rma["rma_id"], amount, reason[:200], approved_by, "issued",
                "2026-09-15T09:00:00Z"))
    db.execute("UPDATE rmas SET status = 'refunded' WHERE rma_id = ?", (rma["rma_id"],))
    db.execute("INSERT INTO audit_log (ts, actor, action, target, details) VALUES (?,?,?,?,?)",
               ("2026-09-15T09:00:00Z", f"support-agent:approved-by:{approved_by}", "issue_refund", refund_id,
                json.dumps({"rma_id": rma["rma_id"], "amount_usd": amount})))
    db.commit()
    return {"refund_id": refund_id, "amount_usd": amount, "status": "issued", "approved_by": approved_by,
            "note": "Refunds reach the original payment method within 10 business days."}


def gated_executor(desk: SupportDesk, db):
    reads = d1.desk_executor(desk)

    def execute(name: str, tool_input: dict, ctx):
        if name != "issue_refund":
            return reads(name, tool_input, ctx)
        try:
            rma = desk.get_rma(tool_input["rma_id"])
        except ToolError as exc:
            return {"error": str(exc)}
        due = rma.get("refund_due_usd")
        if rma.get("status") != "received" or due is None:
            return reads(name, tool_input, ctx)              # the tool's own message explains the state
        approver = policy.refund_approver(due)
        if approver == "agent":
            return reads(name, tool_input, ctx)              # within the agent's limit: the tool issues it
        if not tool_input.get("_approved"):
            raise ApprovalRequired({"summary": f"Refund of ${due:,.2f} on {rma['rma_id']} needs the "
                                               f"{approver.replace('_', ' ')}",
                                    "amount_usd": due, "queue": approver, "rma_id": rma["rma_id"],
                                    "requester_email": desk.requester_email})
        decided = [a for a in ctx.store.approvals(ctx.run_id) if a["status"] == "approved"]
        approved_by = decided[-1]["decided_by"] if decided else "unknown"
        return approved_refund(db, rma, round(float(tool_input["amount_usd"]), 2), tool_input["reason"], approved_by)

    return execute


def worker(store, client, db, name: str):
    desk = SupportDesk(EMAIL, db=db, ticket_ref="T-1207")
    return d1.support_runner(store, client, execute=gated_executor(desk, db), worker=name, system=SYSTEM)


def refunds(db) -> list:
    return [dict(r) for r in db.execute("SELECT refund_id, amount_usd, approved_by, status FROM refunds")]


# ---------------------------------------------------------------------------- the SLA sweeper
def sweep_approvals(store: RunStore, *, now: dt.datetime, escalate_after: dt.timedelta, decline_after: dt.timedelta,
                    by: str = "sla-sweeper") -> list[str]:
    """Runs a cron job every few minutes: escalate approvals nobody picked up, decline the ones nobody decided."""
    actions = []
    for run in store.list(status="waiting_approval"):
        escalated = {e["approval_id"] for e in store.events(run.id, types=("approval.escalated",))}
        for a in store.approvals(run.id):
            if a["status"] != "pending":
                continue
            age = now - dt.datetime.fromisoformat(a["requested_at"])
            if age >= decline_after:
                store.decide(a["approval_id"], approved=False, by=by,
                             note=f"no decision within {decline_after.days} days; escalated to finance, customer to be "
                                  "told it is overdue")
                actions.append(f"{run.id}: declined after {age.days} days -> run back in the queue")
            elif age >= escalate_after and a["approval_id"] not in escalated:
                store.append(run.id, "approval.escalated", {"approval_id": a["approval_id"], "from": a["action"].get("queue"),
                                                            "to": "finance_director", "after_s": age.total_seconds()})
                actions.append(f"{run.id}: escalated to finance_director after {age.total_seconds() / 3600:.0f} h")
    return actions


# ---------------------------------------------------------------------------- the steps
def step_park(store, client, db):
    run = store.create("support", input={"message": MESSAGE, "requester_email": EMAIL, "ticket_id": "T-1207"},
                       run_id="sd-T-1207-approve")
    outcome = worker(store, client, db, "worker-a").run(run.id)
    record = store.get(run.id)
    print(f"worker-a: {d1.outcome_line(outcome)}")
    print(f"store: status={record.status} lease_owner={record.lease_owner} refunds={refunds(db)}")
    approval = store.approvals(run.id)[0]
    print(f"approval: status={approval['status']} action={d1.short(approval['action'], 120)}")
    before = len(mock_api().request_log)
    again = worker(store, client, db, "worker-c").run(run.id)
    print(f"\nanother worker picks it up meanwhile: {d1.outcome_line(again)} - model calls made: "
          f"{len(mock_api().request_log) - before}")
    print(wrap("The run holds no thread, no process and no lease while it waits: it is a row with "
               "status=waiting_approval and a pending approval. Any worker that finds it returns at once. It can "
               "wait an hour or a week at the same cost: nothing."))
    return run


def step_decide(store, client, db, run):
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

    outcome = worker(store, client, db, "worker-b").resume_after_decision(run.id)
    print(f"\nworker-b resumes: {d1.outcome_line(outcome)}")
    print("reply:\n" + wrap(outcome.reply))
    print(f"refunds: {refunds(db)}")
    print("log since the approval:")
    seq = [e["seq"] for e in store.events(run.id, types=("approval.requested",))][0]
    d1.print_log(store, run.id, since_seq=seq - 1)


def step_reject(store, client, db):
    run = store.create("support", input={"message": MESSAGE, "requester_email": EMAIL, "ticket_id": "T-1207"},
                       run_id="sd-T-1207-reject")
    outcome = worker(store, client, db, "worker-a").run(run.id)
    print(f"worker-a: {d1.outcome_line(outcome)}")
    store.decide(outcome.approval_id, approved=False, by="ops.manager",
                 note="the unit shows signs of installation; refund on hold pending a second inspection")
    outcome = worker(store, client, db, "worker-b").resume_after_decision(run.id)
    print(f"worker-b: {d1.outcome_line(outcome)}")
    result = store.events(run.id, types=("tool.result",))[-1]
    print(f"tool result the model saw: {d1.short(result['content'], 150)}")
    print("reply:\n" + wrap(outcome.reply))
    print(f"refunds: {refunds(db)}")


def step_sweeper(store, client, db):
    run = store.create("support", input={"message": MESSAGE, "requester_email": EMAIL, "ticket_id": "T-1207"},
                       run_id="sd-T-1207-overdue")
    worker(store, client, db, "worker-a").run(run.id)
    requested = dt.datetime.fromisoformat(store.approvals(run.id)[0]["requested_at"])
    ladder = dict(escalate_after=dt.timedelta(hours=4), decline_after=dt.timedelta(days=2))
    for label, later in (("10 minutes", dt.timedelta(minutes=10)), ("4 hours", dt.timedelta(hours=4)),
                         ("1 day", dt.timedelta(days=1)), ("2 days", dt.timedelta(days=2))):
        actions = sweep_approvals(store, now=requested + later, **ladder)
        print(f"  sweep at +{label:<10} -> {actions or ['nothing to do']}")
    print(f"  run status now: {store.get(run.id).status}")
    outcome = worker(store, client, db, "worker-b").resume_after_decision(run.id)
    print(f"worker-b: {d1.outcome_line(outcome)}")
    print("reply:\n" + wrap(outcome.reply))
    print("events:")
    d1.print_log(store, run.id, types=("approval.requested", "approval.escalated", "approval.decided", "run.status"))
    print(wrap("The ladder is policy, not runtime: the store keeps requested_at, the sweeper applies the SLA, the "
               "run resumes with a tool error that names the reason, and the customer hears 'overdue', not "
               "'refused'. A late approval after the auto-decline is a NEW decision on a new approval - the old "
               "one is closed - which is exactly the audit trail finance wants."))


def step_stop_and_ask(client):
    desk = SupportDesk(EMAIL, db=memory_db(), ticket_ref="T-1207")
    result = run_support_agent(client, MESSAGE, EMAIL, desk=desk, ticket_ref="T-1207", tools=READ_ONLY)
    print(f"in-memory loop with a read-only toolset: turns={result.turns} tools={[c['name'] for c in result.tool_calls]}")
    print("reply:\n" + wrap(result.reply))
    print(wrap("Stop-and-ask ends the conversation: the customer is told a colleague will follow up, and the "
               "manager's approval later starts a new conversation with none of this context. The durable wait "
               "keeps the run - transcript, tool results, cache prefix, thinking blocks - and resumes it. The "
               "tool_choice variants (force a 'request_approval' tool, or tool_choice none while waiting) shape "
               "what the model may do next; they do not park anything. Managed Agents sessions have the same wait "
               "built in for hosted tools: an always_ask permission policy idles the session with "
               "stop_reason requires_action until a user.tool_confirmation arrives (Day 4)."))


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

    step(2, "Decided from another process, resumed by another worker")
    step_decide(store, client, db, run)

    step(3, "The rejected path")
    step_reject(store, client, memory_db())

    step(4, "Nobody decides: escalate, then decline, on a schedule")
    step_sweeper(store, client, memory_db())

    step(5, "The alternative: stop and ask (read-only toolset)")
    step_stop_and_ask(client)


if __name__ == "__main__":
    main()
