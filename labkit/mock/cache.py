"""A faithful-in-spirit simulation of Claude's prompt cache.

How the real cache works (and what this simulates):

* The prompt is rendered in a fixed order: tools -> system -> messages.
* A `cache_control` breakpoint caches the prefix *up to and including* that block.
  The key is the exact bytes of that prefix (per model), so ANY change earlier in the
  prompt - a timestamp in the system prompt, a reordered tool list - is a miss for
  every breakpoint after it.
* On a request, each breakpoint looks back up to 20 positions for a prefix that an
  earlier request cached; the longest hit is read at ~0.1x price.
* Tokens between the hit and the last breakpoint are written at 1.25x (5-minute TTL)
  or 2x (1-hour TTL).  Reads refresh the TTL.
* Prefixes shorter than the model's minimum (512-4096 tokens) silently don't cache.
* A top-level `cache_control` on the request = "automatic caching": one breakpoint on
  the last cacheable block, which moves forward as the conversation grows.
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


def render_positions(body: dict) -> list[tuple[str, int, dict | None]]:
    """Flatten a request into cacheable positions: (canonical bytes, tokens, cache_control)."""
    positions: list[tuple[str, int, dict | None]] = []
    tools = body.get("tools") or []
    if tools:
        positions.append(("<tool-use-system-prompt>", TOOL_USE_SYSTEM_OVERHEAD, None))
        for tool in tools:
            positions.append((_canonical(tool), text_tokens(_canonical(tool)), tool.get("cache_control")))
    system = body.get("system")
    if isinstance(system, str) and system:
        positions.append((system, text_tokens(system), None))
    elif isinstance(system, list):
        for block in system:
            positions.append((_canonical(block), block_tokens(block), block.get("cache_control")))
    for message in body.get("messages") or []:
        role = message.get("role", "")
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        for i, block in enumerate(blocks):
            marker = f"<{role}>" if i == 0 else ""
            tokens = block_tokens(block) + (MESSAGE_OVERHEAD if i == 0 else 0)
            control = block.get("cache_control") if isinstance(block, dict) else None
            if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                control = control or next((b.get("cache_control") for b in block["content"]
                                           if isinstance(b, dict) and b.get("cache_control")), None)
            positions.append((marker + _canonical(block), tokens, control))
    return positions


class PromptCache:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._entries: dict[str, tuple[float, float]] = {}   # prefix hash -> (expires_at, ttl_seconds)
        self._lock = threading.Lock()
        self._clock = clock

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
                    if entry and entry[0] > now:
                        hit_idx = max(hit_idx, q)
                        break
            if hit_idx >= 0:
                ttl = self._entries[hashes[hit_idx]][1]
                self._entries[hashes[hit_idx]] = (now + ttl, ttl)   # a read refreshes the TTL
                result.read_tokens = cumulative[hit_idx]

            written_upto = hit_idx
            for bp_idx, ttl in breakpoints:
                if bp_idx <= hit_idx or cumulative[bp_idx] < spec.cache_min_tokens:
                    continue
                self._entries[hashes[bp_idx]] = (now + ttl, ttl)
                start = cumulative[written_upto] if written_upto >= 0 else 0
                segment = cumulative[bp_idx] - start
                if ttl >= 3600:
                    result.write_1h_tokens += segment
                else:
                    result.write_5m_tokens += segment
                written_upto = bp_idx
        return result
