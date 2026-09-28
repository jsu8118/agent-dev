"""Builders that scenario policies use to describe what the stand-in model "says".

A policy returns a `Reply`; the renderer turns it into a genuine API-shaped Message
(ids, thinking block, usage, stop_reason, max_tokens truncation, SSE when streaming).

    return say("Your order shipped on 3 Sep.")                     # plain answer
    return use_tools(tool("lookup_order", order_id="SO-10045"),     # tool call(s)
                     preface="Let me look that up.")
    return json_reply({"category": "billing", "priority": "P3"})   # structured output
    return refuse("cyber")                                         # simulated refusal
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Reply:
    content: list[dict] = field(default_factory=list)
    stop_reason: str | None = None          # inferred when None
    stop_details: dict | None = None
    thinking_summary: str | None = None     # shown only when thinking.display == "summarized"
    complexity: float = 0.5                 # 0..1, scales simulated thinking tokens
    progress: list[str] | None = None       # progress updates before tool calls (thinking.display == "updates")
    continuation: Callable[[list[dict]], "Reply"] | None = None   # called with server-tool results (see run_code)
    container: dict | None = None           # set by the API when the reply used the code-execution container

    def with_thinking(self, summary: str) -> "Reply":
        self.thinking_summary = summary
        return self

    def then(self, continuation: Callable[[list[dict]], "Reply"]) -> "Reply":
        """What the model 'says' after the server tools in `content` have run (their results are passed in)."""
        self.continuation = continuation
        return self


def tool(name: str, **tool_input: Any) -> dict:
    """A tool_use block (the renderer assigns the id)."""
    return {"type": "tool_use", "name": name, "input": tool_input}


def say(text: str, *, thinking: str | None = None, complexity: float = 0.5) -> Reply:
    return Reply(content=[{"type": "text", "text": text}], thinking_summary=thinking, complexity=complexity)


def use_tools(*calls: dict, preface: str | None = None, thinking: str | None = None,
              complexity: float = 0.4) -> Reply:
    content: list[dict] = []
    if preface:
        content.append({"type": "text", "text": preface})
    content.extend(calls)
    return Reply(content=content, thinking_summary=thinking, complexity=complexity)


def json_reply(obj: Any, *, thinking: str | None = None, complexity: float = 0.3) -> Reply:
    return Reply(content=[{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}],
                 thinking_summary=thinking, complexity=complexity)


def refuse(category: str | None = "cyber", explanation: str | None = None) -> Reply:
    return Reply(content=[], stop_reason="refusal",
                 stop_details={"type": "refusal", "category": category, "explanation": explanation})


# ------------------------------------------------------------------------------- server tools
def search_tools(query: str, *, limit: int | None = None) -> dict:
    """A tool-search call (server tool). `query` is a regex for the regex variant, natural language for BM25;
    the renderer picks whichever search tool the request declared and fills in the results."""
    block: dict[str, Any] = {"type": "server_tool_use", "name": "tool_search", "input": {"query": query}}
    if limit is not None:
        block["input"]["limit"] = limit
    return block


def run_code(code: str) -> dict:
    """A Python cell for the code-execution container (programmatic tool calling when the code awaits tools).

    The API runs it before the reply continues: use `Reply(...).then(lambda results: say(...))` to write text that
    depends on the result (`results[-1]["content"]["stdout"]`).  When the code calls a client tool, the response
    pauses with `tool_use` blocks carrying a `caller`; the scenario is dispatched again once the results are in,
    with `req.completed_code` holding the finished cell.
    """
    return {"type": "server_tool_use", "name": "code_execution", "input": {"code": code}}


def bash(command: str) -> dict:
    """A shell command for the code-execution container (bash_code_execution)."""
    return {"type": "server_tool_use", "name": "bash_code_execution", "input": {"command": command}}


def create_file(path: str, file_text: str) -> dict:
    """Create a file in the container (text_editor_code_execution, command 'create')."""
    return {"type": "server_tool_use", "name": "text_editor_code_execution",
            "input": {"command": "create", "path": path, "file_text": file_text}}


def view_file(path: str) -> dict:
    return {"type": "server_tool_use", "name": "text_editor_code_execution", "input": {"command": "view", "path": path}}


def cite(doc: dict, quote: str) -> dict:
    """A citation object pointing at `quote` inside a document from `MockRequest.documents`.

    Mirrors the API: plain-text documents -> char_location; search results -> search_result_location;
    other documents -> content_block_location.
    """
    text = doc["text"]
    if doc["kind"] == "search_result":
        return {"type": "search_result_location", "cited_text": quote, "search_result_index": doc["index"],
                "source": doc.get("source") or "", "title": doc.get("title"), "start_block_index": 0,
                "end_block_index": 1}
    source_type = (doc.get("source") or {}).get("type")
    if source_type == "text":
        start = max(text.find(quote), 0)
        return {"type": "char_location", "cited_text": quote, "document_index": doc["index"],
                "document_title": doc.get("title"), "start_char_index": start, "end_char_index": start + len(quote)}
    return {"type": "content_block_location", "cited_text": quote, "document_index": doc["index"],
            "document_title": doc.get("title"), "start_block_index": 0, "end_block_index": 1}


def cited_text(text: str, citations: list[dict]) -> dict:
    """A text content block carrying citations (use inside Reply(content=[...]))."""
    return {"type": "text", "text": text, "citations": citations}
