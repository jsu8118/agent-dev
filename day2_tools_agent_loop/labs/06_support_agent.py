"""Lab 06 - Kestrel's reference support agent, ticket by ticket.

Objective
    Run the production-grade agent this day builds towards (kestrel.support_agent.run_support_agent with
    the kestrel.support_tools toolset) over real tickets from data/support/tickets.jsonl, and read each
    trajectory: which tools it called, which calls the TOOLS refused and why, when it escalated, what it
    replied, and what each ticket cost against Kestrel's $0.40/ticket target.

Concepts
    A complete tool surface (11 tools, 3 side-effect classes); identity from the channel; policy enforced
    in code (refund limits, return eligibility); errors that instruct; idempotent writes; escalation as a
    tool; audit trail; tracing; cost per ticket.

Run
    python day2_tools_agent_loop/labs/06_support_agent.py            # 7 representative tickets
    python day2_tools_agent_loop/labs/06_support_agent.py --all      # all 62 tickets (live: costs ~$1-3)

What to observe
    * T-1207: the model TRIES issue_refund; the tool refuses ($9,188.50 > the agent's $2,500 limit) with an
      error that says what to do next; the model escalates to the support manager instead.
    * T-1006: the sender's address is not on the account, so account tools refuse to disclose anything.
    * T-1801 / T-1507: safety and prompt-injection tickets go straight to humans (P1 field service, security).
    * Every write lands in the audit_log of the scratch database (who/what/when/ticket).
"""

# test: expect=Trajectory
# test: expect=audit_log

from __future__ import annotations

import argparse
import json

from kestrel.support_agent import run_support_agent
from kestrel.support_tools import TOOLS, SupportDesk
from labkit import MODEL, get_client, header, step, wrap
from labkit.data import load_jsonl, scratch_db
from labkit.tracing import Tracer

DEFAULT_TICKETS = ["T-1001", "T-1101", "T-1201", "T-1207", "T-1006", "T-1801", "T-1507"]
COST_TARGET = 0.40

SIDE_EFFECTS = {
    "get_customer_profile": "read", "get_order": "read", "list_customer_orders": "read", "get_invoice": "read",
    "get_rma": "read", "search_knowledge_base": "read",
    "check_return_eligibility": "read (policy check)", "check_warranty": "read (policy check)",
    "create_rma": "write, reversible + idempotent", "issue_refund": "write, IRREVERSIBLE (limit enforced in code)",
    "escalate_to_human": "write (handover to a person)",
}


def toolset_at_a_glance() -> None:
    print(f"{'tool':<26} {'side effects':<44} arguments (required*)")
    for t in TOOLS:
        schema = t["input_schema"]
        args = ", ".join(f"{name}{'*' if name in schema['required'] else ''}" for name in schema["properties"])
        print(f"{t['name']:<26} {SIDE_EFFECTS.get(t['name'], '?'):<44} {args or '(none - identity comes from the channel)'}")


def short(value: object, limit: int = 110) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def run_ticket(client, ticket: dict, label: dict, db) -> dict:
    trajectory: list[str] = []

    def on_tool(name: str, tool_input: dict, content: str, is_error: bool) -> None:
        outcome = "ERROR " + short(json.loads(content).get("error", content), 150) if is_error else "ok " + short(content, 90)
        trajectory.append(f"  -> {name}({short(tool_input, 80)})\n       {outcome}")

    tracer = Tracer("support-agent")
    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    message = f"Subject: {ticket['subject']}\n\n{ticket['body']}"
    result = run_support_agent(client, message, ticket["from_email"], desk=desk, tracer=tracer,
                               ticket_ref=ticket["ticket_id"], on_tool=on_tool)
    totals = tracer.totals()
    print(f"\n[{ticket['ticket_id']}] {ticket['subject']}  (from {ticket['from_email']}; labelled "
          f"{label['category']}/{label['priority']})")
    print("Trajectory:")
    print("\n".join(trajectory) if trajectory else "  (no tool calls)")
    print("Reply sent to the customer:")
    print(wrap(result.reply, indent="  | "))
    print(f"  turns={result.turns} tool_calls={len(result.tool_calls)} escalated={result.escalated} "
          f"input_tokens={result.input_tokens:,} output_tokens={result.output_tokens:,} cost=${totals['cost_usd']:.4f}")
    return {"ticket": ticket["ticket_id"], "category": label["category"], "turns": result.turns,
            "tools": len(result.tool_calls), "errors": sum(1 for c in result.tool_calls if c["is_error"]),
            "escalated": result.escalated, "cost": totals["cost_usd"], "tracer": tracer}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="run all 62 tickets instead of 7")
    args = parser.parse_args()

    client = get_client()
    header(f"Lab 06 - the reference support agent ({MODEL})")
    tickets = {t["ticket_id"]: t for t in load_jsonl("support", "tickets.jsonl")}
    labels = {t["ticket_id"]: t for t in load_jsonl("support", "ticket_labels.jsonl")}
    chosen = list(tickets) if args.all else DEFAULT_TICKETS
    db = scratch_db("day2_lab06.db")          # a fresh writable copy: RMAs, refunds and escalations land here

    step(1, "The toolset at a glance (kestrel/support_tools.py)")
    toolset_at_a_glance()

    step(2, f"Run the agent on {len(chosen)} tickets")
    rows = [run_ticket(client, tickets[tid], labels[tid], db) for tid in chosen]

    step(3, "Summary")
    print(f"{'ticket':<8} {'category':<18} {'turns':>5} {'tools':>5} {'tool errors':>11} {'escalated':>9} {'cost':>8}")
    for r in rows:
        print(f"{r['ticket']:<8} {r['category']:<18} {r['turns']:>5} {r['tools']:>5} {r['errors']:>11} "
              f"{str(r['escalated']):>9} ${r['cost']:>7.4f}")
    average = sum(r["cost"] for r in rows) / len(rows)
    print(f"average cost per ticket: ${average:.4f} (target < ${COST_TARGET:.2f}: "
          f"{'met' if average < COST_TARGET else 'NOT met'}); escalated {sum(r['escalated'] for r in rows)}/{len(rows)}")

    step(4, "The audit trail the tools wrote (audit_log in the scratch DB)")
    for row in db.execute("SELECT actor, action, target, details FROM audit_log ORDER BY id"):
        print(f"  {row['actor']:<28} {row['action']:<18} {row['target']:<10} {short(row['details'], 70)}")

    step(5, "One trace as a tree (Day 6 builds observability on this)")
    refused = next((r for r in rows if r["errors"]), rows[0])
    print(refused["tracer"].render_tree())
    print("in= counts only UNCACHED input tokens. The reference agent caches its system prompt and the growing "
          "conversation (automatic caching), so almost all of its input is billed as cache reads - see cache_r in the "
          "usage summary below. Day 3 explains the mechanics.")


if __name__ == "__main__":
    main()
