"""Solution to exercise 1 (e) - how good are the three label sources? Graded against the hidden truth.

Lab 01 derived weak labels from what the traces carry (a QA review, a CSAT score, a human edit) and measured how
much the sources agree with EACH OTHER. Agreement is not accuracy: two noisy signals can agree on the same
mistake. This solution does what only an author can do - compare every source with the planted ground truth
(`_truth.issues`, which labs never read) - and prints, per source, how often it calls a flawed reply a pass (the
dangerous direction for a gate) and a clean reply a fail (the expensive direction for reviewers).

Run: python advanced/day6_eval_science_release/solutions/ex01_label_quality.py
"""
# test: expect=hidden truth
# test: expect=false pass

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402

SOURCES = {
    "review": (lambda t: t["review"] is not None, lambda t: not t["review"]["issues"]),
    "csat": (lambda t: t["outcome"]["csat"] is not None, lambda t: t["outcome"]["csat"] >= 4),
    "edit": (lambda t: True, d6.observable_success),
    "weak label (lab 01 policy)": (lambda t: True, lambda t: d6.weak_label(t)[0]),
}


def main() -> None:
    header("Exercise 1 (e) - label sources graded against the hidden truth")
    print("SOLUTION ONLY: this script reads `_truth` through d6.hidden_truth_for_grading(); labs never do.")
    traces = d6.load_traces()
    truth = d6.hidden_truth_for_grading()

    step(1, "Each source against the truth (a reply is truly good when it has no planted issue)")
    rows, rates = [], {}
    for name, (has, passes) in SOURCES.items():
        group = [t for t in traces if has(t)]
        good = [not truth[t["trace_id"]] for t in group]
        said = [passes(t) for t in group]
        flawed = [s for s, g in zip(said, good) if not g]
        clean = [s for s, g in zip(said, good) if g]
        rates[name] = (d6.rate(flawed), 1 - d6.rate(clean), len(flawed), len(clean), sum(flawed), sum(not s for s in clean))
        rows.append([name, len(group), d6.pct(sum(s == g for s, g in zip(said, good)) / len(group)),
                     f"{d6.cohens_kappa(said, good):.2f}", f"{sum(flawed)}/{len(flawed)} = {d6.pct(d6.rate(flawed))}",
                     f"{sum(not s for s in clean)}/{len(clean)} = {d6.pct(1 - d6.rate(clean))}"])
    d6.table(rows, ["source", "traces", "accuracy", "kappa", "false pass (flawed -> pass)", "false fail (clean -> fail)"])
    r, c, e, w = (rates[k] for k in SOURCES)
    print(f"  The review is the best source: {r[4]} of {r[2]} flawed replies slipped through, {r[5]} of {r[3]} clean ones "
          f"were flagged.\n  CSAT errs about {d6.pct(c[0], 0)} in both directions (and exists for less than half of the "
          f"traces). The edit\n  signal passes {d6.pct(e[0], 0)} of the flawed replies - nobody touched them - which is the "
          "dangerous direction for a\n  gate. The weak label inherits the mix: good enough to FIND candidates for an eval "
          "set, not to gate on\n  without a hand-verified sample.")

    step(2, "What the reviewers miss, by issue type")
    reviewed = [t for t in traces if t["review"] is not None]
    rows = []
    for issue, n in sorted(Counter(i for t in reviewed for i in truth[t["trace_id"]]).items()):
        caught = sum(issue in t["review"]["issues"] for t in reviewed if issue in truth[t["trace_id"]])
        rows.append([issue, n, caught, d6.pct(caught / n)])
    false_flags = sum(1 for t in reviewed if t["review"]["issues"] and not truth[t["trace_id"]])
    d6.table(rows, ["planted issue", "in reviewed traces", "reviewer listed it", "recall"])
    print(f"  Plus {false_flags} review(s) listing an issue on a clean reply. A label is a measurement: record which "
          "instrument\n  produced it, and re-verify a sample by hand before a set built from it gates a release.")


if __name__ == "__main__":
    main()
