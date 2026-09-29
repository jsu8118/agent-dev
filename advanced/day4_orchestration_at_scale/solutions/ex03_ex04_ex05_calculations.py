"""Solutions to exercises 3, 4 and 5 - the arithmetic of splitting work, budgeting a swarm and fanning out.

Every number in solutions/README.md for these three exercises is printed here, from the formulas stated there.
Prices: claude-opus-5 $5 / $25 per million input / output tokens; cache write 1.25x, cache read 0.1x.

Run: python advanced/day4_orchestration_at_scale/solutions/ex03_ex04_ex05_calculations.py
"""
# test: expect=break-even
# test: expect=pre-warm

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day4 as d4  # noqa: E402

P, H, K = 1_800, 600, 8_000                     # exercise 3's model
PROFILES = {"tuned": {"b": 100, "r": 150, "c": 150}, "naive": {"b": 1_000, "r": 600, "c": 600}}
INPUT = 5.0 / 1_000_000                         # $ per input token on claude-opus-5


# ------------------------------------------------------------------------------ exercise 3
def tokens_one_agent(n: int) -> float:
    return 3 * n * P + 1.5 * H * n * (n - 1)


def tokens_swarm(n: int, b: int, r: int, c: int) -> float:
    return 3 * n * (P + b) + K + c * n + r * n


def cost_one_agent(n: int) -> float:
    """Input-token equivalents with caching: first writes at 1.25, re-reads at 0.1."""
    written = P + H * n
    return 1.25 * written + 0.1 * (tokens_one_agent(n) - written)


def cost_swarm(n: int, b: int, r: int, c: int) -> float:
    written = P + n * b + K + c * n
    reread = 3 * n * (P + b) - P - n * b
    return 1.25 * written + 0.1 * reread + 5 * r * n


def break_even(f_single, f_swarm) -> int:
    return next(n for n in range(1, 1_000) if f_swarm(n) < f_single(n))


def exercise3() -> None:
    step(3, "Exercise 3 - when does splitting the work pay?")
    for name, p in PROFILES.items():
        coeff = 3 * p["b"] + p["c"] + p["r"]
        a, bq, cq = 1.5 * H, -(1.5 * H + coeff), -K
        root = (-bq + math.sqrt(bq * bq - 4 * a * cq)) / (2 * a)
        n_tokens = break_even(tokens_one_agent, lambda n: tokens_swarm(n, **p))
        n_cost = break_even(cost_one_agent, lambda n: cost_swarm(n, **p))
        print(f"  {name}: tokens {1.5 * H:.0f}n^2 - {-bq:.0f}n - {K} >= 0 -> root {root:.2f}, break-even n = {n_tokens}; "
              f"with caching, break-even n = {n_cost}")
    print(f"  {'n':>4}  {'one agent tokens':>16}  {'tuned tokens':>12}  {'naive tokens':>12}  {'one agent $eq':>13}  "
          f"{'tuned $eq':>9}  {'naive $eq':>9}")
    for n in (1, 3, 4, 7, 11, 14, 30, 54, 80):
        print(f"  {n:>4}  {tokens_one_agent(n):>16,.0f}  {tokens_swarm(n, **PROFILES['tuned']):>12,.0f}  "
              f"{tokens_swarm(n, **PROFILES['naive']):>12,.0f}  {cost_one_agent(n):>13,.0f}  "
              f"{cost_swarm(n, **PROFILES['tuned']):>9,.0f}  {cost_swarm(n, **PROFILES['naive']):>9,.0f}")
    n = 11
    print(f"  at n = 11 with caching: tuned swarm {cost_swarm(n, **PROFILES['tuned']) / cost_one_agent(n):.2f}x one agent, naive "
          f"{cost_swarm(n, **PROFILES['naive']) / cost_one_agent(n):.2f}x; tokens: one agent "
          f"{tokens_one_agent(n) / tokens_swarm(n, **PROFILES['tuned']):.2f}x the tuned swarm")
    print("  lab 07 at n = 11: tokens 186,860 vs 93,764 (1.99x); dollars as run $0.3548 / $0.3386 = 1.05x (tuned), "
          "$0.7267 / $0.3386 = 2.15x (naive)")


# ------------------------------------------------------------------------------ exercise 4
def exercise4() -> None:
    step(4, "Exercise 4 - a budget for the swarm")
    cap, worst, coordinator, estimate = 25.00, 0.036, 0.05, 0.045
    units = math.floor((cap - coordinator) / worst)
    print(f"  a. ({cap:.2f} - {coordinator:.2f}) / {worst} = {(cap - coordinator) / worst:.1f} -> {units} units at the worst cost")
    for workers in (8, 25):
        print(f"  b. naive gate, {workers} workers: up to {workers} tasks start while spent < cap -> overshoot < "
              f"{workers} x ${worst} = ${workers * worst:.3f}")
    sequential = 1 + math.floor((cap - coordinator - estimate) / worst)
    stranded = cap - coordinator - sequential * worst
    print(f"  c. admission control, one worker: 1 + floor(({cap:.2f} - {coordinator:.2f} - {estimate}) / {worst}) = "
          f"{sequential} units; ${stranded:.3f} left unspent (< one estimate)")
    for workers in (8, 25):
        held = workers * (estimate - worst)
        print(f"     with {workers} workers the in-flight reservations hold up to {workers} x (${estimate} - ${worst}) = "
              f"${held:.3f} more at the stop: ~{math.floor((cap - coordinator - held - estimate) / worst) + 1} units, never over the cap")
    print("  d. session budget, 25 threads, $0.02 max per request: the gate runs before each request and the request that "
          f"crosses the cap completes -> overshoot <= 25 x $0.02 = ${25 * 0.02:.2f}")


# ------------------------------------------------------------------------------ exercise 5
def exercise5() -> None:
    step(5, "Exercise 5 - fanning out against a cold cache")
    prefix = 3_000
    write, read = 1.25 * prefix * INPUT, 0.1 * prefix * INPUT
    for workers in (11, 7):
        cold = workers * write
        warm = write + workers * read
        print(f"  {workers} workers: all write together {workers} x ${write:.5f} = ${cold:.4f}; pre-warm (one max_tokens=0 "
              f"write) + {workers} reads = ${write:.5f} + {workers} x ${read:.5f} = ${warm:.4f} (saves {1 - warm / cold:.0%})")
    n = 1.25 / (1.25 - 0.1)
    print(f"  pre-warm pays when 1.25N > 1.25 + 0.1N, i.e. N > {n:.2f}: from 2 concurrent workers - at the price of one "
          "extra round trip before the fan-out")
    print(f"  minimum cacheable prefix on {d4.MODEL}: {__import__('labkit').get_spec(d4.MODEL).cache_min_tokens} tokens "
          "(a 3,000-token prefix qualifies)")


def main() -> None:
    header("Exercises 3, 4 and 5 - the arithmetic")
    exercise3()
    exercise4()
    exercise5()


if __name__ == "__main__":
    main()
