"""Solution to exercise 12 - lab 03 on the Tool Runner, keeping the controls of the manual loop.

Objective
    Let `client.beta.messages.tool_runner` drive the agent loop, but wrap every MCP tool yourself
    (instead of `async_mcp_tool`) so that each call goes through: an allow-list, a per-run call
    budget, an approval gate for tools that are not read-only, latency/size logging, and a result
    size cap.

Concepts
    What the MCP helper does internally (MCP Tool -> `beta_async_tool` with the MCP input schema; a
    function that forwards to `session.call_tool`); `anthropic.lib.tools.ToolError` -> is_error tool
    results; runner `max_iterations`; least privilege per task; annotations as *input* to policy
    (trusted only because this is our own server).

Run
    python day5_mcp_agent_sdk/solutions/ex12_tool_runner_controls.py

What to observe
    * The server now also offers `request_rma` (exercise 10); the allow-list keeps it away from a
      question-answering task, so a server upgrade cannot silently widen what the agent can do.
    * Run 1 answers within budget. Run 2 has a budget of 2 calls: the third call gets a clear error,
      and the answer reports the gap instead of inventing stock numbers.
"""

# test: expect=budget exhausted

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from anthropic import beta_async_tool                                  # noqa: E402
from anthropic.lib.tools import ToolError                              # noqa: E402
from mcp import Client                                                 # noqa: E402
from mcp.types import TextContent, Tool                                # noqa: E402

from _mcp_common import load_lab                                       # noqa: E402
from labkit import MODEL, get_async_client, header, step, text_of, wrap  # noqa: E402

lab03 = load_lab("03_claude_with_mcp_tools")        # reuse QUESTION, SYSTEM and the strict-schema sanitiser


def _load_solution(stem: str):
    spec = importlib.util.spec_from_file_location(f"day5_solution_{stem}", Path(__file__).with_name(f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ALLOWED_FOR_THIS_TASK = {"check_stock", "get_order_status", "list_work_orders"}
MAX_RESULT_CHARS = 8000


@dataclass
class Governor:
    budget: int
    approve: Callable[[str, dict], Awaitable[bool]]
    calls: int = 0
    log: list[dict] = field(default_factory=list)


def governed_tool(mcp_tool: Tool, session: Client, gov: Governor):
    read_only = bool(mcp_tool.annotations and mcp_tool.annotations.read_only_hint)

    async def call(**arguments: Any) -> str:
        if gov.calls >= gov.budget:
            gov.log.append({"tool": mcp_tool.name, "outcome": "budget"})
            raise ToolError(f"Tool-call budget exhausted ({gov.budget} calls for this task). Do not call more tools; "
                            "answer with what you have and say what is missing.")
        gov.calls += 1
        if not read_only and not await gov.approve(mcp_tool.name, arguments):
            gov.log.append({"tool": mcp_tool.name, "outcome": "not approved"})
            raise ToolError(f"{mcp_tool.name} needs human approval and was not approved.")
        started = time.perf_counter()
        result = await session.call_tool(mcp_tool.name, arguments)
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
        if not text and result.structured_content is not None:
            text = json.dumps(result.structured_content)
        truncated = max(0, len(text) - MAX_RESULT_CHARS)
        if truncated:
            text = text[:MAX_RESULT_CHARS] + f"\n[... {truncated} characters truncated by the host]"
        gov.log.append({"tool": mcp_tool.name, "args": arguments, "ms": round((time.perf_counter() - started) * 1000, 1),
                        "chars": len(text), "is_error": bool(result.is_error), "outcome": "ok"})
        if result.is_error:
            raise ToolError(text)          # becomes a tool_result with is_error: true
        return text

    return beta_async_tool(call, name=mcp_tool.name, description=mcp_tool.description or "",
                           input_schema=lab03.claude_schema(copy.deepcopy(mcp_tool.input_schema)), strict=True)


async def human_approval(tool_name: str, arguments: dict) -> bool:
    print(f"    [approval requested] {tool_name}({json.dumps(arguments)}) -> denied by default in this demo")
    return False


async def run_task(session: Client, budget: int) -> None:
    tools = (await session.list_tools()).tools
    exposed = [t for t in tools if t.name in ALLOWED_FOR_THIS_TASK]
    hidden = sorted(t.name for t in tools if t.name not in ALLOWED_FOR_THIS_TASK)
    gov = Governor(budget=budget, approve=human_approval)
    print(f"exposed={[t.name for t in exposed]}  hidden by allow-list={hidden}  budget={budget} calls")
    runner = get_async_client().beta.messages.tool_runner(
        model=MODEL, max_tokens=8000, system=lab03.SYSTEM, max_iterations=6,
        tools=[governed_tool(t, session, gov) for t in exposed],
        messages=[{"role": "user", "content": lab03.QUESTION}])
    final = None
    async for message in runner:
        final = message
    for entry in gov.log:
        if entry["outcome"] == "ok":
            print(f"  call {entry['tool']}({json.dumps(entry['args'])}) {entry['ms']} ms, {entry['chars']} chars, "
                  f"is_error={entry['is_error']}")
        else:
            print(f"  call {entry['tool']} -> refused ({entry['outcome']})")
    print("Answer:\n" + wrap(text_of(final)))


async def run() -> None:
    header(f"Exercise 12 - Tool Runner with host-side controls ({MODEL})")
    ex10 = _load_solution("ex10_rma_approval")
    async with Client(ex10.build_server()) as session:      # plant-ops + request_rma, in-process
        step(1, "Run 1 - normal budget")
        await run_task(session, budget=5)
        step(2, "Run 2 - budget of 2 calls: the third call is refused (budget exhausted)")
        await run_task(session, budget=2)


if __name__ == "__main__":
    asyncio.run(run())
