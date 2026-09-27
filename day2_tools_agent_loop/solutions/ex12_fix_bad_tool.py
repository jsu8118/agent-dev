"""Solution to exercise 12 - critique and fix a badly designed tool definition.

The starter (exercises/bad_tool_definition.py) exposes one generic `db` tool with a free-text `mode`, an
identity argument chosen by the model, raw Python-repr results, "ERROR" strings and an unguarded refund.
This script:
  1. lints both designs with automated checks (the same ones you can put in CI),
  2. measures the definition and result sizes with count_tokens,
  3. runs the fixed toolset end to end.
The full critique (15 problems and why each matters) is in solutions/README.md.

Run
    python day2_tools_agent_loop/solutions/ex12_fix_bad_tool.py
"""

# test: expect=problems found
# test: expect=Fixed toolset

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "labs"))
sys.path.insert(0, str(HERE / "exercises"))

from _order_desk import GET_INVOICE, GET_ORDER_STATUS, INVOICE_ID, ORDER_ID, OrderDesk, ToolError, _tool  # noqa: E402,E501
from bad_tool_definition import BAD_TOOL, db  # noqa: E402
from labkit import MODEL, get_client, header, step, wrap  # noqa: E402

lab02 = importlib.import_module("02_agent_loop_from_scratch")

SYSTEM = """<day2_order_desk>
You are the customer-support assistant of Kestrel Pumps & Controls, answering the customer who sent this \
email. Look facts up with the tools; never guess. Today is 2026-09-15. Reply in 2-5 plain sentences."""

LIST_MY_ORDERS = _tool(
    "list_my_orders",
    "List the requester's own recent orders (newest first) with status and total. Use it when the customer asks "
    "about 'our orders' without giving an order ID. The customer is identified from the email channel, so this "
    "tool takes no customer argument.",
    {"status": {"type": "string", "enum": ["pending", "confirmed", "in_production", "shipped", "delivered",
                                           "cancelled", "on_hold"], "description": "Optional status filter"},
     "limit": {"type": "integer", "description": "Maximum orders to return (1-10, default 5)"}},
    [])
FIXED_TOOLS = [GET_ORDER_STATUS, GET_INVOICE, LIST_MY_ORDERS]      # refunds: NOT here (see README)


