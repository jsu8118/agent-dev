"""Lab 07 - Human in the loop: risk-classified tools and approval gates.

Objective
    Give a customer-facing agent WRITE tools with different risk classes and put a human approval gate
    in front of every write: the loop pauses, shows a reviewer exactly what will happen (a dry-run
    preview), and executes only what was approved.  Show both paths: an approved change (the agent
    confirms it) and a declined one (the tool_result says a person declined; the agent adapts instead of
    retrying or pretending).

Concepts
    Side-effect classes (read / reversible write / irreversible write); approval callbacks; dry-run
    previews; "approve exactly what you saw"; declined -> is_error result that tells the model what to do;
    identity from the channel; audit trail of approvals and actions.

Run
    python day2_tools_agent_loop/labs/07_human_in_the_loop.py                # automatic reviewer policy (tests)
    python day2_tools_agent_loop/labs/07_human_in_the_loop.py --interactive  # YOU approve or decline each write

What to observe
    * Reads run immediately; every write stops at the gate with a preview of its effect.
    * The approved address change is executed with exactly the approved arguments and confirmed.
    * The declined cancellation never touches the database; the model explains the next step and does
      not retry.  Both decisions are in the audit log, next to the actions.
"""

# test: expect=APPROVED
# test: expect=DECLINED
# test: expect=audit_log

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from labkit import MODEL, get_client, header, step, text_of, wrap
from labkit.data import scratch_db

SYSTEM = """<day2_hitl>
You are the customer-support assistant of Kestrel Pumps & Controls, answering customer emails. Today is 2026-09-15.
- Look the order up before acting on it.
- Some actions need a person's approval. If a tool result says a reviewer declined, do not retry and do not \
claim the action happened: explain the outcome and the next step.
- Reply to the customer in 3-6 plain sentences.
"""


class Risk(Enum):
    READ = "read"
    REVERSIBLE = "reversible write"
    IRREVERSIBLE = "irreversible write"


@dataclass(frozen=True)
class ApprovalRequest:
    tool: str
    args: dict
    risk: Risk
    preview: dict                  # what WOULD change - computed by code, not described by the model
    ticket: str


@dataclass(frozen=True)
class Decision:
    approved: bool
    reviewer: str
    reason: str


class ToolError(Exception):
    pass


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}}


TOOLS = [
    _tool("get_order_status", "Look up one of the requester's orders: status, promised date, lines, total and the "
          "account manager. Use it before acting on any order.",
          {"order_id": {"type": "string", "description": "Order ID, e.g. SO-10234"}}, ["order_id"]),
    _tool("update_delivery_address", "WRITE (reversible until shipment; needs approval). Change the delivery address "
          "of an order that has not shipped yet.",
          {"order_id": {"type": "string"},
           "new_address": {"type": "string", "description": "Full new delivery address as given by the customer"}},
          ["order_id", "new_address"]),
    _tool("cancel_order", "WRITE (IRREVERSIBLE; needs approval). Cancel an order that has not shipped. Built-to-order "
          "products already in production may carry cancellation charges.",
          {"order_id": {"type": "string"},
           "reason": {"type": "string", "description": "The customer's reason, in a few words"}},
          ["order_id", "reason"]),
]
RISK = {"get_order_status": Risk.READ, "update_delivery_address": Risk.REVERSIBLE, "cancel_order": Risk.IRREVERSIBLE}


