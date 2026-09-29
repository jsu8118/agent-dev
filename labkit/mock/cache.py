"""A faithful-in-spirit simulation of Claude's prompt cache.

How the real cache works (and what this simulates):

* The prompt is rendered in a fixed order: tools -> system -> messages.
* A `cache_control` breakpoint caches the prefix *up to and including* that block.
  The key is the exact bytes of that prefix (per model), so ANY change earlier in the
  prompt - a timestamp in the system prompt, a reordered tool list - is a miss for
  every breakpoint after it.
* On a request, each breakpoint looks back up to 20 positions for a prefix that an
  earlier request cached; the longest hit is read at ~0.1x price.  A run of consecutive
  tool_use blocks counts as one position, and so does a run of tool_result blocks.
* Tokens between the hit and the last breakpoint are written at 1.25x (5-minute TTL)
  or 2x (1-hour TTL).  Reads refresh the TTL.
* Prefixes shorter than the model's minimum (512-4096 tokens) silently don't cache.
* A top-level `cache_control` on the request = "automatic caching": one breakpoint on
  the last cacheable block, which moves forward as the conversation grows.
* An entry becomes readable only once the request that wrote it has started responding
  (`ready_delay`, off by default): N parallel requests with the same prefix all write.
* Tools declared with `defer_loading` are not part of the prefix until a `tool_reference`
  (from tool search) or a `tool_addition` surfaces them - at that position in `messages`,
  which is why discovery doesn't invalidate the cached prefix.
* A turn-scoped system message (`clear_at: "next_user_message"`) keeps its place in the
  prefix but renders nothing (0 tokens) once a later user message exists.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..models import ModelSpec
from .tokens import MESSAGE_OVERHEAD, TOOL_USE_SYSTEM_OVERHEAD, block_tokens, text_tokens

LOOKBACK = 20
DEFERRED_TOOL_PLACEHOLDER_TOKENS = 0


@dataclass
class CacheResult:
    total_tokens: int
    read_tokens: int = 0
    write_5m_tokens: int = 0
    write_1h_tokens: int = 0

    @property
    def write_tokens(self) -> int:
        return self.write_5m_tokens + self.write_1h_tokens

    @property
    def uncached_tokens(self) -> int:
        return max(self.total_tokens - self.read_tokens - self.write_tokens, 0)


def _canonical(block: Any) -> str:
    if isinstance(block, dict):
        block = {k: v for k, v in block.items() if k != "cache_control"}
    return json.dumps(block, ensure_ascii=False, separators=(",", ":"))


def _referenced_tools(block: dict) -> list[str]:
    """Names of deferred tools a block surfaces (tool search results, custom search results, tool additions)."""
    names: list[str] = []
    kind = block.get("type")
    if kind == "tool_search_tool_result":
        content = block.get("content") or {}
        for ref in content.get("tool_references") or []:
            if isinstance(ref, dict) and ref.get("type") == "tool_reference":
                names.append(ref.get("tool_name"))
    elif kind == "tool_result" and isinstance(block.get("content"), list):
        for sub in block["content"]:
            if isinstance(sub, dict) and sub.get("type") == "tool_reference":
                names.append(sub.get("tool_name"))
    elif kind == "tool_addition":
        ref = block.get("tool") or {}
        if ref.get("type") == "tool_reference":
            names.append(ref.get("name"))
    return [n for n in names if n]


def render_positions(body: dict) -> list[tuple[str, int, dict | None]]:
    """Flatten a request into cacheable positions: (canonical bytes, tokens, cache_control)."""
    positions: list[tuple[str, int, dict | None]] = []
    tools = body.get("tools") or []
    deferred = {t.get("name"): t for t in tools if isinstance(t, dict) and t.get("defer_loading")}
    loaded_tools = [t for t in tools if isinstance(t, dict) and not t.get("defer_loading")]
    if loaded_tools:
        positions.append(("<tool-use-system-prompt>", TOOL_USE_SYSTEM_OVERHEAD, None))
        for tool in loaded_tools:
            positions.append((_canonical(tool), text_tokens(_canonical(tool)), tool.get("cache_control")))
    system = body.get("system")
    if isinstance(system, str) and system:
        positions.append((system, text_tokens(system), None))
    elif isinstance(system, list):
        for block in system:
            positions.append((_canonical(block), block_tokens(block), block.get("cache_control")))

    messages = body.get("messages") or []
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    surfaced: set[str] = set()
    code_called = {b.get("id") for m in messages if m.get("role") == "assistant" and isinstance(m.get("content"), list)
                   for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("caller")}
    for m_index, message in enumerate(messages):
        role = message.get("role", "")
        content = message.get("content")
        cleared = role == "system" and message.get("clear_at") == "next_user_message" and m_index < last_user
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        if role == "system" and not blocks:      # an effort-only system message: a position, almost no tokens
            blocks = [{"type": "text", "text": ""}]
        run_kind: str | None = None
        for i, block in enumerate(blocks):
            marker = f"<{role}>" if i == 0 else ""
            tokens = block_tokens(block) + (MESSAGE_OVERHEAD if i == 0 else 0)
            if cleared:
                tokens = 0                       # still part of the prefix, but renders nothing
            if isinstance(block, dict) and ((block.get("type") == "tool_use" and block.get("caller"))
                                            or (block.get("type") == "tool_result" and block.get("tool_use_id") in code_called)):
                tokens = 0                       # programmatic tool calling: these go to the code cell, not the model
            control = block.get("cache_control") if isinstance(block, dict) else None
            if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                control = control or next((b.get("cache_control") for b in block["content"]
                                           if isinstance(b, dict) and b.get("cache_control")), None)
            expansions = ""
            if isinstance(block, dict):
                for name in _referenced_tools(block):
                    if name in deferred and name not in surfaced:
                        surfaced.add(name)                # the API expands the reference into the definition here
                        expansions += _canonical(deferred[name])
                        tokens += text_tokens(_canonical(deferred[name]))
                if block.get("type") == "tool_addition" and (block.get("tool") or {}).get("type") == "tool_definition":
                    tokens += text_tokens(_canonical(block["tool"].get("definition")))
            kind = block.get("type") if isinstance(block, dict) else "text"
            canonical = marker + _canonical(block) + expansions
            # A run of consecutive tool_use (or tool_result) blocks is ONE position for the lookback rule.
            if kind in ("tool_use", "tool_result") and kind == run_kind and positions:
                prev_canon, prev_tokens, prev_control = positions[-1]
                positions[-1] = (prev_canon + canonical, prev_tokens + tokens, control or prev_control)
            else:
                positions.append((canonical, tokens, control))
            run_kind = kind if kind in ("tool_use", "tool_result") else None
    return positions


class PromptCache:
    def __init__(self, clock: Callable[[], float] = time.time, ready_delay: float = 0.0) -> None:
        self._entries: dict[str, tuple[float, float, float]] = {}   # prefix hash -> (expires_at, ttl, ready_at)
        self._lock = threading.Lock()
        self._clock = clock
        self.ready_delay = ready_delay        # seconds until a freshly written entry can be read by other requests

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def process(self, body: dict, spec: ModelSpec) -> CacheResult:
        positions = render_positions(body)
        cumulative: list[int] = []
        hashes: list[str] = []
        running = 0
        digest = hashlib.sha256(spec.id.encode())
        for canonical, tokens, _ in positions:
            running += tokens
            cumulative.append(running)
            digest.update(canonical.encode("utf-8"))
            hashes.append(digest.copy().hexdigest())
        result = CacheResult(total_tokens=running)

        breakpoints: list[tuple[int, float]] = []
        for idx, (_, _, control) in enumerate(positions):
            if control:
                breakpoints.append((idx, 3600.0 if control.get("ttl") == "1h" else 300.0))
        auto = body.get("cache_control")
        if auto and positions:
            auto_idx = len(positions) - 1
            if all(b[0] != auto_idx for b in breakpoints):
                breakpoints.append((auto_idx, 3600.0 if auto.get("ttl") == "1h" else 300.0))
        if not breakpoints:
            return result
        breakpoints.sort()

        now = self._clock()
        with self._lock:
            hit_idx = -1
            for bp_idx, _ in breakpoints:
                for q in range(bp_idx, max(-1, bp_idx - LOOKBACK - 1), -1):
                    entry = self._entries.get(hashes[q])
                    if entry and entry[0] > now and entry[2] <= now:
                        hit_idx = max(hit_idx, q)
                        break
            if hit_idx >= 0:
                _, ttl, ready_at = self._entries[hashes[hit_idx]]
                self._entries[hashes[hit_idx]] = (now + ttl, ttl, ready_at)   # a read refreshes the TTL
                result.read_tokens = cumulative[hit_idx]

            written_upto = hit_idx
            for bp_idx, ttl in breakpoints:
                if bp_idx <= hit_idx or cumulative[bp_idx] < spec.cache_min_tokens:
                    continue
                existing = self._entries.get(hashes[bp_idx])
                if existing and existing[0] > now and existing[2] > now:
                    pass                          # another request is writing this prefix right now: we write too
                self._entries[hashes[bp_idx]] = (now + ttl, ttl, now + self.ready_delay)
                start = cumulative[written_upto] if written_upto >= 0 else 0
                segment = cumulative[bp_idx] - start
                if ttl >= 3600:
                    result.write_1h_tokens += segment
                else:
                    result.write_5m_tokens += segment
                written_upto = bp_idx
        return result
