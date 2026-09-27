"""Lab 03 - Give Claude the MCP server's tools: a manual bridge, then the SDK's MCP helpers.

Objective
    Answer an operations question with Claude + the lab-01 MCP server:
    "Harbor Foods wants to repeat order SO-10283. Do we have enough MS-250 seal kits in stock to
    cover it, and when was the last seal replacement on their pump HF-KP250-03?"
    First by converting MCP tool definitions to Claude tool definitions by hand and running your
    own agent loop; then with the Anthropic SDK's MCP helpers and the Tool Runner; finally by
    using the server's *prompt* and *resource* primitives, not just its tools.

Concepts
    Your program is the MCP host: it owns both the MCP client session and the Claude conversation.
    MCP tool -> Claude tool conversion (inputSchema -> input_schema, strict-mode sanitising);
    CallToolResult -> tool_result (content + is_error); parallel tool calls answered in ONE user
    message; `anthropic.lib.tools.mcp.async_mcp_tool` + `client.beta.messages.tool_runner`;
    `mcp_message` for MCP prompts (with an embedded resource).

Run
    python day5_mcp_agent_sdk/labs/03_claude_with_mcp_tools.py

What to observe
    * The converted schema: pydantic titles and numeric bounds are removed for strict mode, bounds
      move into the description, and every object gets additionalProperties:false. The MCP
      server still validates the bounds - the model just can't be *forced* to respect them.
    * Turn 1 asks for two tools in parallel; both results go back in one user message.
    * The answer is built from tool results only: the stock verdict includes the per-warehouse
      nuance, and "no seal replacement on record" is reported as a fact, not papered over.
    * Part B produces the same conversation with ~15 lines of code instead of ~60.
"""

# test: expect=Drafted email

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from anthropic.lib.tools.mcp import async_mcp_tool, mcp_message
from mcp import Client
from mcp.types import CallToolResult, TextContent, Tool

from _mcp_common import plant_ops_stdio_params
from labkit import MODEL, get_async_client, header, step, text_of, usage_summary, wrap

QUESTION = ("Harbor Foods wants to repeat order SO-10283. Do we have enough MS-250 seal kits in stock to cover it, "
            "and when was the last seal replacement on their pump HF-KP250-03?")
SYSTEM = ("You are Kestrel's operations assistant for internal staff. Use the tools to look up facts; never guess "
          "stock levels, dates or maintenance history. Quote the numbers you rely on. If a record is missing, say so "
          "explicitly. Keep the answer short.")
MAX_TURNS = 8

# JSON-Schema keywords strict tool use does not support (see the Claude API docs on structured
# outputs). The MCP server keeps enforcing them; we only stop sending them to the API.
UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
               "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems")


# --------------------------------------------------------------------------- part A: manual bridge
def claude_schema(schema: dict) -> dict:
    """Make an MCP inputSchema acceptable for a strict Claude tool."""
    schema = copy.deepcopy(schema)

    def fix(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("title", None)
            dropped = {k: node.pop(k) for k in UNSUPPORTED if k in node}
            if "default" in node:
                dropped["default"] = node.pop("default")
            if dropped:          # keep the information for the model, as prose
                note = ", ".join(f"{k}={v}" for k, v in dropped.items())
                node["description"] = (node.get("description", "") + f" ({note})").strip()
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
            for key in ("properties", "$defs", "definitions"):
                for child in (node.get(key) or {}).values():
                    fix(child)
            for key in ("items", "anyOf", "allOf", "oneOf"):
                child = node.get(key)
                for sub in (child if isinstance(child, list) else [child] if child else []):
                    fix(sub)

    fix(schema)
    return schema


def to_claude_tool(mcp_tool: Tool) -> dict:
    return {"name": mcp_tool.name, "description": mcp_tool.description or "",
            "input_schema": claude_schema(mcp_tool.input_schema), "strict": True}


def to_tool_result(tool_use_id: str, result: CallToolResult) -> dict:
    """MCP CallToolResult -> Claude tool_result block (text only; images would map to image blocks)."""
    blocks = [{"type": "text", "text": c.text} for c in result.content if isinstance(c, TextContent)]
    if not blocks and result.structured_content is not None:
        blocks = [{"type": "text", "text": json.dumps(result.structured_content)}]
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": blocks or "(no content)",
            "is_error": bool(result.is_error)}


