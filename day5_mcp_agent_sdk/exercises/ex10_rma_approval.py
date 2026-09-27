"""Exercise 10 starter - a `request_rma` tool with validation and a human approval step.

Run:  python day5_mcp_agent_sdk/exercises/ex10_rma_approval.py
It runs as-is and prints TODO notes; the solution is solutions/ex10_rma_approval.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from mcp import Client, types              # noqa: E402

from _mcp_common import load_lab, tool_result_text   # noqa: E402

lab01 = load_lab("01_mcp_server")


def build_server():
    server = lab01.create_server()

    @server.tool()
    def request_rma(order_id: str, sku: str, qty: int, reason: str) -> dict:
        """Create a draft RMA for one order line."""
        # TODO 1: declarative validation - qty: Annotated[int, Field(ge=1, le=500)], reason: Literal[...four values]
        # TODO 2: business rules in code (order exists, SKU on order, qty <= ordered, delivered) -> raise ToolError(...)
        # TODO 3: human approval via elicitation: a parameter
        #         approval: Annotated[ElicitationResult[Approval], Resolve(ask_approval)]
        #         where ask_approval(...) returns Elicit("Create an RMA draft for ...?", Approval)
        # TODO 4: record the draft under labkit.runs_dir(...), never in data/; make repeated requests idempotent
        return {"draft_id": "TODO", "order_id": order_id, "sku": sku, "qty": qty, "reason": reason}

    return server


async def answer_elicitation(context, params) -> types.ElicitResult:
    print(f"  [host] the server asks: {params.message}")
    return types.ElicitResult(action="accept", content={"approve": True, "approver": "you"})


async def main() -> None:
    async with Client(build_server(), elicitation_callback=answer_elicitation) as client:
        for args in ({"order_id": "SO-10283", "sku": "MS-250", "qty": 2, "reason": "no_longer_needed"},
                     {"order_id": "SO-10283", "sku": "MS-250", "qty": 0, "reason": "defective"},
                     {"order_id": "SO-10283", "sku": "MS-400", "qty": 1, "reason": "defective"}):
            result = await client.call_tool("request_rma", args)
            print(f"request_rma({args}) -> is_error={result.is_error}: {tool_result_text(result, 120)}")
    print("TODO: the 2nd and 3rd calls should fail with helpful errors, and the 1st should ask the host for approval.")


if __name__ == "__main__":
    asyncio.run(main())
