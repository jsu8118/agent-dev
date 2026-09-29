"""Solution to exercise 12 - contradiction detectors, their base rates, which can gate, and how good they are.

Implements the starter's three detectors (exercises/ex12_contradiction_detectors.py), counts them per prompt
version and arm, turns the counts into gate rules (a zero-tolerance gate needs a zero baseline), and finally -
as only a solution may - grades each detector against the hidden truth: precision (when it fires, is there really
an issue?) and recall (how many of the planted issues of that type does it see?).

Run: python advanced/day6_eval_science_release/solutions/ex12_contradiction_detectors.py
"""
# test: expect=precision
# test: expect=zero-tolerance

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))

import _day6 as d6  # noqa: E402
import ex12_contradiction_detectors as starter  # noqa: E402
from labkit import header, step  # noqa: E402

REFUND = re.compile(r"\brefund", re.I)


def handoff_without_escalation(trace: dict) -> bool:
    return d6.handoff_language(trace) and "escalate_to_human" not in trace["tools"]


def refund_promise_without_tool(trace: dict) -> bool:
    return bool(REFUND.search(trace["reply"])) and "issue_refund" not in trace["tools"]


def order_id_contradiction(trace: dict) -> bool:
    return d6.contradicts_ticket_order(trace)


DETECTORS = {"hand-off without escalation": (handoff_without_escalation, "unnecessary_escalation"),
             "refund promise without refund tool": (refund_promise_without_tool, "over_promised"),
             "order id contradicts the customer's": (order_id_contradiction, "hallucinated_id")}


def main() -> None:
    header("Exercise 12 - contradiction detectors (solution)")
    traces = d6.load_traces()

    step(1, "Counts per prompt version and per arm (observable fields only)")
    rows = []
    for name, (detector, _) in DETECTORS.items():
        v = starter.counts(traces, detector, d6.version_of)
        a = starter.counts(traces, detector, d6.arm_of)
        rows.append([name] + [f"{v[g][0]}/{v[g][1]}" for g in ("v14", "v15")]
                    + [f"{a[g][0]}/{a[g][1]}" for g in d6.ARMS])
    d6.table(rows, ["detector", "v14", "v15"] + d6.ARMS)

    step(2, "Which detectors can gate, and how")
    v14 = [t for t in traces if d6.version_of(t) == "v14"]
    for name, (detector, _) in DETECTORS.items():
        base = sum(map(detector, v14))
        if base == 0:
            rule = "zero-tolerance gate: the incumbent never does it, so one occurrence is a regression"
        else:
            rule = (f"relative gate: the incumbent already does it ({base}/{len(v14)}); gate on 'not worse' with an "
                    "interval,\n      and open a defect for the existing rate - a zero-tolerance rule would block every "
                    "release, the incumbent included")
        print(f"  {name}: {rule}")
    print("  All three are cheap enough to run on every reply before it is sent - as guardrails that hold the reply\n"
          "  for a human, not only as offline gates.")

    step(3, "SOLUTION ONLY: graded against the hidden truth")
    truth = d6.hidden_truth_for_grading()
    rows = []
    for name, (detector, issue) in DETECTORS.items():
        fired = [t for t in traces if detector(t)]
        planted = [t for t in traces if issue in truth[t["trace_id"]]]
        tp = sum(issue in truth[t["trace_id"]] for t in fired)
        rows.append([name, issue, len(fired), f"{tp}/{len(fired)} = {d6.pct(tp / len(fired)) if fired else '-'}",
                     f"{tp}/{len(planted)} = {d6.pct(tp / len(planted)) if planted else '-'}"])
    d6.table(rows, ["detector", "planted issue", "fired", "precision", "recall"])
    print("  Precise, not complete. The hand-off detector misses the hand-offs on tickets that needed a human (the\n"
          "  trace does call escalate_to_human there); the order-id detector sees a hallucinated id only when the\n"
          "  customer's email names an order AND the reply quotes one - the rest need the tool results\n"
          "  (compare every id in the reply with the ids the tools returned) or a judge. A detector that never\n"
          "  cries wolf is the right kind for a gate; its blind spots go on the monitor list.")


if __name__ == "__main__":
    main()
