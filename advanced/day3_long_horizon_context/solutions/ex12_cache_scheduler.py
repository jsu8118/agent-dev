"""Solution to exercise 12 - a pre-warming scheduler and lookback-aware breakpoint placement.

Objective
    Implement the starter's schedule() and place_breakpoints() and run its bench: twelve 07:00 requests from three
    depots (one shared prefix per depot), and one request that appends thirty checklist blocks to a conversation whose
    Harbor export is already cached.

Concepts
    grouping by the cache key's ingredients (model, system, tools); one max_tokens=0 pre-warm per group; waiting until
    the entry is readable before fanning out; the 20-position lookback as a hop constraint; the 4-breakpoint limit.

Run
    python advanced/day3_long_horizon_context/solutions/ex12_cache_scheduler.py

What to observe
    * Fire-everything: 12 of 12 requests write. Scheduled: 3 writes (the pre-warms), 12 reads, a fraction of the cost.
    * Without an intermediate breakpoint the 30-block request re-writes the export; with one it reads it.
    * Sixty blocks need more breakpoints than a request has slots: place_breakpoints says so instead of guessing.
"""
# test: expect=requests that wrote
# test: expect=needs

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import importlib.util
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ex12_starter", HERE.parents[0] / "exercises" / "ex12_cache_scheduler.py")
starter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(starter)

from labkit import get_client, is_mock  # noqa: E402

LOOKBACK = 20


def prefix_key(kw: dict) -> str:
    """The ingredients of the cached prefix: model, system and tools (cache_control markers do not matter)."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k != "cache_control"}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    raw = json.dumps([kw["model"], strip(kw.get("system")), strip(kw.get("tools"))], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def schedule(client, requests: list[dict]) -> list:
    groups: dict[str, list[int]] = {}
    for i, kw in enumerate(requests):
        groups.setdefault(prefix_key(kw), []).append(i)
    warmups = [client.messages.create(**dict(requests[idx[0]], max_tokens=0,
                                             messages=[{"role": "user", "content": "warmup"}]))
               for idx in groups.values()]
    if is_mock():
        time.sleep(starter.READY_DELAY)             # live: each max_tokens=0 call returned after prefill
    with cf.ThreadPoolExecutor(max_workers=len(requests)) as pool:
        responses = list(pool.map(lambda kw: client.messages.create(**kw), requests))
    return warmups + responses


def place_breakpoints(blocks: list[dict], gap: int, slots: int) -> list[dict]:
    """Greedy: put each intermediate breakpoint as far out as the previous entry can still be reached."""
    marks, reach = [], LOOKBACK - gap - 1           # block index b sits gap + b + 1 positions after the entry
    while len(blocks) - 1 > reach:                  # the automatic breakpoint on the last block must reach too
        marks.append(reach)
        reach += LOOKBACK
    if len(marks) > 4 - slots:
        raise ValueError(f"{len(blocks)} blocks after a {gap}-position gap needs {len(marks)} intermediate breakpoints "
                         f"but only {4 - slots} are free - split the turn into two requests")
    out = [dict(b) for b in blocks]
    for b in marks:
        out[b]["cache_control"] = {"type": "ephemeral"}
    return out


def main() -> None:
    starter.schedule, starter.place_breakpoints = schedule, place_breakpoints
    starter.main()
    if is_mock():
        print(f"[mock] A pre-warm's entry becomes readable {starter.READY_DELAY} s after it starts - the stand-in for "
              "time to first token. Live, wait for the max_tokens=0 response itself: it returns after prefill.")
    client = get_client()
    try:
        starter.lookback_case(client, 60, place_breakpoints)
    except ValueError as exc:
        print(f"A turn that appends 60 blocks: {exc}")


if __name__ == "__main__":
    main()