class CustomerDesk(OrderDesk):
    """The fix, implemented: identity comes from the channel, results are curated, errors instruct."""

    tool_names = frozenset({"get_order_status", "get_invoice", "list_my_orders"})

    def __init__(self, requester_email: str, **kwargs) -> None:
        super().__init__(**kwargs)
        domain = requester_email.rsplit("@", 1)[-1].lower()
        row = self._query("SELECT customer_id FROM customers WHERE email_domain = ?", (domain,))
        self.customer_id = row[0]["customer_id"] if row else None

    def _check_owner(self, sql: str, key: str) -> None:
        """Check ownership BEFORE reading details, with ONE message for 'missing' and 'not yours'.

        Different messages would let anyone probe which order numbers exist (an enumeration leak).
        """
        owner = self._query(sql, (key,))
        if self.customer_id is None or not owner or owner[0]["customer_id"] != self.customer_id:
            raise ToolError(f"No {key} on the sender's account. Do not disclose anything about it; ask the customer "
                            "to write from their company email address or to check the number.")

    def get_order_status(self, order_id: str) -> dict:
        oid = self._normalise(order_id)
        if ORDER_ID.match(oid):                         # malformed IDs get the base class's format error
            self._check_owner("SELECT customer_id FROM orders WHERE order_id = ?", oid)
        return super().get_order_status(oid)

    def get_invoice(self, invoice_id: str) -> dict:
        iid = self._normalise(invoice_id)
        if INVOICE_ID.match(iid):
            self._check_owner("SELECT customer_id FROM invoices WHERE invoice_id = ?", iid)
        return super().get_invoice(iid)

    def list_my_orders(self, status: str | None = None, limit: int = 5) -> dict:
        if self.customer_id is None:
            raise ToolError("The sender is not a verified customer. Ask for an order ID and its PO number.")
        sql, args = "SELECT order_id, order_date, status, total_usd FROM orders WHERE customer_id = ?", [self.customer_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY order_date DESC LIMIT ?"
        args.append(max(1, min(int(limit), 10)))
        return {"orders": self._query(sql, tuple(args)), "note": "Newest first; ask for an order ID for details."}


IDENTITY_PARAM = re.compile(r"^(cust|customer(_id)?|user(_id)?|email|account(_id)?|requester)$", re.I)
WRITE_WORDS = re.compile(r"(?i)\b(refund|delete|cancel|update|create|issue|pay)\b")


def lint(tool: dict) -> list[str]:
    """Automatable checks for a tool definition - run them in CI next to your unit tests."""
    problems = []
    schema, name, desc = tool.get("input_schema", {}), tool.get("name", ""), tool.get("description", "")
    props = schema.get("properties", {})
    if not re.fullmatch(r"[a-z]+(_[a-z]+)+", name):
        problems.append(f"name '{name}' is not a verb_noun name that says what the tool does")
    if len(desc) < 80:
        problems.append(f"description is {len(desc)} chars: too short to say what it returns and WHEN to use it")
    if not re.search(r"(?i)\b(use|call|when|before|after|only)\b", desc):
        problems.append("description gives no trigger (when should the model call it?)")
    if not tool.get("strict"):
        problems.append("not strict: arguments are not guaranteed to match the schema")
    if schema.get("additionalProperties") is not False:
        problems.append("schema is open (additionalProperties is not false)")
    if props and "required" not in schema:
        problems.append("no `required` list: every argument is optional, including the ones the code needs")
    for pname, pschema in props.items():
        if len(pname) <= 3:
            problems.append(f"argument '{pname}' is an abbreviation the model must guess the meaning of")
        if "description" not in pschema and "enum" not in pschema:
            problems.append(f"argument '{pname}' has no description")
        if IDENTITY_PARAM.match(pname) or "person asking" in pschema.get("description", ""):
            problems.append(f"argument '{pname}' lets the MODEL choose whose data to read (identity must come from "
                            "the channel)")
        if pschema.get("type") == "string" and " or " in pschema.get("description", "") and "enum" not in pschema:
            problems.append(f"argument '{pname}' is a closed set written as free text: use an enum")
        if pschema.get("type") == "boolean" and re.search(r"all|verbose|raw", pname):
            problems.append(f"argument '{pname}' lets the model ask for raw dumps")
    if WRITE_WORDS.search(json.dumps(props)) and re.search(r"(?i)order|invoice", json.dumps(props)):
        problems.append("reads and an irreversible write share one tool: the write cannot be gated, audited or "
                        "rate-limited separately")
    return problems


def main() -> None:
    client = get_client()
    header("Exercise 12 - fixing a badly designed tool")

    step(1, "Automated lint: bad vs fixed")
    bad_problems = lint(BAD_TOOL)
    print(f"`{BAD_TOOL['name']}`: {len(bad_problems)} problems found by the linter")
    for problem in bad_problems:
        print(f"  - {problem}")
    for tool in FIXED_TOOLS:
        issues = lint(tool)
        print(f"`{tool['name']}`: {len(issues)} problems" + (": " + "; ".join(issues) if issues else ""))
    print("(The linter cannot see implementation problems: Python-repr results, 'ERROR' strings, unbounded lists, "
          "no idempotency - the README covers those.)")

    step(2, "What the designs cost in tokens")
    question = [{"role": "user", "content": "Where is SO-10303?"}]
    bare = client.messages.count_tokens(model=MODEL, messages=question).input_tokens
    bad_def = client.messages.count_tokens(model=MODEL, messages=question, tools=[BAD_TOOL]).input_tokens - bare
    fixed_def = client.messages.count_tokens(model=MODEL, messages=question, tools=FIXED_TOOLS).input_tokens - bare
    print(f"definitions: bad = {bad_def:,} tokens (1 vague tool); fixed = {fixed_def:,} tokens (3 documented tools) - "
          "clarity costs a few hundred tokens once per request, and caching makes them cheap")
    raw = db("SO-10303")
    curated, _ = CustomerDesk("jorge.medina@greenvalley-coop.example").run("get_order_status", {"order_id": "SO-10303"})
    everything = db(cust="C-1005", mode="orders_for_customer")
    print(f"one order: bad = {len(raw):,} chars of repr, fixed = {len(curated):,} chars of JSON; "
          f"'all orders for a customer': bad = {len(everything):,} chars, fixed = at most 10 rows")

    step(3, "Fixed toolset, end to end")
    for sender, text in [("jorge.medina@greenvalley-coop.example", "Where is SO-10303?"),
                         ("jorge.medina@greenvalley-coop.example", "Where is SO-10300?")]:
        desk = CustomerDesk(sender)
        run = lab02.run_agent(client, text, desk, tools=FIXED_TOOLS, system=SYSTEM)
        calls = ", ".join(f"{c['name']}{'(is_error)' if c['is_error'] else ''}" for c in desk.calls)
        print(f"{sender}: {text!r} -> tools: {calls}")
        print(wrap(run.answer))
    print("SO-10300 belongs to another customer: the tool refused BEFORE reading it, with the same message a "
          "non-existent order gets, and the model disclosed nothing.")


if __name__ == "__main__":
    main()
