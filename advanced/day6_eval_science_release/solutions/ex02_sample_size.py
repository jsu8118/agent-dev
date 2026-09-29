"""Solution to exercise 2 - how many traces does it take to see a 5-point drop?

Every number here is closed-form arithmetic (normal approximations) checked by a quick simulation, so you can
redo it on paper: z-values, the two-proportion sample-size formula, its inverse (the minimum detectable effect),
Connor's formula for a paired design, a Bonferroni-corrected slice gate, and a one-sided non-inferiority test.

Run: python advanced/day6_eval_science_release/solutions/ex02_sample_size.py
"""
# test: expect=per arm
# test: expect=non-inferiority

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402

P1, P2 = 0.74, 0.69            # baseline success and the 5-point regression to detect
ALPHA, POWER = 0.05, 0.80


def power_by_simulation(p1: float, p2: float, n: int, trials: int = 4000, seed: int = 6) -> float:
    """Share of simulated A/B tests (n per arm) whose two-sided z-test rejects at ALPHA."""
    rng = random.Random(seed)
    hits = 0
    for _ in range(trials):
        k1 = sum(rng.random() < p1 for _ in range(n))
        k2 = sum(rng.random() < p2 for _ in range(n))
        hits += d6.two_proportion_test(k1, n, k2, n)[1] < ALPHA
    return hits / trials


def main() -> None:
    header("Exercise 2 - sample size for a 5-point drop at 80% power")
    za, zb = d6.z_quantile(1 - ALPHA / 2), d6.z_quantile(POWER)

    step(1, "(a) unpaired: traces per arm to detect 74% -> 69%")
    pbar = (P1 + P2) / 2
    a = za * math.sqrt(2 * pbar * (1 - pbar))
    b = zb * math.sqrt(P1 * (1 - P1) + P2 * (1 - P2))
    n = math.ceil((a + b) ** 2 / (P1 - P2) ** 2)
    print(f"  z(1 - alpha/2) = {za:.3f}, z(power) = {zb:.3f}, pooled p = {pbar:.3f}")
    print(f"  n = [ {za:.3f} x sqrt(2 x {pbar:.3f} x {1 - pbar:.3f}) + {zb:.3f} x sqrt({P1} x {1 - P1:.2f} + {P2} x {1 - P2:.2f}) ]^2 "
          f"/ {P1 - P2:.2f}^2")
    print(f"    = ({a:.4f} + {b:.4f})^2 / {(P1 - P2) ** 2:.4f} = {n:,} traces per arm "
          f"(helper: {d6.sample_size_two_proportions(P1, P2):,})")
    print(f"  check by simulation: power at n = {n:,} per arm is about {d6.pct(power_by_simulation(P1, P2, n, trials=600))}")

    step(2, "(b) the minimum detectable effect with 120 items per arm")
    mde = d6.mde_two_proportions(P1, 120)
    print(f"  solve n(74%, 74% - d) = 120 for d: d = {d6.pct(mde)} - a 120-item gate only sees a collapse.")
    print(f"  rule of thumb: MDE ~ (z_a + z_b) x sqrt(2 p (1 - p) / n) = {(za + zb) * math.sqrt(2 * P1 * (1 - P1) / 120):.1%}")

    step(3, "(c) paired: the same scenarios before and after, 6% flip to fail and 1% flip to pass")
    p10, p01 = 0.06, 0.01
    psi, delta = p10 + p01, p10 - p01
    n_pairs = math.ceil((za * math.sqrt(psi) + zb * math.sqrt(psi - delta ** 2)) ** 2 / delta ** 2)
    print(f"  discordant share psi = {psi:.2f}, net effect delta = {delta:.2f}")
    print(f"  n = [ {za:.3f} x sqrt({psi:.2f}) + {zb:.3f} x sqrt({psi:.2f} - {delta:.2f}^2) ]^2 / {delta:.2f}^2 = {n_pairs:,} "
          f"scenarios (helper: {d6.paired_sample_size(p10, p01):,})")
    print(f"  {n / n_pairs:.1f}x fewer than the unpaired design: only the scenarios that flip carry information.")

    step(4, "(d) a gate on EACH of 10 category slices, Bonferroni-corrected")
    per_slice = d6.sample_size_two_proportions(P1, P2, alpha=ALPHA / 10)
    print(f"  alpha per slice = {ALPHA / 10:.3f} -> {per_slice:,} traces per arm PER SLICE, {10 * per_slice:,} per arm in total.")
    print("  Per-slice statistical gates on a 5-point effect are out of reach; slices get monitors and critical checks.")

    step(5, "(e) one-sided non-inferiority: 'v16 is at most 5 points worse' when it is truly equal")
    zo = d6.z_quantile(1 - ALPHA)
    n_ni = math.ceil((zo + zb) ** 2 * 2 * P1 * (1 - P1) / 0.05 ** 2)
    print(f"  n = (z(0.95) + z(0.80))^2 x 2 p (1 - p) / margin^2 = ({zo:.3f} + {zb:.3f})^2 x 2 x {P1} x {1 - P1:.2f} / 0.05^2 "
          f"= {n_ni:,} per arm")
    print(f"  Proving 'no worse than' costs the same order as detecting a drop of the same size ({n_ni:,} vs {n:,} per\n"
          "  arm; the one-sided test is a little cheaper): the margin, not the direction, drives the sample size.")


if __name__ == "__main__":
    main()
