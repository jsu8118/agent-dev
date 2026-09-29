"""Solution to exercise 9 - a selection-eval scorer, and a held-out set.

Objective
    Score description sets A and B properly: accuracy with Wilson intervals, per-tool precision and recall, the
    off-diagonal confusion matrix, and an exact McNemar test on the paired results - on lab 07's 30 tasks and on ten
    held-out tasks written without looking at description set B.

Concepts
    Wilson score interval, per-class precision/recall, confusion matrices, paired designs and McNemar's exact test,
    held-out sets and tuning bias, why 30 tasks cannot separate 90% from 97%

Run
    python advanced/day2_tools_at_scale/solutions/ex09_selection_scorer.py

What to observe
    * The intervals of A and B overlap on 30 tasks; the McNemar p-value says the same.
    * Per-tool recall shows where each set fails; precision shows which tools soak up wrong calls.
    * The held-out score, which is the one to believe.
    * The number of paired tasks it takes to tell 90% from 97% - hundreds, not thirty.
"""
# test: expect=McNemar
# test: expect=held-out
# test: expect=80% power

from __future__ import annotations

import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))

from labkit import get_client, header, step  # noqa: E402

import ex09_selection_scorer as ex  # noqa: E402

d2 = ex.d2

HELD_OUT: list[tuple[str, str]] = [
    ("Has the pallet on NLF9028150109 left the hub yet?", "track_shipment"),
    ("What did we tell GreenValley the ETA would be for shipment SH-50283?", "get_shipment"),
    ("Did SO-10290 ship complete or in pieces?", "list_shipments_for_order"),
    ("Pull up SO-10272 - what exactly did Midland order?", "get_order"),
    ("Who put SO-10248 on hold and when was it released?", "get_order_status_history"),
    ("How much is still outstanding on AR-90244?", "get_invoice"),
    ("Put $310 back on GreenValley's card for SO-10303, they were overcharged.", "issue_refund"),
    ("Lower what Harbor Foods owes on AR-90267 by $150 for the scratched housing.", "issue_credit_note"),
    ("Take the late-payment penalty off AR-90250, the invoice went out late.", "apply_late_fee_waiver"),
    ("Can Midland send back one of the three KP-250-S from SO-10272 that they no longer need?", "check_return_eligibility"),
]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def per_tool(expected: list[str], got: list[str | None]) -> dict[str, tuple[float, float]]:
    out = {}
    for tool in sorted(set(expected) | {g for g in got if g}):
        called = [e for e, g in zip(expected, got) if g == tool]
        needed = [g for e, g in zip(expected, got) if e == tool]
        precision = sum(1 for e in called if e == tool) / len(called) if called else float("nan")
        recall = sum(1 for g in needed if g == tool) / len(needed) if needed else float("nan")
        out[tool] = (precision, recall)
    return out


def mcnemar_exact(a_right: list[bool], b_right: list[bool]) -> tuple[int, int, float]:
    fixes = sum(1 for a, b in zip(a_right, b_right) if b and not a)
    breaks = sum(1 for a, b in zip(a_right, b_right) if a and not b)
    n = fixes + breaks
    if n == 0:
        return fixes, breaks, 1.0
    tail = sum(math.comb(n, i) for i in range(min(fixes, breaks) + 1)) / 2 ** n
    return fixes, breaks, min(1.0, 2 * tail)


def mcnemar_power(n: int, fixes: float, breaks: float, alpha: float = 0.05) -> float:
    """Power of the exact McNemar test on n paired tasks when B fixes a share `fixes` of them and breaks `breaks`:
    enumerate the number of discordant pairs D and, for each, the fixes x the test would call significant."""
    discordant, q = fixes + breaks, fixes / (fixes + breaks)
    power = 0.0
    for d in range(n + 1):
        weight = math.comb(n, d) * discordant ** d * (1 - discordant) ** (n - d)
        if weight < 1e-12:
            continue
        for x in range(d + 1):
            tail = sum(math.comb(d, i) for i in range(min(x, d - x) + 1)) / 2 ** d
            if min(1.0, 2 * tail) <= alpha:
                power += weight * math.comb(d, x) * q ** x * (1 - q) ** (d - x)
    return power


def tasks_needed(fixes: float, breaks: float, target: float = 0.8) -> int:
    n = 10
    while mcnemar_power(n, fixes, breaks) < target:
        n += 5
    return n


def report(label: str, tasks: list[tuple[str, str]], got: dict[str, list[str | None]]) -> None:
    expected = [e for _, e in tasks]
    right = {w: [g == e for g, e in zip(got[w], expected)] for w in got}
    rows = []
    for w in got:
        k = sum(right[w])
        lo, hi = wilson(k, len(expected))
        rows.append([w, f"{k}/{len(expected)}", f"{k / len(expected):.0%}", f"{lo:.0%}-{hi:.0%}"])
    d2.table(rows, ["set", "correct", "accuracy", "95% CI"])
    fixes, breaks, p = mcnemar_exact(right["A"], right["B"])
    print(f"  McNemar exact test on the {label} set: B fixes {fixes}, breaks {breaks}; two-sided p = {p:.3f}")
    for w in got:
        confusions = Counter((e, g) for e, g in zip(expected, got[w]) if e != g)
        print(f"  {w} confusions: " + (", ".join(f"{e} -> {g} x{n}" for (e, g), n in confusions.items()) or "none"))


def main() -> None:
    client = get_client()
    header("Exercise 9 (solution) - a selection-eval scorer")
    expected = [e for _, e in d2.SELECTION_TASKS]
    got = {w: ex.predictions(client, d2.SELECTION_TASKS, w) for w in ("A", "B")}

    step(1, "Lab 07's 30 tasks")
    report("30-task", d2.SELECTION_TASKS, got)
    print("\n  per-tool precision / recall (set A | set B):")
    pa, pb = per_tool(expected, got["A"]), per_tool(expected, got["B"])
    for tool in sorted(set(pa) | set(pb)):
        fa, fb = pa.get(tool, (float("nan"),) * 2), pb.get(tool, (float("nan"),) * 2)
        fmt = lambda x: "  - " if math.isnan(x) else f"{x:.2f}"   # noqa: E731  (no calls made to that tool)
        print(f"    {tool:<26} A {fmt(fa[0])} / {fmt(fa[1])}   B {fmt(fb[0])} / {fmt(fb[1])}")

    step(2, "Ten held-out tasks, written without looking at set B")
    held = {w: ex.predictions(client, HELD_OUT, w) for w in ("A", "B")}
    report("held-out", HELD_OUT, held)
    step(3, "How many tasks would a paired comparison need?")
    for fixes, breaks in ((0.07, 0.0), (0.10, 0.03)):
        print(f"  B fixes {fixes:.0%} of tasks and breaks {breaks:.0%} (90% -> 97%): about {tasks_needed(fixes, breaks)} tasks "
              f"for 80% power (exact McNemar, two-sided 0.05)")
    print("\n  30 tasks cannot separate 90% from 97%: the intervals overlap and the paired test cannot reject 'no\n"
          "  difference'. Grow the set (hundreds of tasks mined from real traffic, Day 6), keep a held-out slice you never\n"
          "  tune on, and report the paired result - not two accuracies side by side.")


if __name__ == "__main__":
    main()
