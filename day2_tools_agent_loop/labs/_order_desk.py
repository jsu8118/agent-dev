"""The Day 2 "order desk": a small, read-mostly toolset over Kestrel's ops DB, shared by labs 02-05 and 08.

The order desk is an INTERNAL assistant for Kestrel's support staff (they are signed in via SSO, so the
tools do not verify customer identity - lab 06 moves to the customer-facing agent, where identity must
come from the email channel).  The module is deliberately small so you can read every design decision:

* One Python method per tool, plus a JSON-schema definition the model sees (TOOLS).  The two are kept
  side by side on purpose: the schema is the model's documentation of your function.
* Results are CURATED JSON: only the fields an agent needs to answer, never raw table rows (lab 05
  measures what raw rows would cost).
* Expected failures raise ToolError with a message written FOR THE MODEL: what went wrong and what to do
  next ("that is an invoice ID - use get_invoice").  `run()` turns them into is_error tool results.
* Read tools are parallel-safe (a lock serialises the shared SQLite connection, not the whole call).
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from kestrel.kb import KnowledgeBase
from labkit.data import ops_db

SYSTEM_PROMPT = """<day2_order_desk>
You are the order-desk assistant of Kestrel Pumps & Controls. Kestrel support staff ask you about customer \
orders, shipments, invoices and company policy. Today is 2026-09-15.
- Look facts up with the tools; never guess a status, date, amount or ID. For several independent look-ups, \
call the tools in parallel.
- If a tool returns an error, read it and follow its advice (switch tools, or ask for a corrected ID). Never \
invent a replacement ID.
- Answer in 2-6 plain sentences for the staff member: facts first (IDs, dates, amounts), then what to tell the \
customer.
"""

ORDER_ID = re.compile(r"^SO-\d{5}$")
INVOICE_ID = re.compile(r"^AR-\d{5}$")
RMA_ID = re.compile(r"^RMA-\d{4}$")


class ToolError(Exception):
    """An expected failure. The message is written for the model: what happened and what to do next."""


class OrderDesk:
    """Tool implementations bound to one database connection (read-only by default).

    `tool_names` whitelists the methods the model may call (set below TOOLS); a subclass that adds a tool
    extends it - never dispatch to arbitrary attributes by name.
    """

    tool_names: frozenset[str] = frozenset()

    def __init__(self, db=None, *, latency_s: float = 0.0) -> None:
        self.db = db if db is not None else ops_db()
        self.latency_s = latency_s          # lab 03: simulated ERP round trip per call, to show parallelism
        self.calls: list[dict] = []          # a record of every call (name, input, is_error) - your audit trail
        self.cases: dict[str, dict] = {}     # the "logistics case system" (in memory: lab 04's write tool)
        self._lock = threading.Lock()
        self._kb: KnowledgeBase | None = None

    # -------------------------------------------------------------------------------- helpers
    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    @staticmethod
    def _normalise(value: str) -> str:
        return (value or "").strip().upper()

    # -------------------------------------------------------------------------------- read tools
    def get_order_status(self, order_id: str) -> dict:
        oid = self._normalise(order_id)
        if INVOICE_ID.match(oid):
            raise ToolError(f"'{oid}' is an invoice ID, not an order ID. Use get_invoice for invoices - its result "
                            "includes the order ID.")
        if RMA_ID.match(oid):
            raise ToolError(f"'{oid}' is an RMA (return) number, not an order ID. RMA look-ups are not available "
                            "here; ask for the order number (SO-12345).")
        if not ORDER_ID.match(oid):
            raise ToolError(f"'{order_id}' is not a valid order ID. Kestrel order IDs look like SO-10234 "
                            "(SO, a hyphen, five digits). Ask the user for the correct ID; do not guess.")
        rows = self._query("SELECT o.*, c.name AS customer, c.account_manager FROM orders o "
                           "JOIN customers c USING (customer_id) WHERE order_id = ?", (oid,))
        if not rows:
            raise ToolError(f"Order {oid} not found. Do not guess another number: ask the customer to double-check "
                            "it (format SO-12345).")
        order = rows[0]
        lines = self._query("SELECT l.sku, p.name, l.qty FROM order_lines l JOIN products p USING (sku) "
                            "WHERE order_id = ? ORDER BY line_no", (oid,))
        ship = self._query("SELECT carrier, tracking_number, ship_date, eta_date, delivered_date, status, "
                           "exception_reason FROM shipments WHERE order_id = ?", (oid,))
        invoice = self._query("SELECT invoice_id FROM invoices WHERE order_id = ?", (oid,))
        return {
            "order_id": oid, "customer": order["customer"], "status": order["status"],
            "order_date": order["order_date"], "promised_date": order["promised_date"],
            "lines": lines,
            # Omit empty fields rather than sending nulls: fewer tokens, nothing for the model to misread.
            "shipment": {k: v for k, v in ship[0].items() if v is not None} if ship else None,
            "invoice_id": invoice[0]["invoice_id"] if invoice else None,
            "account_manager": order["account_manager"],
        }

    def get_invoice(self, invoice_id: str) -> dict:
        iid = self._normalise(invoice_id)
        if ORDER_ID.match(iid):
            raise ToolError(f"'{iid}' is an order ID. Use get_order_status - its result includes the invoice ID.")
        if not INVOICE_ID.match(iid):
            raise ToolError(f"'{invoice_id}' is not a valid invoice ID. Kestrel invoice IDs look like AR-90123.")
        rows = self._query("SELECT i.*, c.name AS customer FROM invoices i JOIN customers c USING (customer_id) "
                           "WHERE invoice_id = ?", (iid,))
        if not rows:
            raise ToolError(f"Invoice {iid} not found. Ask for the invoice number exactly as printed (AR-12345).")
        inv = rows[0]
        amount, paid = inv["amount_usd"], inv["paid_amount_usd"]
        return {"invoice_id": iid, "order_id": inv["order_id"], "customer": inv["customer"],
                "issue_date": inv["issue_date"], "due_date": inv["due_date"], "amount_usd": amount,
                "paid_amount_usd": paid, "balance_usd": round(max(amount - paid, 0.0), 2),
                "overpaid_usd": round(max(paid - amount, 0.0), 2), "status": inv["status"]}

    def search_policies(self, query: str, top_k: int = 3) -> dict:
        if self._kb is None:
            self._kb = KnowledgeBase(sources=("company/policies",))
        hits = self._kb.search(query, top_k=max(1, min(int(top_k), 5)))
        if not hits:
            return {"results": [], "note": "No matching policy text. Try other keywords (e.g. 'refund approval')."}
        # Passages are cut at 700 characters: enough for one policy section, not a whole document.
        return {"results": [{"citation": chunk.cite(), "text": chunk.text[:700]} for _, chunk in hits]}

    # -------------------------------------------------------------------------------- write tool (lab 04)
    def open_logistics_case(self, order_id: str, summary: str) -> dict:
        oid = self._normalise(order_id)
        self.get_order_status(oid)                       # validates the ID (raises ToolError if wrong)
        existing = next((c for c in self.cases.values() if c["order_id"] == oid and c["status"] == "open"), None)
        if existing:                                     # idempotent: a retry must not open a second case
            return {**existing, "existing": True}
        case = {"case_id": f"LOG-{5001 + len(self.cases)}", "order_id": oid, "status": "open",
                "summary": summary[:300], "sla": "logistics replies within 4 business hours"}
        self.cases[case["case_id"]] = case
        return {**case, "existing": False}

    # -------------------------------------------------------------------------------- dispatcher
    def run(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Execute one tool call. Returns (JSON text for the tool_result, is_error)."""
        handler = getattr(self, name, None) if name in self.tool_names else None
        if self.latency_s:
            time.sleep(self.latency_s)       # the network round trip of a real ERP API (no lock held)
        try:
            if handler is None:
                raise ToolError(f"Unknown tool {name!r}. Available tools: {', '.join(sorted(self.tool_names))}.")
            content, is_error = json.dumps(handler(**(tool_input or {})), default=str), False
        except ToolError as exc:
            content, is_error = json.dumps({"error": str(exc)}), True
        except TypeError as exc:                         # wrong/missing arguments (only possible without strict)
            content, is_error = json.dumps({"error": f"Invalid arguments for {name}: {exc}"}), True
        self.calls.append({"name": name, "input": tool_input, "is_error": is_error})
        return content, is_error


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """A strict tool definition: the API guarantees the arguments validate against this schema."""
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}}


