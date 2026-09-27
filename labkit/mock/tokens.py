"""Deterministic token estimates for the mock API.

Real token counts come from Claude's tokenizer (use `client.messages.count_tokens`,
never an OpenAI tokenizer such as tiktoken).  The mock only needs numbers that are
stable and roughly proportional to text length so that cost, caching, and context-budget
lessons behave realistically offline.  ~3.8 characters per token is a fair average for
English prose; JSON and code tokenize less efficiently, which the JSON path reflects.
"""

from __future__ import annotations

import json
from typing import Any

CHARS_PER_TOKEN = 3.8
TOOL_USE_SYSTEM_OVERHEAD = 350     # the API adds a tool-use system prompt when `tools` is non-empty
MESSAGE_OVERHEAD = 4               # role markers per message
IMAGE_TOKENS = 1_500


def text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def json_tokens(value: Any) -> int:
    return text_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) + 2


def block_tokens(block: Any) -> int:
    """Estimate the tokens one content block contributes to the prompt."""
    if isinstance(block, str):
        return text_tokens(block)
    if not isinstance(block, dict):
        return json_tokens(block)
    kind = block.get("type")
    if kind == "text":
        return text_tokens(block.get("text", ""))
    if kind == "image":
        return IMAGE_TOKENS
    if kind == "document":
        source = block.get("source") or {}
        if source.get("type") == "text":
            return text_tokens(source.get("data", "")) + 10
        if source.get("type") == "content":
            return sum(block_tokens(b) for b in source.get("content") or []) + 10
        if source.get("type") == "base64":
            return round(len(source.get("data", "")) * 0.75 / CHARS_PER_TOKEN)
        return 200
    if kind == "tool_use":
        return 8 + json_tokens(block.get("input", {}))
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return 6 + text_tokens(content)
        if isinstance(content, list):
            return 6 + sum(block_tokens(b) for b in content)
        return 6
    if kind in ("thinking", "redacted_thinking"):
        # Prior-turn thinking is not re-read as input on current models; count it as ~0.
        return 0
    return json_tokens(block)


def content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return text_tokens(content)
    if isinstance(content, list):
        return sum(block_tokens(b) for b in content)
    return 0
