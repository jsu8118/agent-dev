"""Shared helpers for the Day 4 labs: per-pattern metering, a latency model, bounded concurrency.

Why these exist
    Day 4 compares *architectures* (single call vs chain vs router vs multi-agent), so every lab
    needs the same three numbers per architecture: tokens/cost, latency, and accuracy.
    labkit's LEDGER totals a whole process; `Tally` totals one architecture (or one phase) so
    two designs run in the same script can be compared side by side.

Latency: measured AND modelled
    Wall-clock is measured with `Timer`, but in mock mode every "model" answers in microseconds,
    so measured time says nothing about the architecture.  `modelled_seconds()` estimates what a
    call would take from its token counts (time-to-first-token + output tokens / decode speed),
    and `critical_path()` turns a set of calls into the time they take when run with N lanes of
    concurrency.  The per-model speeds below are ILLUSTRATIVE PLANNING ASSUMPTIONS, not published
    figures - replace them with the p50s you measure on your own traffic (in live mode the labs
    print both numbers so you can calibrate).
"""

from __future__ import annotations

import asyncio
import heapq
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from labkit import cost_usd, is_mock

# (seconds to first token, output tokens per second) - assumptions for the latency MODEL only.
LATENCY_ASSUMPTIONS: dict[str, tuple[float, float]] = {
    "claude-opus-5": (2.0, 55.0),
    "claude-opus-5-5": (2.0, 55.0),
    "claude-sonnet-5": (1.2, 75.0),
    "claude-haiku-4-5": (0.6, 140.0),
}
DEFAULT_LATENCY = (1.5, 60.0)
PREFILL_TOKENS_PER_S = 20_000.0          # uncached input is read fast, but not for free


def _usage(response: Any, name: str) -> int:
    usage = getattr(response, "usage", None)
    return int(getattr(usage, name, 0) or 0) if usage is not None else 0


def modelled_seconds(response: Any) -> float:
    """Estimated latency of one Messages API call from its usage (see module docstring)."""
    ttft, tps = LATENCY_ASSUMPTIONS.get(getattr(response, "model", ""), DEFAULT_LATENCY)
    uncached = _usage(response, "input_tokens") + _usage(response, "cache_creation_input_tokens")
    return ttft + uncached / PREFILL_TOKENS_PER_S + _usage(response, "output_tokens") / tps


@dataclass
class Tally:
    """Token, cost and latency totals for one architecture or phase."""

    label: str
    calls: int = 0
    input_tokens: int = 0            # everything the model read: uncached + cache writes + cache reads
    cache_read_tokens: int = 0
    output_tokens: int = 0           # includes thinking tokens
    cost_usd: float = 0.0
    modelled_s: float = 0.0          # sum of modelled call latencies (= wall-clock if run sequentially)
    by_model: Counter = field(default_factory=Counter)

    def add(self, response: Any) -> float:
        """Record one response; returns its modelled latency in seconds."""
        self.calls += 1
        self.cache_read_tokens += _usage(response, "cache_read_input_tokens")
        self.input_tokens += (_usage(response, "input_tokens") + _usage(response, "cache_read_input_tokens")
                              + _usage(response, "cache_creation_input_tokens"))
        self.output_tokens += _usage(response, "output_tokens")
        self.cost_usd += cost_usd(response.usage, response.model)
        seconds = modelled_seconds(response)
        self.modelled_s += seconds
        self.by_model[response.model] += 1
        return seconds

    def absorb(self, other: "Tally") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        self.modelled_s += other.modelled_s
        self.by_model.update(other.by_model)

    def models(self) -> str:
        return ", ".join(f"{m} x{n}" for m, n in sorted(self.by_model.items()))

    def line(self) -> str:
        return (f"{self.label}: calls={self.calls} in={self.input_tokens:,} (cache_read={self.cache_read_tokens:,}) "
                f"out={self.output_tokens:,} cost=${self.cost_usd:.4f}")


def critical_path(durations: Iterable[float], lanes: int) -> float:
    """Makespan of independent jobs on `lanes` parallel workers (greedy list scheduling, in order).

    lanes=1 gives the sequential sum; lanes>=len(jobs) gives max(job).  This is how a semaphore
    of size N shapes wall-clock: the slowest lane decides.
    """
    jobs = list(durations)
    if not jobs:
        return 0.0
    finish = [0.0] * max(1, min(lanes, len(jobs)))
    heapq.heapify(finish)
    for d in jobs:
        start = heapq.heappop(finish)
        heapq.heappush(finish, start + d)
    return max(finish)


class Timer:
    """Wall-clock stopwatch: `with Timer() as t: ...; t.seconds`."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.seconds = time.perf_counter() - self._start


async def gather_bounded(factories: list[Callable[[], Awaitable[Any]]], limit: int) -> list[Any]:
    """Run coroutine factories with at most `limit` in flight; failures come back as exception objects.

    Factories (not coroutines) so nothing starts before it holds a semaphore slot.  With
    return_exceptions=True one failed branch cannot cancel its siblings - the caller decides
    whether a partial result is acceptable.
    """
    semaphore = asyncio.Semaphore(limit)

    async def run(factory: Callable[[], Awaitable[Any]]) -> Any:
        async with semaphore:
            return await factory()

    return await asyncio.gather(*(run(f) for f in factories), return_exceptions=True)


def effort_kwargs(model: str, effort: str | None) -> dict:
    """output_config for `effort`, only on models that accept it (Haiku 4.5 rejects the parameter)."""
    from labkit import supports_effort

    if effort and supports_effort(model, effort):
        return {"output_config": {"effort": effort}}
    return {}


def mock_note(text: str) -> None:
    """Print a note that only applies to mock mode (the one mode-dependent thing labs may do)."""
    if is_mock():
        print(f"[mock] {text}")


def table(rows: list[list[Any]], headers: list[str], *, indent: str = "  ") -> str:
    """A plain fixed-width table (readable in terminals and CI logs)."""
    cells = [[str(h) for h in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    out = []
    for n, row in enumerate(cells):
        out.append(indent + "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
        if n == 0:
            out.append(indent + "  ".join("-" * w for w in widths))
    return "\n".join(out)


def text_blocks(message: Any) -> str:
    return "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text").strip()


def parsed(response: Any, what: str = "structured output") -> Any:
    """`response.parsed_output`, after checking stop_reason: a refusal or truncation has no valid JSON to parse."""
    if response.stop_reason == "refusal":
        raise RuntimeError(f"{what}: the model declined (stop_reason=refusal) - route this item to a human")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"{what}: no complete output (stop_reason={response.stop_reason}); raise max_tokens")
    return response.parsed_output
