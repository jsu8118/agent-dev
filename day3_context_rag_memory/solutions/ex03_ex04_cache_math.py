"""Solutions to exercises 3 and 4 - prompt-caching arithmetic you can re-run with your own numbers.

Exercise 3: 5-minute vs 1-hour TTL for three traffic patterns (a TTL walk over request start times).
Exercise 4: the cost of a 20-turn agent with and without caching, and what one context-editing pass costs.

No API calls: prices come from labkit's model catalog (claude-opus-5: $5 / $25 per million tokens,
cache writes 1.25x (5 min) or 2x (1 h), cache reads 0.1x).

Run: python day3_context_rag_memory/solutions/ex03_ex04_cache_math.py
"""
# test: expect=Exercise 3
# test: expect=Exercise 4

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, header, step  # noqa: E402

import _day3 as d3  # noqa: E402

PREFIX = 20_000          # tokens: manuals + tools + system prompt, identical on every request


def ttl_walk(starts_min: list[float], ttl_min: float | None, write_mult: float) -> float:
    """Input-cost units (x prefix price) for requests at these start times. A read refreshes the entry; the entry
    lives `ttl` minutes from the START of the request that wrote or read it."""
    if ttl_min is None:
        return float(len(starts_min))
    units, expires = 0.0, -math.inf
    for t in sorted(starts_min):
        if t < expires:
            units += 0.1
        else:
            units += write_mult
        expires = t + ttl_min
    return units


def patterns() -> dict[str, list[float]]:
    shift = 10 * 60
    a = [i * 4.0 for i in range(shift // 4)]                                   # every 4 minutes
    b = [burst * 30 + j * 0.4 for burst in range(shift // 30) for j in range(6)]  # 6 in 2 min, every 30 min
    c = [0, 35, 110, 150, 230, 245, 330, 400, 455, 520, 560, 590]              # 12/day, irregular 15-85 min gaps
    return {"A: one every 4 min": a, "B: bursts of 6 every 30 min": b, "C: 12 irregular requests/day": c}


def exercise3() -> None:
    price = PREFIX * d3.input_price(MODEL)
    rows = []
    for name, starts in patterns().items():
        costs = {"none": ttl_walk(starts, None, 1), "5m": ttl_walk(starts, 5, 1.25), "1h": ttl_walk(starts, 60, 2.0)}
        best = min(costs, key=costs.get)
        rows.append([name, len(starts), *(d3.money(v * price) for v in costs.values()), best])
    d3.table(rows, ["pattern", "requests/day", "no cache", "5-min TTL", "1-hour TTL", "cheapest"])
    lam = 1 / 50
    for ttl, w in ((5, 1.25), (60, 2.0)):
        p_hit = 1 - math.exp(-lam * ttl)
        per_req = p_hit * 0.1 + (1 - p_hit) * w
        print(f"Pattern C as a Poisson process (mean gap 50 min): P(next request within {ttl} min) = {p_hit:.2f}, "
              f"so each request costs {per_req:.2f}x the prefix with the {ttl}-minute TTL (no cache: 1.00x).")


def loop_costs(turns: int, prefix: int, delta: int, out: int, clear_at: int | None = None,
               cleared: int = 0) -> tuple[int, float, float]:
    """(total input tokens, $ without caching, $ with automatic caching) for a `turns`-turn agent.

    Turn t sends prefix + (t-1)*delta tokens. With caching, turn t reads what turn t-1 sent and writes the rest.
    A clearing pass at turn `clear_at` removes `cleared` tokens from the middle of the history: everything after
    the first edited block must be written again (a cache miss), then later turns read the new, shorter prefix.
    """
    p_in, p_out = d3.input_price(MODEL), d3.output_price(MODEL)
    total, units, prev = 0, 0.0, 0
    for t in range(1, turns + 1):
        size = prefix + (t - 1) * delta - (cleared if clear_at and t >= clear_at else 0)
        total += size
        if t == 1:
            units += size * 1.25
        elif clear_at and t == clear_at:
            units += prefix * 0.1 + (size - prefix) * 1.25        # only the static prefix survives the edit
        else:
            units += prev * 0.1 + (size - prev) * 1.25
        prev = size
    return total, total * p_in + turns * out * p_out, units * p_in + turns * out * p_out


def exercise4() -> None:
    turns, prefix, delta, out = 20, 5_000, 1_200, 350
    total, plain, cached = loop_costs(turns, prefix, delta, out)
    print(f"{turns} turns, prefix {prefix:,}, +{delta:,} tokens per turn, {out} output tokens per turn, {MODEL}:")
    print(f"  input tokens sent: {turns} x {prefix:,} + {delta:,} x {turns}*{turns - 1}/2 = {total:,}")
    d3.table([["no caching", d3.money(plain)], ["automatic caching (5-min TTL)", d3.money(cached)],
              ["saving", f"{1 - cached / plain:.0%}"]], ["", "total cost"])
    _, _, edited = loop_costs(turns, prefix, delta, out, clear_at=12, cleared=8_000)
    print(f"  with one clearing pass at turn 12 that removes 8,000 tokens: {d3.money(edited)} "
          f"({'+' if edited > cached else ''}{(edited - cached) / cached:.0%} vs caching without it) - the pass "
          "re-writes the history after the static prefix once, then every later turn reads a shorter prefix.")


def main() -> None:
    header("Solutions 3 and 4 - caching arithmetic")
    step(3, f"Exercise 3 - 5-minute vs 1-hour TTL for a {PREFIX:,}-token prefix")
    exercise3()
    step(4, "Exercise 4 - a 20-turn agent with and without caching")
    exercise4()


if __name__ == "__main__":
    main()
