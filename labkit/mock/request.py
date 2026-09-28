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

SERVER_TOOL_TYPES = ("tool_search_tool_regex_20251119", "tool_search_tool_bm25_20251119", "code_execution_20250825",
                     "code_execution_20260120", "code_execution_20260521")


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
    def __init__(self, body: dict, headers: dict | None = None, *, raw_body: dict | None = None) -> None:
        self.body = body
        self.raw_body = raw_body if raw_body is not None else body   # as the client sent it (before server edits)
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
        """Every tool in the request, deferred ones included (what the API knows, not what the model sees)."""
        return self.body.get("tools") or []

    @cached_property
    def tool_names(self) -> set[str]:
        return {t.get("name") for t in self.tools if t.get("name")}

    def has_tool(self, *names: str) -> bool:
        return any(n in self.tool_names for n in names)

    @cached_property
    def deferred_tool_names(self) -> set[str]:
        return {t.get("name") for t in self.tools if t.get("defer_loading") and t.get("name")}

    @cached_property
    def server_tools(self) -> dict[str, dict]:
        """Server tools by kind: {"tool_search": def, "code_execution": def}."""
        out: dict[str, dict] = {}
        for t in self.tools:
            kind = t.get("type") or ""
            if kind.startswith("tool_search_tool_"):
                out["tool_search"] = t
            elif kind.startswith("code_execution_"):
                out["code_execution"] = t
        return out

    @cached_property
    def loaded_tool_names(self) -> set[str]:
        """Tools the model can see right now: non-deferred ones, plus deferred ones discovered by tool search or
        surfaced by a tool_addition, minus tools removed by a tool_removal (later changes win)."""
        loaded = {t.get("name") for t in self.tools if t.get("name") and not t.get("defer_loading")}
        for m in self.messages:
            for b in _blocks(m.get("content")):
                kind = b.get("type")
                if kind == "tool_search_tool_result":
                    for ref in (b.get("content") or {}).get("tool_references") or []:
                        loaded.add(ref.get("tool_name"))
                elif kind == "tool_result" and isinstance(b.get("content"), list):
                    for sub in b["content"]:
                        if isinstance(sub, dict) and sub.get("type") == "tool_reference":
                            loaded.add(sub.get("tool_name"))
                elif kind == "tool_addition":
                    ref = b.get("tool") or {}
                    name = ref.get("name") if ref.get("type") == "tool_reference" else (ref.get("definition") or {}).get("name")
                    loaded.add(name)
                elif kind == "tool_removal":
                    loaded.discard((b.get("tool") or {}).get("name"))
        loaded.discard(None)
        return loaded

    def is_loaded(self, name: str) -> bool:
        return name in self.loaded_tool_names

    @cached_property
    def inline_tool_definitions(self) -> dict[str, dict]:
        """Tools defined inline in tool_addition blocks (inline-tools beta), by name."""
        out: dict[str, dict] = {}
        for m in self.messages:
            for b in _blocks(m.get("content")):
                if b.get("type") == "tool_addition" and (b.get("tool") or {}).get("type") == "tool_definition":
                    definition = b["tool"].get("definition") or {}
                    if definition.get("name"):
                        out[definition["name"]] = definition
        return out

    def tool_definition(self, name: str) -> dict | None:
        for t in self.tools:
            if t.get("name") == name:
                return t
        return self.inline_tool_definitions.get(name)

    @cached_property
    def code_callable_tools(self) -> dict[str, dict]:
        """Tools a code cell may call (allowed_callers includes a code execution version)."""
        return {t["name"]: t for t in self.tools
                if t.get("name") and any(str(c).startswith("code_execution") for c in t.get("allowed_callers") or [])}

    @property
    def container_id(self) -> str | None:
        container = self.body.get("container")
        if isinstance(container, dict):
            return container.get("id")
        return container if isinstance(container, str) else None

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
        """The effort in force for this turn: the latest per-message override, else the request's, else the default."""
        spec = self.spec
        default = spec.default_effort if spec else "high"
        level = self.output_config.get("effort") or default or "high"
        for m in self.messages:
            if m.get("role") == "system" and m.get("content") == [] and (m.get("output_config") or {}).get("effort"):
                level = m["output_config"]["effort"]
        return level

    @property
    def task_budget(self) -> int | None:
        budget = self.output_config.get("task_budget") or {}
        return budget.get("total") if isinstance(budget, dict) else None

    @cached_property
    def system_messages(self) -> list[str]:
        """Text of mid-conversation system messages the model sees now: persistent ones, plus turn-scoped
        (clear_at) ones that no later user message has cleared."""
        messages = self.messages
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        out: list[str] = []
        for i, m in enumerate(messages):
            if m.get("role") != "system":
                continue
            if m.get("clear_at") == "next_user_message" and i < last_user:
                continue
            text = "\n".join(b.get("text", "") for b in _blocks(m.get("content")) if b.get("type") == "text")
            if text.strip():
                out.append(text)
        return out

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

    # -- server tools in the history ---------------------------------------------------
    @cached_property
    def code_results(self) -> list[dict]:
        """Code-execution results in the history (python cells, bash, editor), oldest first."""
        out: list[dict] = []
        for m in self.messages:
            if m.get("role") != "assistant":
                continue
            for b in _blocks(m.get("content")):
                if b.get("type") in ("code_execution_tool_result", "bash_code_execution_tool_result",
                                     "text_editor_code_execution_tool_result"):
                    out.append(b)
        return out

    @property
    def completed_code(self) -> dict | None:
        """The code cell that just finished (set by the API when a paused cell resumes and completes), else None."""
        return getattr(self, "_completed_code", None)

    @cached_property
    def tool_search_queries(self) -> list[dict]:
        out: list[dict] = []
        for m in self.messages:
            if m.get("role") == "assistant":
                for b in _blocks(m.get("content")):
                    if b.get("type") == "server_tool_use" and str(b.get("name", "")).startswith("tool_search"):
                        out.append(b.get("input") or {})
        return out

    @property
    def pending_code_calls(self) -> list[ToolCall]:
        """Client tool calls made from code (tool_use blocks with a `caller`) answered by the last user message."""
        if not self.messages or self.messages[-1].get("role") != "user":
            return []
        answered = {b.get("tool_use_id") for b in _blocks(self.messages[-1].get("content")) if b.get("type") == "tool_result"}
        out: list[ToolCall] = []
        for m in self.messages:
            if m.get("role") != "assistant":
                continue
            for b in _blocks(m.get("content")):
                if b.get("type") == "tool_use" and b.get("caller") and b.get("id") in answered:
                    call = next((c for c in self.tool_calls if c.id == b.get("id")), None)
                    if call is not None:
                        out.append(call)
        return out

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
                elif source.get("type") == "file":
                    from .files import get_file_store
                    try:
                        stored = get_file_store().get(source.get("file_id", ""))
                        text = stored.text() if stored.is_text else ""
                    except Exception:
                        text = ""
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