class HitlDesk:
    """Tools bound to one requester (identity from the email channel) and a scratch copy of the ERP."""

    def __init__(self, requester_email: str, db, ticket: str) -> None:
        self.requester_email, self.db, self.ticket = requester_email.lower(), db, ticket
        self.db.execute("CREATE TABLE IF NOT EXISTS delivery_address_changes (change_id TEXT PRIMARY KEY, "
                        "order_id TEXT, new_address TEXT, approved_by TEXT, created_at TEXT)")

    def _order(self, order_id: str) -> dict:
        row = self.db.execute("SELECT o.*, c.name AS customer, c.email_domain, c.account_manager FROM orders o "
                              "JOIN customers c USING (customer_id) WHERE order_id = ?",
                              (order_id.strip().upper(),)).fetchone()
        if row is None:
            raise ToolError(f"Order {order_id} not found. Ask the customer to check the number (SO-12345).")
        if row["email_domain"] != self.requester_email.rsplit("@", 1)[-1]:
            raise ToolError("This order does not belong to the sender's account. Do not disclose or change it.")
        return dict(row)

    def audit(self, action: str, target: str, details: dict) -> None:
        self.db.execute("INSERT INTO audit_log (ts, actor, action, target, details) VALUES (?,?,?,?,?)",
                        ("2026-09-15T09:00:00Z", f"support-agent:{self.ticket}", action, target,
                         json.dumps(details, default=str)[:500]))
        self.db.commit()

    # ------------------------------------------------------------------ read
    def get_order_status(self, order_id: str) -> dict:
        o = self._order(order_id)
        lines = [dict(r) for r in self.db.execute("SELECT sku, qty FROM order_lines WHERE order_id = ?",
                                                  (o["order_id"],))]
        return {"order_id": o["order_id"], "status": o["status"], "promised_date": o["promised_date"],
                "total_usd": o["total_usd"], "lines": lines, "account_manager": o["account_manager"]}

    # ------------------------------------------------------------------ dry runs (shown to the reviewer)
    def preview(self, tool: str, args: dict) -> dict:
        o = self._order(args["order_id"])
        shipped = o["status"] in ("shipped", "delivered", "cancelled")
        if tool == "update_delivery_address":
            return {"order": o["order_id"], "customer": o["customer"], "status": o["status"],
                    "change": f"delivery address -> {args['new_address']}", "precondition_ok": not shipped,
                    "undo": "possible until the order ships"}
        return {"order": o["order_id"], "customer": o["customer"], "status": o["status"],
                "order_value_usd": o["total_usd"], "change": "status -> cancelled",
                "precondition_ok": not shipped, "undo": "NOT possible (production slot and material released)"}

    # ------------------------------------------------------------------ writes (run only after approval)
    def update_delivery_address(self, order_id: str, new_address: str, approved_by: str) -> dict:
        o = self._order(order_id)
        if o["status"] in ("shipped", "delivered", "cancelled"):
            raise ToolError(f"{o['order_id']} is {o['status']}: the address can no longer be changed. Suggest a "
                            "carrier redirect instead.")
        existing = self.db.execute("SELECT change_id FROM delivery_address_changes WHERE order_id = ? AND "
                                   "new_address = ?", (o["order_id"], new_address)).fetchone()
        if existing:                                           # idempotent: same request -> same change
            return {"change_id": existing[0], "order_id": o["order_id"], "new_address": new_address,
                    "existing": True}
        n = self.db.execute("SELECT COUNT(*) FROM delivery_address_changes").fetchone()[0]
        change_id = f"ADR-{8001 + n}"
        self.db.execute("INSERT INTO delivery_address_changes VALUES (?,?,?,?,?)",
                        (change_id, o["order_id"], new_address, approved_by, dt.date(2026, 9, 15).isoformat()))
        self.audit("update_delivery_address", o["order_id"], {"change_id": change_id, "approved_by": approved_by})
        return {"change_id": change_id, "order_id": o["order_id"], "new_address": new_address, "existing": False}

    def cancel_order(self, order_id: str, reason: str, approved_by: str) -> dict:
        o = self._order(order_id)
        if o["status"] in ("shipped", "delivered", "cancelled"):
            raise ToolError(f"{o['order_id']} is {o['status']} and cannot be cancelled here.")
        self.db.execute("UPDATE orders SET status = 'cancelled', notes = ? WHERE order_id = ?",
                        (f"Cancelled by customer request: {reason[:200]}", o["order_id"]))
        self.audit("cancel_order", o["order_id"], {"approved_by": approved_by, "reason": reason[:200]})
        return {"cancellation_id": f"CXL-{o['order_id'][3:]}", "order_id": o["order_id"], "status": "cancelled",
                "note": "The customer receives a cancellation confirmation by email."}


# ---------------------------------------------------------------------------------- approvers
def auto_policy(request: ApprovalRequest) -> Decision:
    """Non-interactive stand-in for the human reviewer (used in tests and batch runs)."""
    if not request.preview.get("precondition_ok"):
        return Decision(False, "auto-policy", f"precondition failed (order is {request.preview.get('status')})")
    if request.risk is Risk.REVERSIBLE:
        return Decision(True, "auto-policy", "reversible change on an unshipped order; logged for spot checks")
    status = str(request.preview.get("status", "?")).replace("_", " ")
    return Decision(False, "auto-policy", f"irreversible action on an order that is {status} (value "
                                          f"${request.preview.get('order_value_usd', 0):,.2f}) needs the account "
                                          "manager's review - cancellation charges may apply")


def ask_human(request: ApprovalRequest) -> Decision:
    print(f"\n  >>> APPROVAL NEEDED [{request.risk.value}] {request.tool}({json.dumps(request.args)})")
    for key, value in request.preview.items():
        print(f"      {key:<16} {value}")
    answer = input("      Approve? [y/N] ").strip().lower()
    if answer.startswith("y"):
        return Decision(True, "human reviewer (you)", "approved at the console")
    reason = input("      Reason for declining (shown to the agent): ").strip() or "declined by the reviewer"
    return Decision(False, "human reviewer (you)", reason)


