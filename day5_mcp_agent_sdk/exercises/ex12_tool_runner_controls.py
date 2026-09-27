"""Exercise 12 starter - lab 03 on the Tool Runner, keeping the manual loop's controls.

Run:  python day5_mcp_agent_sdk/exercises/ex12_tool_runner_controls.py
It runs as-is (mock mode needs no key) and prints TODO notes; the solution is solutions/ex12_tool_runner_controls.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from anthropic import beta_async_tool                 # noqa: E402
from mcp import Client                                # noqa: E402

from _mcp_common import load_lab, plant_ops_stdio_params, tool_result_text   # noqa: E402
from labkit import MODEL, get_async_client, text_of   # noqa: E402

lab03 = load_lab("03_claude_with_mcp_tools")


def governed_tool(mcp_tool, session: Client):
    async def call(**arguments):
        # TODO 1: refuse tools not on this task's allow-list (better: filter them out before building the runner)
        # TODO 2: enforce a per-run call budget -> raise anthropic.lib.tools.ToolError("...") when exhausted
        # TODO 3: ask for approval when mcp_tool.annotations.read_only_hint is not True
        # TODO 4: log latency and result size; cap the result size
        result = await session.call_tool(mcp_tool.name, arguments)
        return tool_result_text(result, 100_000)
    return beta_async_tool(call, name=mcp_tool.name, description=mcp_tool.description or "",
                           input_schema=lab03.claude_schema(mcp_tool.input_schema), strict=True)


async def main() -> None:
    async with Client(plant_ops_stdio_params()) as session:
        tools = (await session.list_tools()).tools
        runner = get_async_client().beta.messages.tool_runner(
            model=MODEL, max_tokens=8000, system=lab03.SYSTEM, max_iterations=6,
            tools=[governed_tool(t, session) for t in tools],
            messages=[{"role": "user", "content": lab03.QUESTION}])
        final = None
        async for message in runner:
            final = message
        print(text_of(final)[:400])
    print("\nTODO: add the four controls in governed_tool, then re-run with a budget of 2 and check the answer "
          "admits what it could not look up.")


if __name__ == "__main__":
    asyncio.run(main())
