"""Solutions to exercises 3 and 4 - the arithmetic of durability (no API calls).

Exercise 3 prices the three ways to get a crashed 40-turn run to its end - re-run it from scratch (with and
without prompt caching), or resume it from the event log (cache warm or cold) - and compares the storage and
rebuild reads of an event log with snapshots. Exercise 4 applies Little's law to Kestrel's copilot (runs in
flight and approvals parked at any instant), counts what deploys killed before the runtime was durable, and
chooses heartbeat, lease and sweeper intervals.

Prices (claude-opus-5): $5 / $25 per million input / output tokens; cache writes 1.25x (5-minute TTL), reads 0.1x.
Run: python advanced/day1_durable_agents/solutions/ex03_ex04_durability_math.py
"""
# test: expect=Resume from the log
# test: expect=Little's law

from __future__ import annotations

import math

from labkit import header, step

IN, OUT = 5.00 / 1e6, 25.00 / 1e6
READ, WRITE = 0.1 * IN, 1.25 * IN

# ---------------------------------------------------------------------------- exercise 3: the givens
P = 3_000          # tokens in the first request: tools + system (2,600) + the planner's message (400)
M0 = 400           # the planner's message (the only part of P that lives in the messages array)
D = 450            # tokens each turn adds to the history (assistant turn with the tool call + the tool result)
O = 250            # output tokens billed per turn (thinking included)
T = 40             # turns
CRASH = 38         # the worker dies after turn 38's tool result was logged
BYTES_PER_TOKEN = 4
META = 300         # bytes of metadata per logged event (ids, usage, timestamps)


def prompt(t: int) -> int:
    """Input tokens of the request for turn t."""
    return P + (t - 1) * D


def uncached(turns: range) -> tuple[int, float]:
    tokens = sum(prompt(t) for t in turns)
    return tokens, tokens * IN + len(turns) * O * OUT


def cached(turns: range, *, warm_prefix: int = 0) -> float:
    """Automatic caching: each request reads what the previous request sent and writes the rest.
    `warm_prefix` = tokens already in the cache before the first request of `turns`."""
    cost, cached_so_far = 0.0, warm_prefix
    for t in turns:
        read = min(cached_so_far, prompt(t))
        cost += read * READ + (prompt(t) - read) * WRITE + O * OUT
        cached_so_far = prompt(t)
    return cost


def money(x: float) -> str:
    return f"${x:,.4f}"


def exercise_3() -> None:
    step("3a", "Re-run from scratch, no caching")
    lost_tokens, lost = uncached(range(1, CRASH + 1))
    rerun_tokens, rerun = uncached(range(1, T + 1))
    print(f"  lost attempt (turns 1-{CRASH}): {lost_tokens:,} input tokens + {CRASH * O:,} output = {money(lost)}")
    print(f"  full re-run (turns 1-{T}):     {rerun_tokens:,} input tokens + {T * O:,} output = {money(rerun)}")
    print(f"  bill with the crash: {money(lost + rerun)} against {money(rerun)} uninterrupted "
          f"(+{(lost + rerun) / rerun - 1:.0%})")

    step("3b", "Re-run from scratch, automatic caching on every request")
    lost_c, rerun_c = cached(range(1, CRASH + 1)), cached(range(1, T + 1))
    print(f"  lost attempt {money(lost_c)} + re-run {money(rerun_c)} = {money(lost_c + rerun_c)} against "
          f"{money(rerun_c)} uninterrupted (+{(lost_c + rerun_c) / rerun_c - 1:.0%})")
    print(f"  (the re-run's own responses differ from the lost attempt's, so only the first {P:,} tokens could be "
          f"shared - at most {money(P * (WRITE - READ))} saved)")

    step("3c", "Resume from the log: only turns 39 and 40 are generated")
    warm = cached(range(CRASH + 1, T + 1), warm_prefix=prompt(CRASH))
    cold = cached(range(CRASH + 1, T + 1), warm_prefix=0)
    print(f"  cache warm (resumed within 5 minutes of the last request): {money(warm)} "
          f"= {warm / rerun_c:.0%} of an uninterrupted cached run")
    print(f"  cache cold (turn 39 re-writes {prompt(CRASH + 1):,} tokens at 1.25x):  {money(cold)} "
          f"= {cold / rerun_c:.0%}")
    print(f"  rebuilding the messages array from the log costs 0 tokens; re-running costs {money(rerun_c)} "
          f"cached, {money(rerun)} uncached")

    step("3d", "Storage: the log against snapshots of the messages array")
    log = (M0 * BYTES_PER_TOKEN + META) + T * (D * BYTES_PER_TOKEN + 3 * META)
    snapshot = lambda t: (M0 + D * t) * BYTES_PER_TOKEN           # the array after turn t, system and tools excluded
    every_turn = sum(snapshot(t) for t in range(1, T + 1))
    every_10 = sum(snapshot(t) for t in range(10, T + 1, 10))
    print(f"  event log:                        {log:>10,} bytes (grows linearly: {D * BYTES_PER_TOKEN + 3 * META:,} per turn)")
    print(f"  a snapshot after every turn:      {every_turn:>10,} bytes ({every_turn / log:.0f}x the log; grows with T^2)")
    print(f"  snapshots every 10 turns + log:   {every_10 + log:>10,} bytes")

    step("3e", "Rebuild reads at the crash")
    full = 2 + CRASH * 3                                          # run.created, run.status, 3 events per turn
    tail = 2 + (CRASH - 30) * 3                                   # after turn 30's logged response: its tool pair + turns 31-38
    print(f"  full log: {full} events; latest snapshot (turn 30) + tail: 1 row + {tail} events = {tail + 1} rows")
    print("  (exercise 12 measures 116 and 27 on the real run.) Either way it is milliseconds against seconds for one "
          "model call: snapshots are for logs of thousands of events, not for a 40-turn run.")