# ---------------------------------------------------------------------------------- the gated loop
def run_ticket(client, email: str, sender: str, desk: HitlDesk,
               approver: Callable[[ApprovalRequest], Decision], max_turns: int = 8) -> str:
    # Minimal loop (tools run only on stop_reason == "tool_use"; see lab 02 for the full stop-reason policy).
    # What matters here is gate_and_run(): every write passes the approval gate before it executes.
    messages: list[dict] = [{"role": "user", "content": email}]
    for _ in range(max_turns):
        response = client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM, tools=TOOLS,
                                          messages=messages)
        if response.stop_reason == "refusal":
            return "(declined by the model - route to a human)"
        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            return text_of(response)
        results = []
        for call in calls:
            content, is_error = gate_and_run(desk, call.name, dict(call.input), approver)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "(max turns reached - handed to a human)"


def gate_and_run(desk: HitlDesk, name: str, args: dict,
                 approver: Callable[[ApprovalRequest], Decision]) -> tuple[str, bool]:
    risk = RISK.get(name)
    try:
        if risk is None:
            raise ToolError(f"Unknown tool {name!r}.")
        if risk is Risk.READ:
            print(f"  [read]   {name}({json.dumps(args)}) -> runs without approval")
            return json.dumps(desk.get_order_status(**args)), False
        preview = desk.preview(name, args)               # validates the call before bothering a human
        decision = approver(ApprovalRequest(name, dict(args), risk, preview, desk.ticket))
        desk.audit("approval", name, {"approved": decision.approved, "reviewer": decision.reviewer,
                                      "reason": decision.reason, "args": args})
        verdict = "APPROVED" if decision.approved else "DECLINED"
        print(f"  [{risk.value}] {name}({json.dumps(args)})\n      preview: {preview}\n"
              f"      -> {verdict} by {decision.reviewer}: {decision.reason}")
        if not decision.approved:
            return json.dumps({"error": f"DECLINED by {decision.reviewer}: {decision.reason}. The action was NOT "
                                        "performed. Do not retry it; tell the customer what happens next."}), True
        # Execute EXACTLY the arguments the reviewer saw (args was copied before the preview).
        result = getattr(desk, name)(**args, approved_by=decision.reviewer)
        return json.dumps(result), False
    except ToolError as exc:
        return json.dumps({"error": str(exc)}), True


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-in-the-loop approval gates")
    parser.add_argument("--interactive", action="store_true", help="approve or decline each write yourself")
    args = parser.parse_args()
    approver = ask_human if args.interactive else auto_policy

    client = get_client()
    header(f"Lab 07 - human in the loop ({MODEL}; reviewer: {'you' if args.interactive else 'automatic policy'})")
    db = scratch_db("day2_lab07.db")

    step(1, "Risk classes and the approval rule")
    for tool in TOOLS:
        risk = RISK[tool["name"]]
        rule = "runs immediately" if risk is Risk.READ else "stops at the gate: preview -> approve/decline -> audit"
        print(f"  {tool['name']:<26} {risk.value:<20} {rule}")

    cases = [
        ("T-1005", "derek.walsh@vanguardfire.example",
         "Subject: Change delivery address - SO-10312\n\nWe need to change the delivery address for SO-10312 to our "
         "new warehouse: 4410 Industrial Pkwy, Unit 7, Rockport. It hasn't shipped yet, right?\n\nDerek Walsh"),
        ("T-LAB7", "ingrid.solberg@polarcold.example",
         "Subject: Cancel SO-10285\n\nPlease cancel our KP-400 order SO-10285 - the cold-store extension has been "
         "postponed indefinitely. Please confirm the cancellation.\n\nIngrid Solberg, Polar Cold Storage"),
    ]
    for number, (ticket, sender, email) in enumerate(cases, start=2):
        step(number, f"Ticket {ticket} from {sender}")
        print(wrap(email.split("\n\n", 1)[1].split("\n\n")[0]))
        reply = run_ticket(client, email, sender, HitlDesk(sender, db, ticket), approver)
        print("Reply to the customer:\n" + wrap(reply, indent="  | "))

    step(len(cases) + 2, "What actually happened in the database")
    status = db.execute("SELECT status FROM orders WHERE order_id = 'SO-10285'").fetchone()[0]
    changes = [dict(r) for r in db.execute("SELECT * FROM delivery_address_changes")]
    print(f"  SO-10285 status: {status}   delivery_address_changes: {changes}")
    print("  audit_log:")
    for row in db.execute("SELECT actor, action, target, details FROM audit_log ORDER BY id"):
        print(f"    {row['actor']:<22} {row['action']:<24} {row['target']:<24} {row['details'][:90]}")


if __name__ == "__main__":
    main()
