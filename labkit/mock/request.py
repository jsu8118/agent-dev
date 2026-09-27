"""A read-only, convenience view over a /v1/messages request body.

Scenario policies (the rule-based "stand-in models" in labkit/mock/scenarios) use this
to ask questions like "has the agent already called lookup_order?" or "what did the
last tool return?" without re-implementing message parsing each time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from ..models import get_spec, known_model, thinking_active


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    result: str | None = None       # text of the matching tool_result (None if unanswered)
    is_error: bool = False

    def result_json(self) -> Any:
        """Parse the tool result as JSON (returns None if it is not JSON)."""
        if self.result is None:
            return None
        try:
            return json.loads(self.result)
        except ValueError:
            return None


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in content]
    return []


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict) and b.get("type") == "search_result":
                parts.append(" ".join(c.get("text", "") for c in b.get("content", []) if isinstance(c, dict)))
        return "\n".join(parts)
    return ""


class MockRequest:
    def __init__(self, body: dict, headers: dict | None = None) -> None:
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    # -- basic fields ------------------------------------------------------------
    @property
    def model(self) -> str:
        return self.body.get("model", "")

    @property
    def spec(self):
        return get_spec(self.model) if known_model(self.model) else None

    @property
    def max_tokens(self) -> int:
        return int(self.body.get("max_tokens", 0))

    @property
    def stream(self) -> bool:
        return bool(self.body.get("stream"))

    @property
    def messages(self) -> list[dict]:
        return self.body.get("messages") or []

    @property
    def betas(self) -> set[str]:
        raw = self.headers.get("anthropic-beta", "")
        return {b.strip() for b in raw.split(",") if b.strip()}

    @cached_property
    def system_text(self) -> str:
        system = self.body.get("system")
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            return "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
        return ""

    # -- tools -------------------------------------------------------------------
    @property
    def tools(self) -> list[dict]:
        return self.body.get("tools") or []

    @cached_property
    def tool_names(self) -> set[str]:
        return {t.get("name") for t in self.tools if t.get("name")}

    def has_tool(self, *names: str) -> bool:
        return any(n in self.tool_names for n in names)

    @property
    def tool_choice(self) -> dict | None:
        return self.body.get("tool_choice")

    # -- output / thinking config -----------------------------------------------------
    @property
    def output_config(self) -> dict:
        return self.body.get("output_config") or {}

    @property
    def output_schema(self) -> dict | None:
        fmt = self.output_config.get("format") or self.body.get("output_format")
        if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            return fmt.get("schema")
        return None

    @property
    def effort(self) -> str:
        spec = self.spec
        default = spec.default_effort if spec else "high"
        return self.output_config.get("effort") or default or "high"

    @property
    def thinking(self) -> dict | None:
        return self.body.get("thinking")

    @property
    def thinking_active(self) -> bool:
        return bool(self.spec) and thinking_active(self.model, self.thinking)

    @property
    def thinking_display(self) -> str:
        return (self.thinking or {}).get("display", "omitted")

    # -- conversation views -----------------------------------------------------------
    def texts(self, role: str = "user") -> list[str]:
        """Plain text of every message with the given role (tool results excluded)."""
        out = []
        for m in self.messages:
            if m.get("role") != role:
                continue
            text = "\n".join(b.get("text", "") for b in _blocks(m.get("content")) if b.get("type") == "text")
            if text.strip():
                out.append(text)
        return out

    @property
    def first_user_text(self) -> str:
        texts = self.texts("user")
        return texts[0] if texts else ""

    @property
    def last_user_text(self) -> str:
        """Text blocks of the most recent user message (empty if it holds only tool results)."""
        for m in reversed(self.messages):
            if m.get("role") == "user":
                return "\n".join(b.get("text", "") for b in _blocks(m.get("content")) if b.get("type") == "text")
        return ""

    @cached_property
    def conversation_text(self) -> str:
        """All user text plus all tool-result text: handy for keyword matching."""
        parts: list[str] = []
        for m in self.messages:
            for b in _blocks(m.get("content")):
                if b.get("type") == "text" and m.get("role") == "user":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    parts.append(_result_text(b))
        return "\n".join(parts)

    @cached_property
    def tool_calls(self) -> list[ToolCall]:
        """Every client tool call in the conversation, with its result attached."""
        calls: list[ToolCall] = []
        by_id: dict[str, ToolCall] = {}
        for m in self.messages:
            for b in _blocks(m.get("content")):
                if m.get("role") == "assistant" and b.get("type") == "tool_use":
                    call = ToolCall(id=b.get("id", ""), name=b.get("name", ""), input=b.get("input") or {})
                    calls.append(call)
                    by_id[call.id] = call
                elif m.get("role") == "user" and b.get("type") == "tool_result":
                    call = by_id.get(b.get("tool_use_id", ""))
                    if call is not None:
                        call.result = _result_text(b)
                        call.is_error = bool(b.get("is_error"))
        return calls

    def calls(self, name: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == name]

    def called(self, name: str) -> bool:
        return any(c.name == name for c in self.tool_calls)

    @property
    def last_tool_results(self) -> list[ToolCall]:
        """Tool calls answered by the final user message (empty unless this is a tool-result turn)."""
        if not self.messages or self.messages[-1].get("role") != "user":
            return []
        ids = {b.get("tool_use_id") for b in _blocks(self.messages[-1].get("content")) if b.get("type") == "tool_result"}
        return [c for c in self.tool_calls if c.id in ids]

    @property
    def is_tool_result_turn(self) -> bool:
        return bool(self.last_tool_results)

    @property
    def assistant_turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "assistant")

    @cached_property
    def documents(self) -> list[dict]:
        """Document / search_result blocks in user turns, in API order (document_index / search_result_index).

        Each item: {"index", "kind", "title", "text", "citations", "source"}.  Text-source documents cite by
        character range; custom-content documents and search results cite by content block.
        """
        docs: list[dict] = []
        counters = {"document": 0, "search_result": 0}

        def add(block: dict) -> None:
            kind = block.get("type")
            if kind == "document":
                source = block.get("source") or {}
                if source.get("type") == "text":
                    text = source.get("data", "")
                elif source.get("type") == "content":
                    text = "\n".join(b.get("text", "") for b in source.get("content") or [] if isinstance(b, dict))
                else:
                    text = ""
            else:
                text = "\n".join(b.get("text", "") for b in block.get("content") or [] if isinstance(b, dict))
            docs.append({"index": counters[kind], "kind": kind, "title": block.get("title"), "text": text,
                         "citations": bool((block.get("citations") or {}).get("enabled")),
                         "source": block.get("source") if kind == "search_result" else (block.get("source") or {})})
            counters[kind] += 1

        for m in self.messages:
            if m.get("role") != "user":
                continue
            for b in _blocks(m.get("content")):
                if b.get("type") in ("document", "search_result"):
                    add(b)
                elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                    for sub in b["content"]:
                        if isinstance(sub, dict) and sub.get("type") == "search_result":
                            add(sub)
        return docs

    def search(self, pattern: str, text: str | None = None, flags: int = re.IGNORECASE) -> re.Match | None:
        return re.search(pattern, self.conversation_text if text is None else text, flags)

    def mentions(self, *words: str, text: str | None = None) -> bool:
        haystack = (self.conversation_text if text is None else text).lower()
        return any(w.lower() in haystack for w in words)
