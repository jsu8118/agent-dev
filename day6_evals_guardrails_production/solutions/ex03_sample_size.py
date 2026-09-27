"""Solution to exercise 3 - how many scenarios do you need to detect a 5-point regression?

Baseline pass rate 90%, candidate 85%, two-sided alpha = 0.05, power = 80%.
Computes the noise floor of today's 30-case set, the unpaired and paired (McNemar) sample sizes, and checks
the paired formula with a Monte-Carlo simulation that uses the same exact McNemar test as the lab harness.

Run
    python day6_evals_guardrails_production/solutions/ex03_sample_size.py
"""

# test: expect=paired design

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _evalkit import mcnemar_exact, wilson  # noqa: E402
from labkit import header, step  # noqa: E402

Z_ALPHA, Z_BETA = 1.959964, 0.841621          # two-sided 5%, power 80%


def unpaired_n(p1: float, p2: float) -> int:
    """Per-arm n for a two-proportion z-test (independent samples)."""
    pbar = (p1 + p2) / 2
    num = (Z_ALPHA * math.sqrt(2 * pbar * (1 - pbar)) + Z_BETA * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return math.ceil(num / (p1 - p2) ** 2)


def paired_n(p_fail_new: float, p_fix_new: float) -> int:
    """Scenarios for McNemar's test (Connor 1987): only discordant pairs carry information."""
    disc, delta = p_fail_new + p_fix_new, p_fail_new - p_fix_new
    return math.ceil((Z_ALPHA * math.sqrt(disc) + Z_BETA * math.sqrt(disc - delta ** 2)) ** 2 / delta ** 2)


def simulated_power(n: int, p_fail_new: float, p_fix_new: float, trials: int = 2000, seed: int = 6) -> float:
    rng = random.Random(seed)
    hits = 0
    for _ in range(trials):
        b = c = 0
        for _ in range(n):
            u = rng.random()
            if u < p_fail_new:
                b += 1                     # passed before, fails now
            elif u < p_fail_new + p_fix_new:
                c += 1                     # failed before, passes now
        hits += mcnemar_exact(b, c) < 0.05 and b > c
    return hits / trials


def main() -> None:
    header("Exercise 3 - sample size for detecting a 5-point regression (90% -> 85%)")

    step(1, "The noise floor of today's golden set")
    for n in (30, 100, 300):
        lo, hi = wilson(round(0.9 * n), n)
        print(f"  n={n:<4} 90% observed -> 95% CI {lo:.1%}-{hi:.1%} (half-width ~{(hi - lo) / 2:.1%})")
    print("  With 30 scenarios the interval is ~22 points wide: a 5-point drop is invisible in the aggregate.")

    step(2, "Unpaired design (two independent samples of scenarios)")
    print(f"  n per arm = {unpaired_n(0.90, 0.85)} scenarios  (1,372 runs in total)")

    step(3, "Paired design: the SAME scenarios before and after the change (McNemar)")
    for fail_new, fix_new, label in ((0.05, 0.00, "pure regression: 5% flip to fail, none flip to pass"),
                                     (0.06, 0.01, "churn: 6% flip to fail, 1% flip to pass"),
                                     (0.10, 0.05, "heavy churn: 10% / 5% (e.g. a model migration)")):
        n = paired_n(fail_new, fix_new)
        print(f"  {label:<55} n = {n:>4}   simulated power at n: {simulated_power(n, fail_new, fix_new):.0%}")
    print(f"  at today's n=30 (pure regression): power = {simulated_power(30, 0.05, 0.0):.0%} - an exact McNemar test "
          f"needs >= 6 one-way flips for p < 0.05 (p = {mcnemar_exact(6, 0):.3f}),\n  and 30 scenarios x 5% gives 1.5 on "
          "average. Simulated power sits a little under 80% because the exact test is conservative: add ~10%.")
    print("  The paired design needs 3-5x fewer scenarios than the unpaired one, because each scenario is its own\n"
          "  control; its cost is driven by how much the change CHURNS results, not by the pass rate.")

    step(4, "Repeated runs (reps) vs more scenarios")
    print("  Reps average out the model's sampling noise ON THESE scenarios - they sharpen 'did this change\n"
          "  regress our golden set?' but add no new scenarios, so they cannot tell you about unseen traffic.\n"
          "  Budget: grow the set toward ~150-250 paired scenarios (production failures first), and use 3 reps on\n"
          "  the high-stakes slice. In mock mode reps add nothing - the stand-in is deterministic.")

    step(5, "What that means for Kestrel's release gate")
    print("  1. Zero-tolerance per-case critical checks (they need no statistics: one unauthorised refund is a fail).\n"
          "  2. Aggregate pass-rate changes smaller than the noise floor trigger REVIEW, not BLOCK.\n"
          "  3. Grow the paired golden set until a 5-point drop is detectable (~155+ scenarios).")


if __name__ == "__main__":
    main()
