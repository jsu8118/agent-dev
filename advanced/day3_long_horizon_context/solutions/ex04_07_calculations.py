"""Solutions to exercises 4-7 - the arithmetic behind lookback misses, compaction cadence, pre-warming and budgets.

Objective
    Compute every number the worked solutions quote, from the prices and traffic stated in the exercises, so the
    arithmetic can be checked and re-run with your own traffic. No API calls.

Concepts
    cache write 1.25x (5-minute) / 2x (1-hour), read 0.1x (0.05x Opus 5.5, 0.025x Fable 5.1); the cost of a lookback
    miss; the cost curve of compaction cadence; pre-warming a fan-out; keep-alive vs the 1-hour TTL; task-budget sizing.

Run
    python advanced/day3_long_horizon_context/solutions/ex04_07_calculations.py

What to observe
    * Exercise 4: one miss at turn 20 costs more than a normal turn; batching or automatic caching removes all six.
    * Exercise 5: the cheapest cadence is not the most frequent one - each compaction has a price.
    * Exercise 6: the keep-alive beats the 1-hour TTL on Fable 5.1 for gaps under an hour.
    * Exercise 7: the budget, and the `remaining` value after each summary reset.
"""
# test: expect=Exercise 4
# test: expect=Exercise 7

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day3 as d3  # noqa: E402

M = 1_000_000


def money(x: float) -> str:
    return d3.money(x)


# ------------------------------------------------------------------------------------------------ exercise 4
def exercise4() -> None:
    price, write, read = 5 / M, 1.25, 0.1                        # Claude Opus 5
    P, D, T = 5_000, 1_500, 40
    lookup_turns = [5, 10, 15, 20, 25, 30]

    def context(t: int) -> int:                                  # conversation after turn t
        return P + D * t

    def turn_cost(t: int, miss: bool) -> float:                  # the first request of turn t (one request per turn)
        if miss:                                                 # reads only the system entry, re-writes the rest
            return (P * read + (context(t - 1) - P + D) * write) * price
        return (context(t - 1) * read + D * write) * price

    normal = sum(turn_cost(t, False) for t in range(1, T + 1))
    misses = [(t + 1, turn_cost(t + 1, True) - turn_cost(t + 1, False)) for t in lookup_turns]
    print("Exercise 4 - lookback misses after sequential look-up turns (input side, Opus 5)")
    print(f"  normal turn 21: read {context(20):,} x 0.1 + write {D:,} x 1.25 = {money(turn_cost(21, False))}")
    print(f"  missed turn 21: read {P:,} x 0.1 + write {context(20) - P + D:,} x 1.25 = {money(turn_cost(21, True))}")
    print(f"  extra per miss = (C(t) - P) x (1.25 - 0.1) x $5/MTok; at turn 21: {money(misses[3][1])}")
    d3.table([[t, money(extra)] for t, extra in misses] + [["six misses", money(sum(e for _, e in misses))]],
             ["turn after a look-up", "extra cost"])
    print(f"  session input cost without misses {money(normal)}; with them {money(normal + sum(e for _, e in misses))} "
          f"(+{sum(e for _, e in misses) / normal:.0%}). Batching the eight look-ups into one parallel round, or "
          "automatic caching, brings the extra to $0.")


