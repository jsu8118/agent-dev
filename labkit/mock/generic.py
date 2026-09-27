"""The generic fallback policy: used when no scenario policy matches a request.

It is deliberately humble - it never pretends to reason.  It honours the request's
*contract* (structured-output schemas, forced tool_choice, tool results that need an
answer) so that any script, including your own experiments, runs end-to-end offline.
"""

from __future__ import annotations

from .reply import Reply, json_reply, say, tool
from .request import MockRequest
from .schema_tools import synthesize


def _preview(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def generic_reply(req: MockRequest) -> Reply:
    schema = req.output_schema
    if schema is not None:
        return json_reply(synthesize(schema, text=req.conversation_text))

    choice = req.tool_choice or {}
    if choice.get("type") in ("any", "tool") and req.tools and not req.is_tool_result_turn:
        target = next((t for t in req.tools if t.get("name") == choice.get("name")), None) \
            if choice.get("type") == "tool" else next((t for t in req.tools if "input_schema" in t), None)
        if target is not None:
            args = synthesize(target.get("input_schema") or {"type": "object"}, text=req.conversation_text)
            return Reply(content=[tool(target["name"], **(args or {}))])

    if req.is_tool_result_turn:
        lines = [f"- {c.name}: {_preview(c.result or '', 200)}" for c in req.last_tool_results]
        return say("[mock] Here is what the tools returned:\n" + "\n".join(lines))

    question = _preview(req.last_user_text or req.first_user_text or "(no text)")
    return say(
        f"[mock {req.model}] This is a simulated reply (no scenario policy matched). "
        f"You asked: \"{question}\". Set ANTHROPIC_API_KEY to get a real answer from Claude.",
        complexity=0.25,
    )
