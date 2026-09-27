"""Turn a policy's `Reply` into an API-shaped Message (JSON or an SSE event stream)."""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
from typing import Any

from . import signing
from .cache import CacheResult
from .reply import Reply
from .request import MockRequest
from .tokens import CHARS_PER_TOKEN, json_tokens, text_tokens

# Simulated thinking volume per effort level (tokens at complexity 1.0).
EFFORT_THINKING_TOKENS = {"low": 60, "medium": 180, "high": 420, "xhigh": 800, "max": 1400}

_counter = itertools.count(1)
_counter_lock = threading.Lock()


def next_id(prefix: str) -> str:
    with _counter_lock:
        n = next(_counter)
    return f"{prefix}{n:06d}"


def _stable_tool_id(req: MockRequest, index: int, block: dict) -> str:
    seed = json.dumps([len(req.messages), index, block.get("name"), block.get("input"),
                       req.last_user_text[-200:], [c.id for c in req.tool_calls][-5:]], sort_keys=True, default=str)
    return signing.TOOL_ID_PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:20]


def _thinking_tokens(req: MockRequest, reply: Reply) -> int:
    thinking = req.thinking or {}
    if thinking.get("type") == "enabled":
        budget = int(thinking.get("budget_tokens", 1024))
        return max(64, int(budget * min(max(reply.complexity, 0.05), 1.0) * 0.5))
    base = EFFORT_THINKING_TOKENS.get(req.effort, 420)
    return max(16, int(base * min(max(reply.complexity, 0.05), 1.0)))


def build_message(req: MockRequest, reply: Reply, cache: CacheResult, *, model: str | None = None,
                  cleared_edits: list[dict] | None = None, compaction: dict | None = None) -> dict:
    content: list[dict] = []
    stop_reason = reply.stop_reason
    stop_sequence = None
    budget = req.max_tokens
    output_tokens = 0

    if compaction is not None:
        content.append(compaction)

    has_tool_use = any(b.get("type") == "tool_use" for b in reply.content)
    adaptive = (req.thinking or {}).get("type", "adaptive") == "adaptive"
    trivial = adaptive and reply.complexity < 0.15 and not has_tool_use   # adaptive thinking may skip easy turns

    if stop_reason != "refusal":
        if req.thinking_active and not trivial:
            thinking_budget = _thinking_tokens(req, reply)
            summary = ""
            if req.thinking_display == "summarized":
                summary = reply.thinking_summary or "Reviewing the request and the available context, then deciding on the next step."
            content.append({"type": "thinking", "thinking": summary, "signature": signing.sign(summary)})
            used = min(thinking_budget, budget)
            output_tokens += used
            budget -= used
            if budget <= 0:
                stop_reason = "max_tokens"

        stops = req.body.get("stop_sequences") or []
        for index, block in enumerate(reply.content):
            if stop_reason == "max_tokens":
                break
            if block["type"] == "text":
                text = block["text"]
                for seq in stops:
                    pos = text.find(seq)
                    if pos != -1:
                        text, stop_reason, stop_sequence = text[:pos], "stop_sequence", seq
                cost = text_tokens(text)
                if cost > budget:
                    text = text[: max(0, int(budget * CHARS_PER_TOKEN))]
                    cost, stop_reason = budget, "max_tokens"
                content.append({"type": "text", "text": text})
                output_tokens += cost
                budget -= cost
                if stop_reason == "stop_sequence":
                    break
            elif block["type"] == "tool_use":
                rendered = {"type": "tool_use", "id": _stable_tool_id(req, index, block),
                            "name": block["name"], "input": block.get("input") or {}}
                cost = json_tokens(rendered["input"]) + 8
                if cost > budget:
                    rendered["input"] = {}       # cut off mid-generation: never run a truncated call
                    cost, stop_reason = budget, "max_tokens"
                content.append(rendered)
                output_tokens += cost
                budget -= cost
            else:
                content.append(block)
                output_tokens += json_tokens(block)
        if stop_reason is None:
            stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"

    usage: dict[str, Any] = {
        "input_tokens": cache.uncached_tokens,
        "cache_creation_input_tokens": cache.write_tokens,
        "cache_read_input_tokens": cache.read_tokens,
        "cache_creation": {"ephemeral_5m_input_tokens": cache.write_5m_tokens,
                           "ephemeral_1h_input_tokens": cache.write_1h_tokens},
        "output_tokens": output_tokens,
        "service_tier": "standard",
    }
    message: dict[str, Any] = {
        "id": next_id("msg_mock_"),
        "type": "message",
        "role": "assistant",
        "model": model or req.model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": usage,
    }
    if stop_reason == "refusal":
        message["stop_details"] = reply.stop_details or {"type": "refusal", "category": None, "explanation": None}
    if cleared_edits:
        message["context_management"] = {"applied_edits": cleared_edits}
    return message


# ---------------------------------------------------------------------------- SSE
def _chunks(text: str, size: int = 24) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def to_sse(message: dict) -> bytes:
    """Encode a Message as the event stream the real API sends for `stream: true`."""
    out: list[str] = []

    def emit(event: str, data: dict) -> None:
        out.append(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

    usage = message["usage"]
    start = {k: v for k, v in message.items() if k not in ("content", "stop_reason", "stop_sequence",
                                                              "stop_details", "context_management")}
    start.update({"content": [], "stop_reason": None, "stop_sequence": None,
                  "usage": {**usage, "output_tokens": 1}})
    emit("message_start", {"type": "message_start", "message": start})
    emit("ping", {"type": "ping"})
    for index, block in enumerate(message["content"]):
        kind = block["type"]
        if kind == "text":
            emit("content_block_start", {"type": "content_block_start", "index": index,
                                         "content_block": {"type": "text", "text": ""}})
            for piece in _chunks(block["text"]):
                emit("content_block_delta", {"type": "content_block_delta", "index": index,
                                             "delta": {"type": "text_delta", "text": piece}})
        elif kind == "thinking":
            emit("content_block_start", {"type": "content_block_start", "index": index,
                                         "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            if block["thinking"]:
                for piece in _chunks(block["thinking"]):
                    emit("content_block_delta", {"type": "content_block_delta", "index": index,
                                                 "delta": {"type": "thinking_delta", "thinking": piece}})
            emit("content_block_delta", {"type": "content_block_delta", "index": index,
                                         "delta": {"type": "signature_delta", "signature": block["signature"]}})
        elif kind == "tool_use":
            emit("content_block_start", {"type": "content_block_start", "index": index,
                                         "content_block": {"type": "tool_use", "id": block["id"],
                                                           "name": block["name"], "input": {}}})
            for piece in _chunks(json.dumps(block["input"], ensure_ascii=False), 32):
                emit("content_block_delta", {"type": "content_block_delta", "index": index,
                                             "delta": {"type": "input_json_delta", "partial_json": piece}})
        else:
            emit("content_block_start", {"type": "content_block_start", "index": index, "content_block": block})
        emit("content_block_stop", {"type": "content_block_stop", "index": index})
    delta: dict[str, Any] = {"stop_reason": message["stop_reason"], "stop_sequence": message["stop_sequence"]}
    if message.get("stop_details"):
        delta["stop_details"] = message["stop_details"]
    emit("message_delta", {"type": "message_delta", "delta": delta,
                           "usage": {"output_tokens": usage["output_tokens"],
                                     "input_tokens": usage["input_tokens"],
                                     "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
                                     "cache_read_input_tokens": usage["cache_read_input_tokens"]}})
    emit("message_stop", {"type": "message_stop"})
    return "".join(out).encode("utf-8")
