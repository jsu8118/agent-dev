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
from typing import Any


@dataclass
class Reply:
    content: list[dict] = field(default_factory=list)
    stop_reason: str | None = None          # inferred when None
    stop_details: dict | None = None
    thinking_summary: str | None = None     # shown only when thinking.display == "summarized"
    complexity: float = 0.5                 # 0..1, scales simulated thinking tokens

    def with_thinking(self, summary: str) -> "Reply":
        self.thinking_summary = summary
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
