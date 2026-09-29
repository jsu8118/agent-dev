"""Solution to exercise 10 - Cohen's kappa, weighted kappa for ordinal scores, and a bootstrap interval.

Implements the three functions of the starter (exercises/ex10_kappa_calculator.py), checks the unweighted kappa
against the labs' helper, and shows three things a single agreement number hides: agreement expected by chance,
the size of ordinal disagreements, and the sampling error of a kappa computed on a few dozen items.

Run: python advanced/day6_eval_science_release/solutions/ex10_kappa_calculator.py
"""
# test: expect=weighted kappa
# test: expect=always says tie

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))

import _day6 as d6  # noqa: E402
from ex10_kappa_calculator import datasets  # noqa: E402
from labkit import header, step  # noqa: E402


def cohens_kappa(a: Sequence[Any], b: Sequence[Any]) -> float:
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum((ca[label] / n) * (cb[label] / n) for label in set(ca) | set(cb))
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def weighted_kappa(a: Sequence[int], b: Sequence[int], categories: Sequence[int], weights: str = "linear") -> float:
    k, n = len(categories), len(a)
    pos = {c: i for i, c in enumerate(categories)}
    observed = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        observed[pos[x]][pos[y]] += 1 / n
    row = [sum(observed[i]) for i in range(k)]
    col = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    power = 1 if weights == "linear" else 2
    w = [[(abs(i - j) / (k - 1)) ** power for j in range(k)] for i in range(k)]
    num = sum(w[i][j] * observed[i][j] for i in range(k) for j in range(k))
    den = sum(w[i][j] * row[i] * col[j] for i in range(k) for j in range(k))
    return 1.0 if den == 0 else 1 - num / den


def bootstrap_ci(a: Sequence[Any], b: Sequence[Any], stat: Callable[[Sequence[Any], Sequence[Any]], float],
                 n_boot: int = 1000, seed: int = 6) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(a)
    values = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        values.append(stat([a[i] for i in idx], [b[i] for i in idx]))
    values.sort()
    return values[int(0.025 * n_boot)], values[int(0.975 * n_boot) - 1]


def main() -> None:
    header("Exercise 10 - a kappa calculator (solution)")
    data = datasets()

    step(1, "Cohen's kappa, checked against the labs' helper")
    rows = []
    for name, (a, b) in data.items():
        k = cohens_kappa(a, b)
        lo, hi = bootstrap_ci(a, b, cohens_kappa)
        rows.append([name, len(a), d6.pct(sum(x == y for x, y in zip(a, b)) / len(a)), f"{k:.3f}",
                     f"{d6.cohens_kappa(a, b):.3f}", f"{lo:.2f} to {hi:.2f}"])
    d6.table(rows, ["rater pair", "items", "agreement", "kappa (ours)", "kappa (helper)", "95% bootstrap"])
    print("  The two annotators agree on about half the pairs, and kappa says most of that is chance: with three\n"
          "  labels used in similar proportions, two random raters would agree a third of the time. The interval\n"
          "  over 39 items is wide enough to hold 'barely above chance' and 'moderate' - report it with the number.")

    step(2, "Weighted kappa: the size of an ordinal disagreement")
    a, b = data["reviewer score vs CSAT (1-5)"]
    cats = [1, 2, 3, 4, 5]
    table = Counter(zip(a, b))
    print("  joint counts (rows = reviewer score, columns = CSAT):")
    print("         " + "".join(f"{c:>5}" for c in cats))
    for r in cats:
        print(f"    {r:<4} " + "".join(f"{table[(r, c)]:>5}" for c in cats))
    unweighted, linear, quad = cohens_kappa(a, b), weighted_kappa(a, b, cats), weighted_kappa(a, b, cats, "quadratic")
    lo, hi = bootstrap_ci(a, b, lambda x, y: weighted_kappa(x, y, cats, "quadratic"))
    print(f"  unweighted kappa {unweighted:.2f}   linear weighted kappa {linear:.2f}   quadratic weighted kappa {quad:.2f} "
          f"(95% {lo:.2f} to {hi:.2f})")
    print("  Unweighted kappa counts a 4-vs-5 like a 1-vs-5. The weighted kappas credit near-misses, and the two\n"
          "  scales still agree weakly at best: a reviewer's score and a customer's rating measure different things\n"
          "  (policy correctness vs the customer's experience). Use weighted kappa for ordinal scales - the first\n"
          "  course's 1-5 judge calibration is exactly that case - and say which weights you used.")

    step(3, "The paradox kappa exists for")
    h1, h2 = data["H1 vs H2 (pairwise preference)"]
    always_tie = ["tie"] * len(h1)
    agree = sum(x == y for x, y in zip(h1, always_tie)) / len(h1)
    print(f"  A judge that always says tie agrees with H1 on {d6.pct(agree)} of these pairs - and its kappa is "
          f"{cohens_kappa(h1, always_tie):.2f}.")
    skewed_a = ["pass"] * 90 + ["fail"] * 10
    skewed_b = ["pass"] * 85 + ["fail"] * 5 + ["pass"] * 5 + ["fail"] * 5
    print(f"  On a 90/10 label, two raters agreeing on {sum(x == y for x, y in zip(skewed_a, skewed_b))}% of items get kappa "
          f"{cohens_kappa(skewed_a, skewed_b):.2f}: when one label dominates, chance agreement is\n  high and raw "
          "agreement flatters everyone. Read kappa with the confusion table, never alone.")


if __name__ == "__main__":
    main()
