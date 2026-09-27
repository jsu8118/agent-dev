"""Exercise 9 starter - add a resource template `kestrel://assets/{asset_id}` to the plant-ops server.

Run:  python day5_mcp_agent_sdk/exercises/ex09_asset_resource.py
It runs as-is and prints TODO notes; the solution is solutions/ex09_asset_resource.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from mcp import Client                     # noqa: E402

from _mcp_common import load_lab           # noqa: E402

lab01 = load_lab("01_mcp_server")          # lab01._assets() and lab01.WORK_ORDERS_CSV are there to reuse
TEMPLATE = "kestrel://assets/{asset_id}"


def build_server():
    server = lab01.create_server()
    # TODO 1: @server.resource(TEMPLATE, name=..., mime_type="application/json", description=...)
    #         def asset(asset_id: str) -> str: return json.dumps({...asset record..., "recent_work_orders": [...5]})
    # TODO 2: unknown IDs -> raise ResourceNotFoundError("... Valid IDs: ...") (mcp.server.mcpserver.exceptions)
    # TODO 3 (bonus): @server.completion() returning mcp.types.Completion(values=[matching IDs])
    return server


async def main() -> None:
    async with Client(build_server()) as client:
        templates = [t.uri_template for t in (await client.list_resource_templates()).resource_templates]
        print(f"resource templates: {templates}")
        if TEMPLATE not in templates:
            print(f"TODO: register {TEMPLATE} (see the comments in build_server)")
            return
        print((await client.read_resource("kestrel://assets/HF-KP250-03")).contents[0].text[:300])
        print("TODO: also try kestrel://assets/XX-KP999-01 and kestrel://assets/.. and check both are refused")


if __name__ == "__main__":
    asyncio.run(main())
