"""Solution to exercise 3 - pass@k and pass^k from repeated runs of five agentic scenarios.

The unbiased estimators from c passes in n runs: pass@k = 1 - C(n-c, k) / C(n, k) (at least one of k attempts
passes), pass^k = C(c, k) / C(n, k) (all k pass). The plug-in (c/n)^k is biased upwards for pass^k on small n,
and the pooled rate raised to k is a different number again - it hides which scenario fails.

Run: python advanced/day6_eval_science_release/solutions/ex03_pass_k.py
"""
# test: expect=pass^3

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402

RUNS = {"S1 order status": 10, "S2 return, eligible": 9, "S3 warranty with photos": 8, "S4 two-issue email": 6,
        "S5 return past the window": 3}
N, K = 10, 3


def main() -> None:
    header("Exercise 3 - pass@k and pass^k from repeated runs")
    step(1, f"Per scenario, n = {N} runs, k = {K}")
    rows, at, hat, naive = [], [], [], []
    for name, c in RUNS.items():
        a, h = d6.pass_at_k(N, c, K), d6.pass_hat_k(N, c, K)
        at.append(a)
        hat.append(h)
        naive.append((c / N) ** K)
        rows.append([name, f"{c}/{N}", d6.pct(a), f"C({c},{K})/C({N},{K}) = {math.comb(c, K)}/{math.comb(N, K)} = {d6.pct(h)}",
                     d6.pct((c / N) ** K)])
    d6.table(rows, ["scenario", "passed", f"pass@{K}", f"pass^{K} (unbiased)", f"naive p^{K}"])
    pooled = sum(RUNS.values()) / (N * len(RUNS))
    print(f"  mean pass@{K} {d6.pct(statistics.fmean(at))}   mean pass^{K} {d6.pct(statistics.fmean(hat))}   "
          f"mean naive {d6.pct(statistics.fmean(naive))}   pooled rate {d6.pct(pooled)} -> pooled^{K} = {d6.pct(pooled ** K)}")

    step(2, "What each number is for")
    print(f"  pass@{K} ({d6.pct(statistics.fmean(at))}) answers 'if a checker keeps the best of {K} attempts, how often do we "
          "get a good one?'\n  - right for draft-and-verify workflows, wrong for a customer who sees every reply.")
    print(f"  pass^{K} ({d6.pct(statistics.fmean(hat))}) answers 'if this scenario comes up {K} times, how often do all "
          "three go right?' -\n  the number for a support agent. Report it PER SCENARIO: S5 alone is "
          f"{d6.pct(d6.pass_hat_k(N, RUNS['S5 return past the window'], K))}, which no average shows.")

    step(3, "With 5 runs instead of 10")
    values = sorted({d6.pass_hat_k(5, c, K) for c in range(6)})
    print(f"  pass^{K} from 5 runs can only be {', '.join(d6.pct(v, 0) for v in values)}; from 10 runs "
          f"{len({d6.pass_hat_k(10, c, K) for c in range(11)})} values.")
    p = RUNS["S3 warranty with photos"] / N
    dist: dict[float, float] = {}
    for c in range(6):
        prob = math.comb(5, c) * p ** c * (1 - p) ** (5 - c)
        dist[d6.pass_hat_k(5, c, K)] = dist.get(d6.pass_hat_k(5, c, K), 0.0) + prob
    print(f"  If S3 truly passes {d6.pct(p, 0)} of runs (pass^3 = {d6.pct(p ** K)}), five runs report:\n    "
          + ", ".join(f"{d6.pct(v, 0)} with probability {d6.pct(q, 0)}" for v, q in sorted(dist.items())) + ".")
    print("  Five runs give a number, not an estimate: size the runs to the k you report.")


if __name__ == "__main__":
    main()
