"""Exercise 10 starter - a kappa calculator: Cohen's kappa, weighted kappa for ordinal scores, a bootstrap interval.

Implement the three functions marked TODO, then run the script. It applies them to two real rater pairs:
  1. the two human annotators of the pairwise judgments (nominal: A, B, tie) - check your unweighted kappa
     against the labs' helper (d6.cohens_kappa);
  2. the QA reviewer's 1-5 score vs the customer's 1-5 CSAT on the traces that have both (ordinal: a 4 vs a 5
     is a smaller disagreement than a 1 vs a 5, which only a weighted kappa sees).
Until a function is implemented the script prints what is left to do instead of crashing.

Run: python advanced/day6_eval_science_release/exercises/ex10_kappa_calculator.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402


def cohens_kappa(a: Sequence[Any], b: Sequence[Any]) -> float | None:
    """TODO: agreement beyond chance, (p_observed - p_expected) / (1 - p_expected).

    p_observed = share of items where the raters agree; p_expected = sum over labels of (share of a with that
    label) x (share of b with that label). Return 1.0 when p_expected == 1 (both raters used one label).
    """
    return None


def weighted_kappa(a: Sequence[int], b: Sequence[int], categories: Sequence[int], weights: str = "linear") -> float | None:
    """TODO: 1 - sum(w_ij * O_ij) / sum(w_ij * E_ij) over the k x k table of categories.

    O = observed joint proportions; E = product of the two raters' marginal proportions; disagreement weight
    w_ij = |i - j| / (k - 1) for "linear", (|i - j| / (k - 1))^2 for "quadratic" (i, j are category positions).
    """
    return None


def bootstrap_ci(a: Sequence[Any], b: Sequence[Any], stat: Callable[[Sequence[Any], Sequence[Any]], float],
                 n_boot: int = 1000, seed: int = 6) -> tuple[float, float] | None:
    """TODO: percentile interval of stat(a, b) over n_boot resamples of the ITEMS (pairs), with replacement."""
    return None


def datasets() -> dict[str, tuple[list, list]]:
    """The rater pairs the exercise uses (observable fields only)."""
    double = [p for p in d6.load_pairs() if len(p["judgments"]) > 1]
    both = [t for t in d6.load_traces() if t["review"] is not None and t["outcome"]["csat"] is not None]
    return {"H1 vs H2 (pairwise preference)": ([p["judgments"][0]["preferred"] for p in double],
                                               [p["judgments"][1]["preferred"] for p in double]),
            "reviewer score vs CSAT (1-5)": ([t["review"]["score"] for t in both], [t["outcome"]["csat"] for t in both])}


def main() -> None:
    header("Exercise 10 - a kappa calculator (starter)")
    data = datasets()
    for name, (a, b) in data.items():
        agree = sum(x == y for x, y in zip(a, b)) / len(a)
        print(f"{name}: {len(a)} items, raw agreement {d6.pct(agree)}")

    step(1, "Cohen's kappa on both rater pairs")
    a, b = data["H1 vs H2 (pairwise preference)"]
    k = cohens_kappa(a, b)
    if k is None:
        print(f"TODO: implement cohens_kappa(); the labs' helper says {d6.cohens_kappa(a, b):.2f} for H1 vs H2.")
    else:
        print(f"  H1 vs H2: yours {k:.3f}, helper {d6.cohens_kappa(a, b):.3f}")

    step(2, "Weighted kappa for the ordinal pair")
    a, b = data["reviewer score vs CSAT (1-5)"]
    if weighted_kappa(a, b, [1, 2, 3, 4, 5]) is None:
        print("TODO: implement weighted_kappa() (linear and quadratic weights) and compare it with the unweighted value.")

    step(3, "A bootstrap interval")
    if bootstrap_ci(a, b, lambda x, y: 0.0) is None:
        print("TODO: implement bootstrap_ci() and put an interval on every kappa you report.")
    print("\nSolution: advanced/day6_eval_science_release/solutions/ex10_kappa_calculator.py")


if __name__ == "__main__":
    main()
