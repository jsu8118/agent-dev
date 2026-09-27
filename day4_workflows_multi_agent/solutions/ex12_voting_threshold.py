"""Solution to exercise 12 - choose a voting rule by expected cost, not by habit.

Lab 03's five lenses vote on every invoice. This solution compares aggregation rules on the same votes:
    * k-of-5 thresholds (k = 1..5; k = 3 is the majority);
    * veto rules - named lenses may escalate alone, everyone else needs a majority;
    * a weighted vote (weights reflect how directly a lens sees the loss it is voting on).
Each rule is scored with an explicit cost model: a missed escalation costs the invoice amount at risk
(duplicate payment or payment to a fraudster), a false alarm costs a clerk's review time. The best rule
is the one with the lowest expected cost - with its precision/recall shown so nobody is surprised.

The votes are collected once (100 calls on the fast model); every rule is then evaluated in code, for
free. That separation - expensive judgements once, cheap aggregation many times - is the point.

Run
    python day4_workflows_multi_agent/solutions/ex12_voting_threshold.py [--false-alarm-cost 15]
"""

# test: expect=expected cost
# test: expect=veto

from __future__ import annotations

import argparse
import asyncio
from typing import Callable

import _labs
from _ap import load_expected, load_invoices, load_master, review_context, short
from _common import table
from labkit import get_async_client, header, step

lab03 = _labs.load("03_parallelization")
Rule = Callable[[dict[str, bool | None]], bool]


def k_of_n(k: int) -> Rule:
    return lambda ballots: sum(bool(v) for v in ballots.values()) >= k


def veto(lenses: set[str], k: int = 3) -> Rule:
    return lambda ballots: any(ballots.get(l) for l in lenses) or sum(bool(v) for v in ballots.values()) >= k


def weighted(weights: dict[str, float], threshold: float) -> Rule:
    return lambda ballots: sum(weights[l] for l, v in ballots.items() if v) >= threshold


RULES: dict[str, Rule] = {
    **{f"{k} of 5" + (" (majority)" if k == 3 else ""): k_of_n(k) for k in range(1, 6)},
    "veto: security": veto({"security"}),
    "veto: security + ap_clerk": veto({"security", "ap_clerk"}),
    "weighted (sec 2, treas 2, clerk 2, aud 1, proc 0.5) >= 2": weighted(
        {"security": 2, "treasury": 2, "ap_clerk": 2, "auditor": 1, "procurement": 0.5}, 2.0),
}


def evaluate(rule: Rule, votes: dict[str, dict], truth: set[str], amounts: dict[str, float],
             false_alarm_cost: float) -> dict:
    flagged = {f for f, ballots in votes.items() if rule(ballots)}
    tp, fp, fn = flagged & truth, flagged - truth, truth - flagged
    return {"flagged": flagged, "tp": len(tp), "fp": len(fp), "fn": len(fn),
            "precision": len(tp) / len(flagged) if flagged else 1.0, "recall": len(tp) / len(truth) if truth else 1.0,
            "cost": sum(amounts[f] for f in fn) + false_alarm_cost * len(fp)}


async def collect_votes(files, contexts) -> dict[str, dict]:
    async with get_async_client() as client:
        result = await lab03.run_votes(client, files, contexts, concurrency=8)
    return result["votes"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--false-alarm-cost", type=float, default=15.0, help="USD of clerk time per false alarm")
    args = parser.parse_args()
    header("Exercise 12 - voting thresholds, veto lenses and expected cost")
    master, invoices, expected = load_master(), load_invoices(), load_expected()
    files = [f for f, _ in invoices]
    contexts = {f: review_context(f, invoices, master) for f in files}
    truth = {f for f in files if expected[f]["expected_decision"] in ("security_hold", "reject")}
    # Money at risk if an escalation is missed: the invoice total in USD (EUR at the policy rate 1.08).
    amounts = {f: expected[f]["invoice_total"] * (1.08 if expected[f]["currency"] == "EUR" else 1.0) for f in files}

    step(1, "Collect the votes once (5 lenses x 20 invoices, fast model)")
    votes = asyncio.run(collect_votes(files, contexts))
    print(f"  escalations the AP team expects: {', '.join(short(f) for f in sorted(truth))}")

    step(2, f"Score every rule (miss = invoice amount at risk, false alarm = ${args.false_alarm_cost:.0f})")
    scored = {name: evaluate(rule, votes, truth, amounts, args.false_alarm_cost) for name, rule in RULES.items()}
    rows = [[name, len(s["flagged"]), s["tp"], s["fp"], s["fn"], f"{s['precision']:.2f}", f"{s['recall']:.2f}",
             f"${s['cost']:,.0f}"] for name, s in scored.items()]
    print(table(rows, ["rule", "escalated", "TP", "FP", "FN", "precision", "recall", "expected cost"]))

    step(3, "Pick the rule")
    ranking = sorted(scored, key=lambda n: (scored[n]["cost"], len(scored[n]["flagged"])))
    best = ranking[0]
    print(f"  lowest expected cost: {best} -> escalates {', '.join(short(f) for f in sorted(scored[best]['flagged']))}")
    print("  ranking of the flat thresholds: " + " < ".join(n for n in ranking if " of 5" in n))
    print("  The majority rule misses the duplicate (only 2 lenses see double payment); a veto for the lenses that")
    print("  own a risk catches it without flooding clerks. Twenty invoices is a small sample: validate on a larger")
    print("  labelled set, and re-run with a different --false-alarm-cost to see the flat thresholds re-rank.")


if __name__ == "__main__":
    main()
