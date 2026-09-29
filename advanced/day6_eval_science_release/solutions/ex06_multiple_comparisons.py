"""Solution to exercise 6 - ten slices, three corrections, one pre-registered rule.

Recomputes lab 04's per-category Fisher p-values (v15 vs v14, generic success metric), applies Bonferroni, Holm and
Benjamini-Hochberg, and prints the family-wise error arithmetic that motivates them. Then adds the derived hand-off
metric's return_request p-value to the family, to show that a real effect with a mechanism survives any of them.

Run: python advanced/day6_eval_science_release/solutions/ex06_multiple_comparisons.py
"""
# test: expect=Holm
# test: expect=family-wise

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402


def slice_pvalues(traces: list[dict], metric) -> dict[str, float]:
    out = {}
    for cat in d6.CATEGORIES:
        a = [metric(t) for t in traces if d6.version_of(t) == "v14" and t["category"] == cat]
        b = [metric(t) for t in traces if d6.version_of(t) == "v15" and t["category"] == cat]
        out[cat] = d6.fisher_exact(sum(b), len(b) - sum(b), sum(a), len(a) - sum(a))
    return out


def show(pvalues: dict[str, float], title: str) -> None:
    raw = {k: p < 0.05 for k, p in pvalues.items()}
    bonf, holm = d6.bonferroni(pvalues), d6.holm(pvalues)
    bh05, bh10 = d6.benjamini_hochberg(pvalues, q=0.05), d6.benjamini_hochberg(pvalues, q=0.10)
    print(title)
    rows = [[k, f"{p:.4f}"] + ["*" if f[k] else "" for f in (raw, bonf, holm, bh05, bh10)]
            for k, p in sorted(pvalues.items(), key=lambda kv: kv[1])]
    d6.table(rows, ["slice", "Fisher p", "raw 0.05", "Bonferroni", "Holm", "BH q=.05", "BH q=.10"])


def main() -> None:
    header("Exercise 6 - multiple comparisons on lab 04's slices")
    traces = d6.load_traces()

    step(1, "(a) The generic metric: ten slices, four ways to decide")
    generic = slice_pvalues(traces, d6.observable_success)
    show(generic, "  v15 vs v14, observable success:")
    m = len(generic)
    print(f"  Bonferroni threshold 0.05/{m} = {0.05 / m:.4f}; Holm compares the smallest p with 0.05/{m}, the next with "
          f"0.05/{m - 1}, ...;\n  BH compares the i-th smallest with i x q / {m}. Only the raw threshold flags return_request "
          f"(p = {generic['return_request']:.4f}).")

    step(2, "(b) Why: the family-wise error rate")
    for k in (10, 50):
        print(f"  {k} independent tests at 0.05, no real effect anywhere: P(at least one flag) = 1 - 0.95^{k} = "
              f"{1 - 0.95 ** k:.0%}")
    print("  Ten slices x five metrics is 50 tests: without a rule, a release with no regression 'finds' one almost\n"
          "  every time - and a team that looks at enough slices can always find a reason to block or to ship.")

    step(3, "(c) Add the derived metric: a real effect survives every correction")
    def handoff(t: dict) -> bool:
        return d6.handoff_language(t) and "escalate_to_human" not in t["tools"]
    family = {f"{k} (success)": p for k, p in generic.items()}
    family["return_request (hand-off)"] = slice_pvalues(traces, handoff)["return_request"]
    show(family, "  the same family plus the hand-off metric on return_request:")
    print("  Gate vs monitor: a hard gate that blocks a release should control the family-wise error (Holm: the same\n"
          "  guarantee as Bonferroni, never less power); a monitor that routes slices to a human can use BH, which\n"
          "  controls the share of flagged slices that are false alarms and keeps more power.")

    step(4, "(d) The rule, written before the rollout")
    print("  * Gates: return_request and safety_incident success (Holm across the gates), plus zero-tolerance checks.\n"
          "  * Monitors: every other category x metric, BH at q = 0.10, a flag means 'a human looks this week'.\n"
          "  * A flagged slice becomes a finding only with a mechanism (a reply text, a tool pattern, a release note).")


if __name__ == "__main__":
    main()
