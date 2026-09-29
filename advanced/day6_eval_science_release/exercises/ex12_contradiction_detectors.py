"""Exercise 12 starter - detectors for replies that contradict their own trace, and which of them can gate a release.

A reply is a set of claims; a trace is a record of what was done. Lab 04's regression was a claim ("I have
escalated your return to a colleague") that the trace contradicts (no escalate_to_human call). Implement three
detectors over the observable fields of one trace:

  handoff_without_escalation(trace)   the reply hands the customer to a colleague; no escalate_to_human in tools
  refund_promise_without_tool(trace)  the reply promises a refund; no refund tool (issue_refund) ran
  order_id_contradiction(trace)       the reply names an order id, the customer's email names one, and they differ

Then run the script: it counts each detector per prompt version and per arm. From those counts alone decide which
detectors can be ZERO-TOLERANCE release gates and which need a relative rule ("not worse than the incumbent").

Run: python advanced/day6_eval_science_release/exercises/ex12_contradiction_detectors.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402


def handoff_without_escalation(trace: dict) -> bool | None:
    """TODO: hand-off language in trace["reply"] (d6.handoff_language helps) and no "escalate_to_human" in trace["tools"]."""
    return None


def refund_promise_without_tool(trace: dict) -> bool | None:
    """TODO: the reply promises a refund (a regex on "refund") and "issue_refund" is not in trace["tools"]."""
    return None


def order_id_contradiction(trace: dict) -> bool | None:
    """TODO: d6.order_ids(reply) vs d6.ticket_order_ids(ticket_id): both non-empty and no id in common."""
    return None


DETECTORS: dict[str, Callable[[dict], bool | None]] = {
    "hand-off without escalation": handoff_without_escalation,
    "refund promise without refund tool": refund_promise_without_tool,
    "order id contradicts the customer's": order_id_contradiction,
}


def counts(traces: list[dict], detector: Callable[[dict], bool | None], key: Callable[[dict], str]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for group, items in sorted(d6.by(traces, key).items()):
        out[group] = (sum(bool(detector(t)) for t in items), len(items))
    return out


def main() -> None:
    header("Exercise 12 - contradiction detectors (starter)")
    traces = d6.load_traces()
    step(1, "Run every detector over the traces")
    for name, detector in DETECTORS.items():
        if detector(traces[0]) is None:
            print(f"TODO: implement {detector.__name__}()")
            continue
        by_version = counts(traces, detector, d6.version_of)
        by_arm = counts(traces, detector, d6.arm_of)
        print(f"  {name}: " + ", ".join(f"{g} {k}/{n}" for g, (k, n) in by_version.items()) + "  |  "
              + ", ".join(f"{g} {k}/{n}" for g, (k, n) in by_arm.items()))
    step(2, "Decide")
    print("TODO: which detectors can be zero-tolerance gates, which need 'not worse than the incumbent', and why?\n"
          "Solution: advanced/day6_eval_science_release/solutions/ex12_contradiction_detectors.py")


if __name__ == "__main__":
    main()