async def manual_loop(mcp_client: Client) -> None:
    mcp_tools = (await mcp_client.list_tools()).tools
    claude_tools = [to_claude_tool(t) for t in mcp_tools]
    limit_before = next(t for t in mcp_tools if t.name == "list_work_orders").input_schema["properties"]["limit"]
    limit_after = next(t for t in claude_tools if t["name"] == "list_work_orders")["input_schema"]["properties"]["limit"]
    print(f"MCP  limit schema: {json.dumps(limit_before)}")
    print(f"Claude limit schema: {json.dumps(limit_after)}")

    client = get_async_client()
    messages: list[dict] = [{"role": "user", "content": QUESTION}]
    for turn in range(1, MAX_TURNS + 1):
        response = await client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM, tools=claude_tools,
                                                messages=messages)
        messages.append({"role": "assistant", "content": response.content})   # full content, thinking included
        calls = [b for b in response.content if b.type == "tool_use"]
        print(f"turn {turn}: stop_reason={response.stop_reason}  tool calls={[f'{c.name}({json.dumps(c.input)})' for c in calls]}"
              f"  [{usage_summary(response)}]")
        if response.stop_reason != "tool_use":
            print("\nAnswer:\n" + wrap(text_of(response)))
            return
        # Parallel calls: run them concurrently, answer ALL of them in one user message.
        results = await asyncio.gather(*(mcp_client.call_tool(c.name, c.input) for c in calls))
        for c, r in zip(calls, results):
            print(f"   {c.name} -> is_error={r.is_error}, {len(r.content)} content block(s)")
        messages.append({"role": "user", "content": [to_tool_result(c.id, r) for c, r in zip(calls, results)]})
    print(f"Stopped after {MAX_TURNS} turns without a final answer.")


# --------------------------------------------------------------------------- part B: SDK helpers + Tool Runner
async def tool_runner(mcp_client: Client) -> None:
    client = get_async_client()
    mcp_tools = (await mcp_client.list_tools()).tools
    # Same sanitising as part A, then let the helper wrap each MCP tool as a runnable beta tool.
    runnable = [async_mcp_tool(t.model_copy(update={"input_schema": claude_schema(t.input_schema)}), mcp_client,
                               strict=True) for t in mcp_tools]
    runner = client.beta.messages.tool_runner(model=MODEL, max_tokens=8000, system=SYSTEM, tools=runnable,
                                              messages=[{"role": "user", "content": QUESTION}], max_iterations=MAX_TURNS)
    final = None
    async for message in runner:              # one BetaMessage per model turn; tools run between turns
        calls = [f"{b.name}({json.dumps(b.input)})" for b in message.content if b.type == "tool_use"]
        print(f"runner turn: stop_reason={message.stop_reason}  tool calls={calls}")
        final = message
    print("\nAnswer:\n" + wrap(text_of(final)))


# --------------------------------------------------------------------------- part C: prompts + resources
async def prompt_primitive(mcp_client: Client) -> None:
    """A user picks the server's 'draft_rma_email' prompt (a slash command in Claude Code/Desktop)."""
    client = get_async_client()
    prompt = await mcp_client.get_prompt("draft_rma_email", {"order_id": "SO-10283", "sku": "MS-250",
                                                             "reason": "no_longer_needed"})
    messages = [mcp_message(m) for m in prompt.messages]
    print("prompt messages -> Claude content blocks: "
          + ", ".join(f"{m['role']}:{m['content'][0]['type']}" for m in messages))
    tools = [async_mcp_tool(t.model_copy(update={"input_schema": claude_schema(t.input_schema)}), mcp_client, strict=True)
             for t in (await mcp_client.list_tools()).tools if t.name == "get_order_status"]   # least privilege
    runner = client.beta.messages.tool_runner(model=MODEL, max_tokens=8000, tools=tools, messages=messages,
                                              max_iterations=4)
    final = None
    async for message in runner:
        final = message
    print("\nDrafted email:\n" + wrap(text_of(final)))


async def run() -> None:
    header(f"Lab 03 - Claude + MCP tools ({MODEL})")
    print(f"Question: {QUESTION}")
    async with Client(plant_ops_stdio_params()) as mcp_client:    # one server process for all three parts
        step("A", "manual bridge - convert MCP tools, run our own agent loop")
        await manual_loop(mcp_client)
        step("B", "the Anthropic SDK's MCP helpers + the Tool Runner")
        await tool_runner(mcp_client)
        step("C", "the server's prompt (with an embedded policy resource) drives the chat")
        await prompt_primitive(mcp_client)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