GET_ORDER_STATUS = _tool(
    "get_order_status",
    "Look up ONE Kestrel sales order: status, promised date, order lines, shipment (carrier, tracking number, "
    "ship date, ETA, delivered date, carrier exceptions) and its invoice ID. Use it whenever a question is about "
    "where an order is, when it ships or arrives, or what was ordered. For several orders, call it once per order "
    "(in parallel). Order IDs look like SO-10234.",
    {"order_id": {"type": "string", "description": "Sales order ID, e.g. SO-10234"}},
    ["order_id"])

GET_INVOICE = _tool(
    "get_invoice",
    "Look up ONE customer invoice: amount, issue and due dates, amount paid, balance, overpayment and status. "
    "Use it for billing and payment questions. Invoice IDs look like AR-90123; an order's invoice ID is in the "
    "get_order_status result.",
    {"invoice_id": {"type": "string", "description": "Invoice ID, e.g. AR-90123"}},
    ["invoice_id"])

SEARCH_POLICIES = _tool(
    "search_policies",
    "Search Kestrel's written policies (shipping and delays, returns and refunds, warranty, data privacy, safety "
    "escalation). Use it BEFORE telling anyone what they are entitled to (compensation, refunds, returns, "
    "warranty) and quote the policy it returns. Returns the best-matching policy sections with citations.",
    {"query": {"type": "string", "description": "Keywords, e.g. 'late delivery compensation customs'"},
     "top_k": {"type": "integer", "description": "Number of passages to return (1-5, default 3)"}},
    ["query"])

OPEN_LOGISTICS_CASE = _tool(
    "open_logistics_case",
    "WRITE ACTION. Open a case for Kestrel's logistics team to chase a carrier about a late or stuck shipment. "
    "Use only when the user asks for follow-up with the carrier, after you have looked the order up. Idempotent: "
    "returns the existing open case for the order instead of opening a second one.",
    {"order_id": {"type": "string", "description": "Sales order ID, e.g. SO-10234"},
     "summary": {"type": "string", "description": "What is wrong and what logistics should do (1-2 sentences)"}},
    ["order_id", "summary"])

TOOLS: list[dict] = [GET_ORDER_STATUS, GET_INVOICE, SEARCH_POLICIES]
TOOL_NAMES = frozenset(t["name"] for t in [*TOOLS, OPEN_LOGISTICS_CASE])
OrderDesk.tool_names = TOOL_NAMES
