"""Companion to exercise 6 - one MCP server, many hosts: mount the plant-ops server in the Agent SDK.

Objective
    Lab 03 used the lab-01 server from a hand-written host. Here the Claude Agent SDK (Claude Code's
    harness) is the host: it spawns the same server over stdio, lists its tools, and answers the same
    question - with no bridging code at all. This is the "N x M -> N + M" argument for MCP in practice.

Concepts
    `mcp_servers={"name": {"type": "stdio", "command": ..., "args": ...}}` (external MCP server) vs
    `create_sdk_mcp_server` (in-process); tool names `mcp__<server>__<tool>`; `tools=[]` to remove every
    built-in tool; least privilege with an explicit `allowed_tools` list and `permission_mode="dontAsk"`.

Run
    python day5_mcp_agent_sdk/solutions/ex06_plant_ops_in_agent_sdk.py

What to observe
    * The init message reports the server as connected and lists only its three tools.
    * The agent's tool calls are prefixed with the server name you chose ("plant-ops").
    * Nothing in this file knows the tools' schemas: they came from the server at runtime.
"""

# test: timeout=240
# test: expect=mcp__plant-ops__check_stock

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query   # noqa: E402

from _agent_common import agent_runtime, show                           # noqa: E402
from _mcp_common import LABS_DIR, SERVER_SCRIPT, load_lab               # noqa: E402
from labkit import MODEL, header                                        # noqa: E402

lab03 = load_lab("03_claude_with_mcp_tools")
PLANT_OPS_TOOLS = ["mcp__plant-ops__check_stock", "mcp__plant-ops__get_order_status", "mcp__plant-ops__list_work_orders"]


async def run() -> None:
    header(f"Exercise 6 companion - the plant-ops MCP server inside the Claude Agent SDK ({MODEL})")
    with agent_runtime("ex06") as env:
        options = ClaudeAgentOptions(
            model=MODEL,
            system_prompt=lab03.SYSTEM,
            tools=[],                                   # no Read/Bash/...: only what the MCP server offers
            mcp_servers={"plant-ops": {"type": "stdio", "command": sys.executable,
                                       "args": [str(SERVER_SCRIPT), "--serve"], "env": {"LABKIT_QUIET": "1"}}},
            strict_mcp_config=True,                     # ignore any other MCP configuration the CLI might find
            allowed_tools=PLANT_OPS_TOOLS,
            permission_mode="dontAsk",
            cwd=str(LABS_DIR),
            setting_sources=[],
            max_turns=10,
            max_budget_usd=1.0,
            env=env,
        )
        result = None
        async for message in query(prompt=lab03.QUESTION, options=options):
            show(message)
            if isinstance(message, ResultMessage):
                result = message
    if result is not None:
        print(f"\nSame server as lab 03, different host: {result.num_turns} turns, estimated ${result.total_cost_usd or 0:.4f}.")


if __name__ == "__main__":
    asyncio.run(run())
