"""Exercise 12 (starter) - A request scheduler that pre-warms shared prefixes and keeps turns inside the lookback.

Three depots start their technicians at 07:00; each depot's requests share one prefix (tools + a depot header + the
reference card). Fired all at once, every request writes its depot's prefix. And a turn that appends thirty checklist
blocks misses the previous cache entry, because a breakpoint looks back at most 20 positions.

Run
    python advanced/day3_long_horizon_context/exercises/ex12_cache_scheduler.py

TODO
    1. schedule(client, requests): group the requests by shared prefix (model, system, tools); for each group send one
       max_tokens=0 pre-warm with the same prefix, wait until its entry is readable (mock: READY_DELAY seconds; live:
       the pre-warm returns after prefill), then send the group's requests in parallel. Return every response.
    2. place_breakpoints(blocks, gap, slots): mark intermediate blocks with cache_control so that no breakpoint is
       more than 20 positions from the previous cache entry (`gap` = positions already between that entry and the
       first new block). Use at most `slots` breakpoints; if that is impossible, raise ValueError saying so.
"""

from __future__ import annotations

import concurrent.futures as cf
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, is_mock, mock_api  # noqa: E402

import _day3 as d3  # noqa: E402

READY_DELAY = 0.5
CARD = "\n\n".join(d3.read_manual_section(doc, sec) for doc, sec in [("IOM-KP250", "6"), ("IOM-KP250", "9"),
                                                                     ("IOM-KP400", "4"), ("IOM-KP400", "5"),
                                                                     ("UM-KC1", "3"), ("UM-KC1", "4")])
QUESTIONS = ["KP-250 baseplate bolt torque?", "KP-400 vibration limits?", "KP-250 regrease interval?",
             "What is F05 on a KC-1?"]


def depot_system(depot: str) -> list[dict]:
    return [{"type": "text", "text": f"<adv_day3_cache_lab>\nKestrel field reference, {depot} depot.\n\n{CARD}",
             "cache_control": {"type": "ephemeral"}}]


def requests_for_morning(day: str) -> list[dict]:
    return [dict(model=MODEL, max_tokens=1000, system=depot_system(f"{depot} ({day})"), tools=d3.FIELD_TOOLS,
                 messages=[{"role": "user", "content": q}]) for depot in ("North", "Harbor", "East") for q in QUESTIONS]


def naive_schedule(client, requests: list[dict]) -> list:
    with cf.ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(lambda kw: client.messages.create(**kw), requests))


def schedule(client, requests: list[dict]) -> list:
    """TODO 1 - pre-warm each shared prefix once, then fan out."""
    raise NotImplementedError("schedule")


def place_breakpoints(blocks: list[dict], gap: int, slots: int) -> list[dict]:
    """TODO 2 - intermediate breakpoints so every hop is <= 20 positions; returns a new list of blocks."""
    raise NotImplementedError("place_breakpoints")


def writes_reads(responses: list) -> tuple[int, int, int, float]:
    wrote = sum(1 for r in responses if r.usage.cache_creation_input_tokens)
    return (wrote, sum(r.usage.cache_creation_input_tokens for r in responses),
            sum(r.usage.cache_read_input_tokens for r in responses), sum(d3.response_cost(r) for r in responses))


def morning(client, scheduler, day: str) -> list:
    if is_mock():
        mock_api().cache.ready_delay = READY_DELAY
    try:
        return scheduler(client, requests_for_morning(day))
    finally:
        if is_mock():
            mock_api().cache.ready_delay = 0.0


def lookback_case(client, n_blocks: int, placer, tag: str = "a") -> tuple[int, int]:
    """A conversation with Harbor's export cached, then one request appending n checklist blocks."""
    system = depot_system("Harbor")
    log = {"type": "document", "title": "harbor", "source": {"type": "text", "media_type": "text/plain",
                                                             "data": d3.site_log("harbor")}}
    first = [{"role": "user", "content": [log, {"type": "text", "text": f"Harbor export, case {n_blocks}{tag}."}]}]
    r1 = client.messages.create(model=MODEL, max_tokens=1000, system=system, messages=first,
                                cache_control={"type": "ephemeral"})
    blocks = [{"type": "text", "text": f"Case {n_blocks}{tag}, checklist item {i + 1}: OK."} for i in range(n_blocks)]
    blocks = placer(copy.deepcopy(blocks), len(r1.content), 2)     # system + automatic already use two slots
    r2 = client.messages.create(model=MODEL, max_tokens=1000, system=system, cache_control={"type": "ephemeral"},
                                messages=first + [{"role": "assistant", "content": r1.content},
                                                  {"role": "user", "content": blocks}])
    return r2.usage.cache_read_input_tokens, r2.usage.cache_creation_input_tokens


def main() -> None:
    client = get_client()
    print("Exercise 12 - pre-warming scheduler and lookback-aware breakpoints")
    rows = [["fire everything at 07:00", *writes_reads(morning(client, naive_schedule, "Monday"))]]
    todo = []
    try:
        rows.append(["schedule(): pre-warm per prefix, then fan out", *writes_reads(morning(client, schedule,
                                                                                            "Tuesday"))])
    except NotImplementedError:
        todo.append("TODO 1: implement schedule()")
    rows = [[r[0], r[1], r[2], r[3], d3.money(r[4])] for r in rows]
    d3.table(rows, ["12 requests, 3 depots", "requests that wrote", "cache writes", "cache reads", "cost"])
    rows = [["no intermediate breakpoint", *lookback_case(client, 30, lambda b, g, s: b)]]
    try:
        rows.append(["place_breakpoints()", *lookback_case(client, 30, place_breakpoints, tag="b")])
    except NotImplementedError:
        todo.append("TODO 2: implement place_breakpoints()")
    d3.table(rows, ["30 checklist blocks in one request", "cache read", "cache write"])
    for line in todo:
        print(line)


if __name__ == "__main__":
    main()