# ---------------------------------------------------------------------------- exercise 4: the givens
TICKETS = 1_900                  # per month
BUSINESS_HOURS = 21 * 10         # per month
ACTIVE_S = 40                    # seconds a worker is busy with a run, on average
APPROVAL_SHARE, APPROVAL_WAIT_H = 0.12, 18
DEPLOYS = 3 * 21                 # per month, rolling, 30 s grace period
H, TTL, SWEEP = 10, 30, 60       # heartbeat, lease TTL, sweeper interval (seconds)
SLOW_SHARE, GET_ORDER_SHARE = 0.002, 0.60     # ERP calls slower than 30 s; runs that call get_order


def exercise_4() -> None:
    step("4a", "Little's law: L = lambda x W")
    lam_h = TICKETS / BUSINESS_HOURS
    active = lam_h / 3600 * ACTIVE_S
    parked = lam_h * APPROVAL_SHARE * APPROVAL_WAIT_H
    print(f"  arrivals: {TICKETS:,} / {BUSINESS_HOURS} business hours = {lam_h:.2f} runs per hour")
    print(f"  runs a worker is busy with at any instant: {lam_h:.2f}/3600 x {ACTIVE_S} s = {active:.3f}")
    print(f"  runs parked on an approval at any instant: {lam_h:.2f} x {APPROVAL_SHARE:.0%} x {APPROVAL_WAIT_H} h = {parked:.1f}")

    step("4b", "Before durability: what the deploys killed")
    killed = DEPLOYS * active
    p_one = 1 - math.exp(-active)
    gap_h = 10 / 3
    print(f"  {DEPLOYS} deploys x {active:.3f} busy runs = {killed:.1f} in-flight runs killed per month "
          f"(a given deploy hits at least one {p_one:.0%} of the time)")
    print(f"  approvals held in worker memory: a deploy every {gap_h:.1f} business hours against waits of "
          f"{APPROVAL_WAIT_H} h - P(no deploy during a wait) = e^(-{APPROVAL_WAIT_H}/{gap_h:.2f}) = "
          f"{math.exp(-APPROVAL_WAIT_H / gap_h):.1%}: practically all {APPROVAL_SHARE * TICKETS:.0f} approvals a month")

    step("4c", "With leases: time from a kill -9 to the resume")
    print(f"  the lease runs out {TTL} s after the last heartbeat; the sweeper finds it within the next {SWEEP} s: "
          f"{TTL}-{TTL + SWEEP} s. A graceful SIGTERM releases the lease: the next poll takes it at once.")

    step("4d", "False takeovers from a slow ERP call without in-tool heartbeats")
    takeovers = TICKETS * GET_ORDER_SHARE * SLOW_SHARE
    print(f"  {TICKETS:,} x {GET_ORDER_SHARE:.0%} x {SLOW_SHARE:.1%} = {takeovers:.1f} false takeovers per month, each "
          "re-generating the rest of the run - and, live, minting new tool_use ids (new idempotency keys) for "
          "every later write")

    step("4e", "Choosing the numbers")
    slowest, budget = 60, 120
    ttl = 75
    sweep = budget - ttl - 15
    print(f"  requirement: no false takeover from a {slowest} s step without heartbeats -> TTL > {slowest} s; resume a "
          f"dead worker within {budget} s -> TTL + sweep <= {budget} s")
    print(f"  one answer: TTL {ttl} s, sweeper every {sweep} s, heartbeat every {ttl // 3} s -> resumed "
          f"{ttl}-{ttl + sweep} s after the last heartbeat")
    print(f"  the better answer: heartbeat from inside the slow tool and keep TTL {TTL} s ({TTL}-{TTL + SWEEP} s "
          "with a 60 s sweeper; fence writes for the tail you did not measure)")


def main() -> None:
    header("Exercises 3 and 4 - the arithmetic of durability")
    print(f"Prices: $5 / $25 per MTok, cache write 1.25x, read 0.1x. Run: P={P:,}, D={D}, O={O}, T={T}, crash after "
          f"turn {CRASH}.")
    exercise_3()
    exercise_4()


if __name__ == "__main__":
    main()
