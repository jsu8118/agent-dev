"""Solution to exercise 2 - pass@k vs pass^k, with numbers.

pass@k  = P(at least one of k attempts succeeds)   - the right metric when you can CHECK and pick a winner
pass^k  = P(all k attempts succeed)                - the right metric when every attempt reaches a customer

Run
    python day6_evals_guardrails_production/solutions/ex02_pass_at_k.py
"""

# test: expect=pass^k

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _evalkit import pass_at_k, pass_hat_k  # noqa: E402
from labkit import header, step  # noqa: E402


def main() -> None:
    header("Exercise 2 - pass@k vs pass^k")

    step(1, "Independent attempts with per-attempt success p")
    ks = (1, 3, 5, 10)
    print(f"  {'p':>6} | " + " ".join(f"pass@{k:<3}" for k in ks) + " | " + " ".join(f"pass^{k:<3}" for k in ks))
    for p in (0.80, 0.90, 0.95, 0.99):
        at = " ".join(f"{1 - (1 - p) ** k:>8.1%}" for k in ks)
        hat = " ".join(f"{p ** k:>8.1%}" for k in ks)
        print(f"  {p:>6.0%} | {at} | {hat}")
    print("  A 90% agent looks like 99.9% on pass@3 and 72.9% on pass^3. Kestrel's customers see every attempt,\n"
          "  so the support agent is judged on pass^k; a coding agent whose patch is checked by tests before\n"
          "  anyone sees it can legitimately use pass@k (generate k, keep the one that passes).")

    step(2, "Estimating from repeated runs (what _evalkit does with --reps)")
    for n, c in ((10, 10), (10, 9), (10, 8), (10, 5)):
        print(f"  {c}/{n} runs passed -> pass@3 {pass_at_k(n, c, 3):6.1%}   pass^3 {pass_hat_k(n, c, 3):6.1%}   "
              f"(naive p^3 = {(c / n) ** 3:6.1%})")
    print("  The unbiased estimators use combinations of the observed runs (C(c,k)/C(n,k) for pass^k), so they\n"
          "  need n >= k runs per scenario; compute them PER SCENARIO, then average - failures cluster on the hard\n"
          "  scenarios, which is exactly what the per-scenario estimate preserves and a global p^k would hide.")

    step(3, "Correlated failures: why per-scenario matters")
    easy, hard = 0.99, 0.60              # 80% easy scenarios, 20% hard ones; overall per-attempt p = 0.912
    overall = 0.8 * easy + 0.2 * hard
    print(f"  mixed set: overall p = {overall:.3f}; independent-model pass^5 = {overall ** 5:.1%}; "
          f"true pass^5 = {0.8 * easy ** 5 + 0.2 * hard ** 5:.1%}")
    print("  The true reliability is dominated by the hard slice: report pass^k per scenario type, not one number.")

    step(4, "Pipelines multiply")
    print(f"  5 sequential steps at 99% each -> {0.99 ** 5:.1%} end to end; at 95% each -> {0.95 ** 5:.1%}.")


if __name__ == "__main__":
    main()
