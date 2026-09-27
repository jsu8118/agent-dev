"""Solution to exercise 10 - a write tool with layered input validation and a human approval step.

Objective
    Add `request_rma` to the plant-ops server. It validates arguments declaratively (JSON Schema from
    type hints), validates business rules in code, and asks a human to approve through MCP
    *elicitation* before it records a draft RMA. The demo host answers the elicitation requests
    with a scripted "human", so every path runs offline.

Concepts
    Three validation layers (schema -> business rules -> human), resolvers (`Resolve`) and
    elicitation (`Elicit`) in the MCP Python SDK 2.x, era-neutral: over 2026-07-28 the question
    travels inside an InputRequiredResult and the client retries; over older protocol versions the
    server sends an `elicitation/create` request mid-call. Idempotent writes. Writes go to .runs/,
    never to data/.

Run
    python day5_mcp_agent_sdk/solutions/ex10_rma_approval.py

What to observe
    * Invalid input is rejected before anyone is bothered; business-rule errors say how to recover.
    * The approval question reaches the host with a JSON Schema for the answer (approve, approver).
    * A declined request produces a tool error the model can relay - it must not retry.
    * Asking twice for the same RMA returns the existing draft without asking the human again.
"""

# test: expect=approved

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from mcp import Client, types                                         # noqa: E402
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult  # noqa: E402
from mcp.server.mcpserver import Elicit, Resolve                      # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError                 # noqa: E402
from mcp.types import ToolAnnotations                                 # noqa: E402
from pydantic import BaseModel, Field                                 # noqa: E402

from _mcp_common import load_lab, tool_result_text                   # noqa: E402
from labkit import runs_dir                                           # noqa: E402
from labkit.data import ops_db                                        # noqa: E402

lab01 = load_lab("01_mcp_server")
DRAFTS = runs_dir("day5_rma_drafts") / "drafts.jsonl"

Reason = Literal["defective", "wrong_item", "damaged_in_transit", "no_longer_needed"]


class Approval(BaseModel):
    """What the human answers. Elicitation schemas must be flat objects of primitive fields."""
    approve: bool = Field(description="Create this RMA draft?")
    approver: str = Field(default="", description="Your name, for the audit trail")


class RmaDraft(BaseModel):
    draft_id: str
    order_id: str
    sku: str
    qty: int
    reason: str
    approved_by: str
    created_at: str
    existing: bool
    next_step: str


def _load_drafts() -> list[dict]:
    if not DRAFTS.exists():
        return []
    return [json.loads(line) for line in DRAFTS.read_text(encoding="utf-8").splitlines() if line.strip()]


def _existing(order_id: str, sku: str, reason: str) -> dict | None:
    return next((d for d in _load_drafts() if (d["order_id"], d["sku"], d["reason"]) == (order_id, sku, reason)), None)


def check_business_rules(order_id: str, sku: str, qty: int) -> None:
    """Layer 2: rules the schema cannot express. Messages tell the caller how to recover."""
    with ops_db() as db:
        order = db.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if order is None:
            raise ToolError(f"Order {order_id} not found. Ask the customer for the order ID (format SO-12345).")
        lines = {r["sku"]: r["qty"] for r in db.execute("SELECT sku, qty FROM order_lines WHERE order_id = ?", (order_id,))}
    if sku not in lines:
        raise ToolError(f"{sku} is not on order {order_id} (lines: {', '.join(f'{s} x{q}' for s, q in lines.items())}).")
    if qty > lines[sku]:
        raise ToolError(f"Only {lines[sku]} x {sku} were ordered on {order_id}; qty {qty} is too high.")
    if order["status"] != "delivered":
        raise ToolError(f"Order {order_id} is '{order['status']}', not delivered: cancel or amend it instead of an RMA.")


def ask_approval(order_id: str, sku: str, qty: int, reason: str):
    """Resolver: runs before the tool body. It may return a value, or an Elicit marker to ask the user."""
    order_id, sku = order_id.strip().upper(), sku.strip().upper()
    check_business_rules(order_id, sku, qty)               # fail fast: never ask a human about an invalid request
    existing = _existing(order_id, sku, reason)
    if existing:                                           # idempotent: approved already, don't ask again
        return Approval(approve=True, approver=existing["approved_by"])   # a plain value is injected as "accepted"
    return Elicit(f"Create an RMA draft for {qty} x {sku} on order {order_id} (reason: {reason})?", Approval)