# ------------------------------------------------------------------------------------------------ exercise 5
def exercise5() -> list[list]:
    price, out_price, write, read = 5 / M, 25 / M, 1.25, 0.1
    P, D, S, T, W = 5_000, 1_600, 600, 40, 30_000

    def day(k: int) -> tuple[float, int, int]:
        cost, peak, forks = 0.0, 0, 0
        base, i = P, 0                                           # base = the prefix a segment starts from
        for t in range(1, T + 1):
            i += 1
            prompt = base + D * i
            peak = max(peak, prompt)
            if i == 1 and base > P:                              # first request after a reset: read P, write S + D
                cost += (P * read + (S + D) * write) * price
            else:
                cost += ((prompt - D) * read + D * write) * price
            if i == k and t < T:                                 # compact: a fork reads the context, writes S
                cost += prompt * read * price + S * out_price
                forks += 1
                base, i = P + S, 0
        return cost, peak, forks

    rows = []
    for k in (3, 5, 10, 15, 40):
        cost, peak, forks = day(k)
        rows.append([k if k < 40 else "never", forks, peak, "yes" if peak <= W else "no", money(cost)])
    print("\nExercise 5 - compaction cadence (own summary every k turns; input + summary output, Opus 5)")
    print(f"  largest k under W = {W:,}: (W - (P + S)) / D = ({W:,} - {P + S:,}) / {D:,} = "
          f"{(W - P - S) / D:.2f} -> k = {(W - P - S) // D}")
    d3.table(rows, ["compact every k turns", "summaries", "peak context", f"under {W:,}?", "cost per day"])
    return rows


# ------------------------------------------------------------------------------------------------ exercise 6
def exercise6() -> None:
    print("\nExercise 6 - pre-warming and keep-alive")
    price, prefix, n = 5 / M, 6_000, 8                           # Opus 5, one depot
    cold = n * prefix * 1.25 * price
    warm = prefix * 1.25 * price + n * prefix * 0.1 * price
    print(f"  (a) 8-way fan-out over a {prefix:,}-token prefix: cold {money(cold)} (8 writes), pre-warmed "
          f"{money(warm)} (1 write + 8 reads): {money(cold - warm)} per depot-morning, "
          f"{money((cold - warm) * 5 * 250)} per year for 5 depots x 250 days")
    fable, ctx = 10 / M, 30_000                                  # Fable 5.1, a 30K conversation
    rows = []
    for gap in (20, 30, 45, 70):
        pings = gap // 4.5 if gap > 5 else 0                    # a max_tokens=0 re-send every 4.5 minutes
        keepalive = pings * ctx * 0.025 * fable
        rewrite = ctx * (1.25 - 0.025) * fable                   # the entry expired: re-write instead of read
        one_hour = ctx * (2.0 - 1.25) * fable if gap < 60 else rewrite + ctx * (2.0 - 1.25) * fable
        rows.append([f"{gap} min", int(pings), money(keepalive), money(rewrite), money(one_hour)])
    d3.table(rows, ["idle gap", "keep-alive pings", "5-min + keep-alive", "5-min, let it expire",
                    "1-hour TTL premium"])
    print("  '1-hour TTL premium' = what writing the 30K context at 2x instead of 1.25x costs (the entry then "
          "survives gaps under an hour; past an hour it expires as well). Reads at 0.025x make pings nearly free.")


# ------------------------------------------------------------------------------------------------ exercise 7
def exercise7() -> None:
    print("\nExercise 7 - sizing a task budget (lab 01's measured day)")
    output, results = 10_852, 54_768
    spend = output + results
    print(f"  measured spend: {output:,} output + {results:,} tool results = {spend:,} tokens")
    print(f"  budget with a 30% margin: {spend:,} x 1.3 = {round(spend * 1.3):,} -> set 90,000 (a round number above it)")
    per_site = {"gbwd": 19_169, "harbor": 18_188, "riverbend": 3_268, "cedar": 2_879, "cobalt": 19_037,
                "westfield": 3_079}                                # lab 01, budget-100k arm: output + tool results
    total, spent = 90_000, 0
    rows = []
    for site, s in per_site.items():
        spent += s
        rows.append([site, s, spent, total - spent])
    d3.table(rows, ["after site", "spent at the site", "spent so far", "remaining to pass after the reset"])
    print("  After a summary reset the history no longer shows earlier spend, so the next request passes "
          "task_budget.remaining = total - spent so far; without it the model would see a fresh 90,000.")


def main() -> None:
    exercise4()
    exercise5()
    exercise6()
    exercise7()


if __name__ == "__main__":
    main()
