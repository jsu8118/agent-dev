"""Lab 02 - Be the MCP host: connect, negotiate, discover, call - and watch the JSON-RPC wire.

Objective
    Drive the lab-01 server the way Claude Desktop or Claude Code does, over stdio (a subprocess)
    and in-process, and see exactly which JSON-RPC messages flow. No LLM is involved: MCP is a
    protocol between programs; the model only ever sees what the host chooses to show it.

Concepts
    Host / client / server roles; stdio transport; lifecycle and capability negotiation in both
    protocol eras (the classic `initialize` handshake of 2024-11-05 ... 2025-11-25 and the
    2026-07-28 `server/discover` probe with per-request `_meta`); JSON-RPC requests, responses,
    notifications and errors; tool errors vs protocol errors; in-process vs subprocess cost.

Run
    python day5_mcp_agent_sdk/labs/02_mcp_client_explore.py

What to observe
    * Legacy mode: initialize -> response (capabilities) -> notifications/initialized, then requests.
    * Auto mode (default in mcp 2.x): a `server/discover` probe; afterwards EVERY request carries the
      protocol version and client capabilities in `_meta` - the server keeps no per-connection state.
    * Two error shapes: an unknown resource URI is a JSON-RPC *error* (code -32602, a protocol
      problem the host handles); a failing tool call - bad arguments, a raised exception and, in the
      Python SDK's MCPServer, even an unknown tool name - is a normal *result* with isError=true,
      which the host can pass to the model so it can correct itself.
    * Spawning a stdio server costs a process start; each call then costs a few hundred microseconds
      to a few milliseconds of IPC. In-process calls skip both.
"""

# test: expect=JSON-RPC messages

from __future__ import annotations

import asyncio
import json
import statistics
import time

from mcp import Client, MCPError, stdio_client
from mcp.types import Implementation

from _mcp_common import load_lab, plant_ops_stdio_params, summarize_jsonrpc, tapped, tool_result_text
from labkit import header, step

server_lab = load_lab("01_mcp_server")
CLIENT = Implementation(name="kestrel-lab02-host", version="1.0.0")   # shows up as clientInfo on the wire


async def show_wire(mode: str) -> None:
    """Connect over stdio in the given negotiation mode and print every JSON-RPC message."""
    wire: list[tuple[str, float, dict]] = []
    async with Client(tapped(stdio_client(plant_ops_stdio_params()), wire), mode=mode, client_info=CLIENT) as client:
        await client.list_tools()
        await client.call_tool("check_stock", {"sku": "MS-250"})
        await client.read_resource("kestrel://manuals")
        print(f"negotiated protocol version: {client.protocol_version}")
    print(f"{len(wire)} JSON-RPC messages (-> client to server, <- server to client):")
    for direction, _, message in wire:
        print(f"  {direction} {summarize_jsonrpc(message, width=140)}")
    first_request, first_response = wire[0][2], wire[1][2]
    print("\nThe opening exchange in full:")
    print("  -> " + json.dumps(first_request, indent=2).replace("\n", "\n     "))
    print("  <- " + json.dumps(first_response, indent=2)[:1400].replace("\n", "\n     "))


async def explore(client: Client) -> None:
    tools = (await client.list_tools()).tools
    print(f"tools: {[t.name for t in tools]}")
    resources = (await client.list_resources()).resources
    templates = (await client.list_resource_templates()).resource_templates
    prompts = (await client.list_prompts()).prompts
    print(f"resources: {[r.uri for r in resources]}  templates: {[t.uri_template for t in templates]}")
    print(f"prompts: {[p.name for p in prompts]}")

    result = await client.call_tool("get_order_status", {"order_id": "SO-10283"})
    print(f"get_order_status -> is_error={result.is_error}; text: {tool_result_text(result, 120)}")

    failing = await client.call_tool("get_order_status", {"order_id": "10283"})
    print(f"tool error      -> is_error={failing.is_error}; text: {tool_result_text(failing, 120)}")

    unknown = await client.call_tool("delete_all_orders", {})
    print(f"unknown tool    -> is_error={unknown.is_error}; text: {tool_result_text(unknown, 120)}")
    try:
        await client.read_resource("kestrel://manuals/../../company/policies/warranty_policy")
    except MCPError as exc:
        print(f"protocol error  -> MCPError code={exc.error.code}: {exc.error.message[:90]}")


async def time_calls(client: Client, n: int = 25) -> list[float]:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        await client.call_tool("check_stock", {"sku": "MS-250"})
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


async def run() -> None:
    header("Lab 02 - an MCP client: negotiation, discovery, calls and the JSON-RPC wire")

    step(1, "stdio + legacy handshake (protocol eras up to 2025-11-25)")
    await show_wire("legacy")

    step(2, "stdio + auto negotiation (mcp 2.x default: probe server/discover, fall back to initialize)")
    await show_wire("auto")

    step(3, "Discovery and error semantics over stdio")
    t0 = time.perf_counter()
    async with Client(plant_ops_stdio_params()) as client:
        connect_ms = (time.perf_counter() - t0) * 1000
        await explore(client)
        stdio_ms = await time_calls(client)

    step(4, "The same server in-process: Client(mcp) - no subprocess, no JSON framing")
    t0 = time.perf_counter()
    async with Client(server_lab.mcp) as client:
        inproc_connect_ms = (time.perf_counter() - t0) * 1000
        await explore(client)
        inproc_ms = await time_calls(client)

    step(5, "What the transport costs (this machine, this run)")
    print(f"connect:  stdio {connect_ms:7.1f} ms (spawns python, imports the server)   in-process {inproc_connect_ms:6.1f} ms")
    print(f"call p50: stdio {statistics.median(stdio_ms):7.2f} ms   in-process {statistics.median(inproc_ms):6.2f} ms "
          f"({len(stdio_ms)} check_stock calls each; both include the SQLite query)")
    print("Both are negligible next to a model turn (seconds). Choose the transport for isolation and "
          "deployment, not for speed.")
    print("\nDone: all JSON-RPC messages above were produced and parsed by the real MCP SDK.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