def build_server():
    server = lab01.create_server()

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                             open_world_hint=False))
    def request_rma(order_id: str, sku: str,
                    qty: Annotated[int, Field(ge=1, le=500, description="Units to return")],
                    reason: Reason,
                    approval: Annotated[ElicitationResult[Approval], Resolve(ask_approval)]) -> RmaDraft:
        """Create a draft RMA for one order line after a human approves it. Use after confirming the order
        with get_order_status. The draft is reviewed by the returns team before an RMA number is issued."""
        order_id, sku = order_id.strip().upper(), sku.strip().upper()
        if not isinstance(approval, AcceptedElicitation) or not approval.data.approve:
            raise ToolError("A human declined this RMA request. Do not retry; tell the requester it was not approved.")
        existing = _existing(order_id, sku, reason)
        if existing:
            return RmaDraft(**existing, existing=True, next_step="Already drafted; the returns team will follow up.")
        draft = {"draft_id": f"RMA-DRAFT-{len(_load_drafts()) + 1:04d}", "order_id": order_id, "sku": sku, "qty": qty,
                 "reason": reason, "approved_by": approval.data.approver or "unknown",
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with DRAFTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(draft) + "\n")
        return RmaDraft(**draft, existing=False, next_step="Returns team issues the RMA-#### number within 1 business day.")

    return server


class ScriptedHuman:
    """The host's elicitation handler. A real host renders a form; this one follows a script."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)

    async def __call__(self, context, params: types.ElicitRequestParams) -> types.ElicitResult:
        answer = self.answers.pop(0) if self.answers else "decline"
        fields = list((getattr(params, "requested_schema", None) or {}).get("properties", {}))
        print(f"    [host asks the user] {params.message}  (form fields: {fields}) -> user: {answer}")
        if answer == "approve":
            return types.ElicitResult(action="accept", content={"approve": True, "approver": "k.lam (returns desk)"})
        return types.ElicitResult(action=answer)            # "decline" or "cancel"


async def call(client: Client, label: str, args: dict) -> None:
    print(f"- {label}: request_rma({json.dumps(args)})")
    result = await client.call_tool("request_rma", args)
    if result.is_error:
        print(f"    is_error=True: {tool_result_text(result, 200)}")
    else:
        draft = result.structured_content
        print(f"    {'existing' if draft['existing'] else 'approved'} draft {draft['draft_id']} by {draft['approved_by']}; "
              f"next: {draft['next_step']}")


async def run() -> None:
    DRAFTS.unlink(missing_ok=True)          # start the demo from an empty draft store
    server = build_server()
    human = ScriptedHuman(["approve", "decline"])
    async with Client(server, elicitation_callback=human) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "request_rma")
        props = tool.input_schema["properties"]
        print(f"request_rma schema: qty={props['qty']}  reason.enum={props['reason']['enum']}")
        print(f"(the 'approval' parameter is filled by the resolver, so it is NOT in the schema: {'approval' not in props})\n")
        await call(client, "valid request, human approves", {"order_id": "SO-10283", "sku": "MS-250", "qty": 2,
                                                             "reason": "no_longer_needed"})
        await call(client, "same request again (idempotent)", {"order_id": "SO-10283", "sku": "MS-250", "qty": 2,
                                                               "reason": "no_longer_needed"})
        await call(client, "schema violation (qty=0)", {"order_id": "SO-10283", "sku": "MS-250", "qty": 0,
                                                        "reason": "defective"})
        await call(client, "business rule (SKU not on order)", {"order_id": "SO-10283", "sku": "MS-400", "qty": 1,
                                                                "reason": "defective"})
        await call(client, "business rule (qty > ordered)", {"order_id": "SO-10283", "sku": "MS-250", "qty": 11,
                                                             "reason": "defective"})
        await call(client, "valid request, human declines", {"order_id": "SO-10283", "sku": "MS-250", "qty": 1,
                                                             "reason": "defective"})
    print(f"\nDraft store: {DRAFTS} -> {len(_load_drafts())} draft(s); data/ untouched.")


if __name__ == "__main__":
    asyncio.run(run())
