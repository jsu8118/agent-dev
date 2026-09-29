"""Lab 04 - Finding the regression: slices, Simpson's paradox, a derived metric, and the gate that catches it.

Objective
    Reproduce Kestrel's v15 incident from the traces alone. Start from what the release review saw (the
    aggregate held, CSAT ticked up, the reviewers' policy findings dropped), show why arm-level averages
    mislead when arms see different traffic (the canary "beats" everyone because it only gets easy tickets),
    slice by category x prompt version with an exact test and a multiple-comparisons correction, derive the
    metric that did not exist yet - hand-off language in a reply whose trace never called escalate_to_human -
    and run the release gate that would have blocked v15 in its first week.

Concepts
    aggregate vs slice, Simpson's paradox and standardisation, Fisher's exact test, Bonferroni, mechanism
    before verdict, derived metrics from reply text x tool calls, non-inferiority margins with intervals,
    minimum slice size, sequential detection on a live stream.

Run
    python advanced/day6_eval_science_release/labs/04_finding_the_regression.py

What to observe
    * Step 1 is the dashboard that said "ship": 73.8% -> 73.6% success, CSAT 3.82 -> 3.88, policy findings down.
    * Step 2: the haiku canary's 81.8% is a category mix, not a quality; standardise before you compare arms.
    * Step 3: return_request is the only slice under alpha = 0.05 and it does NOT survive Bonferroni on the
      generic metric. The derived hand-off metric (7/18 vs 0/44, p = 0.00006) does - a mechanism, not a p-value,
      is what turns a flagged slice into a finding.
    * Step 4: the gate reads INSUFFICIENT on tiny slices, REVIEW where the interval allows a 10-point drop, and
      BLOCK on the zero-tolerance derived check - it blocks v15 on the data available by 2026-09-06.
    * Step 5: a sequential test on the derived metric fires on the first day of the rollout; the complaint that
      actually surfaced the bug came three weeks later.
"""
# test: expect=BLOCK
# test: expect=Simpson

from __future__ import annotations

import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402

ROLLOUT_DAY = "2026-08-31"          # v15 went to 50% of traffic
FIRST_WEEK_END = "2026-09-06"       # the data a release review could have looked at after one week
MARGIN = 0.10                       # non-inferiority margin for a category slice
MIN_SLICE = 10                      # below this, a slice cannot say anything


def unexpected_handoff(trace: dict) -> bool:
    """The reply hands the customer off, but the trace never called escalate_to_human: a claim the trace contradicts."""
    return d6.handoff_language(trace) and "escalate_to_human" not in trace["tools"]


def v14(traces: list[dict]) -> list[dict]:
    return [t for t in traces if d6.version_of(t) == "v14"]


def v15(traces: list[dict]) -> list[dict]:
    return [t for t in traces if d6.version_of(t) == "v15"]


# ------------------------------------------------------------------------------------------ step 1
def step_dashboard(traces: list[dict]) -> None:
    rows = []
    for name, group in (("v14 (baseline)", v14(traces)), ("v15 (all arms)", v15(traces))):
        succ = [d6.observable_success(t) for t in group]
        csat = [t["outcome"]["csat"] for t in group if t["outcome"]["csat"] is not None]
        reviews = [t["review"] for t in group if t["review"]]
        policy = sum("wrong_policy" in r["issues"] or "missing_citation" in r["issues"] for r in reviews)
        rows.append([name, len(group), d6.pct(d6.rate(succ)), f"{statistics.fmean(csat):.2f}",
                     f"{statistics.fmean(r['score'] for r in reviews):.2f}", f"{policy}/{len(reviews)}",
                     d6.money(statistics.fmean(t["cost_usd"] for t in group))])
    d6.table(rows, ["prompt version", "traces", "success", "mean CSAT", "reviewer score", "policy/citation findings",
                    "cost/trace"])
    print("  Release notes for v15: 'Reworded policy citations; added be-brief guidance; new instruction to hand off\n"
          "  ambiguous returns to a colleague.' The two things it set out to fix improved (policy and citation findings\n"
          "  dropped from 13/114 reviews to 1/40), CSAT went up, success held. The dashboard said ship.")


# ------------------------------------------------------------------------------------------ step 2
def step_simpson(traces: list[dict]) -> None:
    easy = ("order_status", "product_inquiry")
    rows = []
    for arm in ("baseline", "haiku-fastpath"):
        s = [t for t in traces if d6.arm_of(t) == arm]
        e = [t for t in s if t["category"] in easy]
        rows.append([arm, len(s), d6.pct(d6.rate([d6.observable_success(t) for t in s])),
                     d6.pct(len(e) / len(s), 0), len(e), d6.pct(d6.rate([d6.observable_success(t) for t in e])),
                     d6.ci_text(*d6.wilson(sum(d6.observable_success(t) for t in e), len(e)))])
    d6.table(rows, ["arm", "traces", "success (all)", "share easy cats", "easy traces", "success (easy cats)", "95% CI"])
    mix = Counter(t["category"] for t in traces if d6.arm_of(t) == "baseline")
    base_by_cat = {c: d6.rate([d6.observable_success(t) for t in traces if d6.arm_of(t) == "baseline" and t["category"] == c])
                   for c in d6.CATEGORIES}
    standardised = sum(base_by_cat[c] * mix[c] for c in d6.CATEGORIES) / sum(mix.values())
    easy_only = sum(base_by_cat[c] * mix[c] for c in easy) / sum(mix[c] for c in easy)
    print(f"  Simpson's paradox in one line: the canary looks {d6.pct(0.818 - 0.738, 1)} better than baseline overall and "
          f"is within noise of it on the\n  only categories it serves. Baseline's success is {d6.pct(standardised)} on its own "
          f"traffic mix and {d6.pct(easy_only)} on the canary's mix.")
    print("  Compare arms on the same traffic (standardise to one category mix, or compare per category), never on\n"
          "  whatever traffic the router happened to send each of them.")


# ------------------------------------------------------------------------------------------ step 3
def slice_table(traces: list[dict], metric, label: str, alpha: float = 0.05) -> list[tuple[str, float, int, int]]:
    rows, flagged = [], []
    bonferroni = alpha / len(d6.CATEGORIES)
    for cat in d6.CATEGORIES:
        a = [metric(t) for t in v14(traces) if t["category"] == cat]
        b = [metric(t) for t in v15(traces) if t["category"] == cat]
        p = d6.fisher_exact(sum(b), len(b) - sum(b), sum(a), len(a) - sum(a))
        flag = "**" if p < bonferroni else ("*" if p < alpha else "")
        rows.append([cat, f"{sum(a)}/{len(a)}", d6.pct(d6.rate(a)), f"{sum(b)}/{len(b)}", d6.pct(d6.rate(b)),
                     f"{d6.rate(b) - d6.rate(a):+.1%}", f"{p:.4f}", flag])
        flagged.append((cat, p, len(a), len(b)))
    d6.table(rows, ["category", f"v14 {label}", "rate", f"v15 {label}", "rate", "delta", "Fisher p", "flag"])
    print(f"  * p < {alpha}   ** p < {bonferroni:.3f} (Bonferroni for {len(d6.CATEGORIES)} slices)")
    return flagged


def step_slices(traces: list[dict]) -> None:
    print("Generic metric - observable success (no failure, no human rewrite):")
    slice_table(traces, d6.observable_success, "ok")
    print("  One slice under alpha = 0.05: return_request (p = 0.037), and it does not survive Bonferroni. technical_support\n"
          "  moved -23 points on 14 traces (p = 0.12). With ten slices, one of them is usually red. Before a verdict: a mechanism.")
    print("\nWhat does a v15 return-request reply look like?")
    examples = [t for t in v15(traces) if t["category"] == "return_request" and d6.handoff_language(t)][:1]
    normal = [t for t in v14(traces) if t["category"] == "return_request"][:1]
    for t in normal + examples:
        print(f"  {t['trace_id']} ({d6.version_of(t)}, tools {t['tools']}):\n    \"{t['reply']}\"")
    print("  The v15 reply says 'escalated ... to a colleague' - and the tool list shows no escalate_to_human call. The\n"
          "  reply claims an action the trace did not take; the RMA was never opened. That is a metric nobody had:")
    print("\nDerived metric - hand-off language in a reply whose trace never escalated:")
    slice_table(traces, unexpected_handoff, "hand-offs")
    print("  return_request: 5/18 vs 0/44 on the strict version, 7/18 vs 0/44 if you count every hand-off sentence\n"
          f"  (Fisher p = {d6.fisher_exact(7, 11, 0, 44):.5f}). Survives any correction. The mechanism matches the release note "
          "('hand off ambiguous returns'):\n  the model read 'ambiguous' generously. technical_support has no mechanism: "
          "same issue mix, same tools -> monitor, don't block.")


# ------------------------------------------------------------------------------------------ step 4
def gate(traces: list[dict], as_of: str) -> str:
    """The release gate: per-slice non-inferiority with intervals, a minimum slice size, and zero-tolerance derived checks."""
    window = [t for t in traces if d6.day_of(t) <= as_of]
    base, cand = v14(window), v15(window)
    print(f"Gate on data up to {as_of}: {len(base)} baseline traces, {len(cand)} candidate traces")
    rows, verdict = [], "PASS"
    for cat in d6.CATEGORIES:
        a = [d6.observable_success(t) for t in base if t["category"] == cat]
        b = [d6.observable_success(t) for t in cand if t["category"] == cat]
        crit = [unexpected_handoff(t) for t in cand if t["category"] == cat]
        if len(b) < MIN_SLICE:
            status = "INSUFFICIENT"
        else:
            lo, _ = d6.wilson(sum(b), len(b))
            status = "REVIEW" if lo < d6.rate(a) - MARGIN else "ok"
        if sum(crit):
            status = "BLOCK"
        rows.append([cat, len(b), d6.pct(d6.rate(a)) if a else "-", d6.pct(d6.rate(b)) if b else "-",
                     d6.ci_text(*d6.wilson(sum(b), len(b))) if b else "-", sum(crit), status])
        if status == "BLOCK":
            verdict = "BLOCK"
        elif status == "REVIEW" and verdict != "BLOCK":
            verdict = "REVIEW"
    d6.table(rows, ["slice", "n (v15)", "v14", "v15", "v15 95% CI", "critical hand-offs", "status"])
    print(f"  release gate: {verdict}")
    return verdict


def step_gate(traces: list[dict]) -> None:
    print(f"Rules: a slice with fewer than {MIN_SLICE} candidate traces is INSUFFICIENT (it cannot pass or fail); a slice whose\n"
          f"  lower 95% bound sits more than {MARGIN:.0%} below baseline is REVIEW; a zero-tolerance derived check (a hand-off\n"
          "  the trace contradicts) is BLOCK on its own, no statistics needed - one is one too many.\n")
    gate(traces, FIRST_WEEK_END)
    print()
    gate(traces, d6.TODAY)
    print("  After one week most slices are INSUFFICIENT - honest, and a reason to keep the canary small until they are\n"
          "  not - but the critical check already says BLOCK. Averages need samples; a contradiction needs one trace.")


# ------------------------------------------------------------------------------------------ step 5
def step_timeline(traces: list[dict]) -> None:
    stream = sorted((t for t in v15(traces) if t["category"] == "return_request"), key=lambda t: t["ts"])
    outcomes = [d6.handoff_language(t) for t in stream]
    base = [d6.handoff_language(t) for t in v14(traces) if t["category"] == "return_request"]
    p0 = max(d6.rate(base), 0.02)
    result = d6.sprt(outcomes, p0=p0, p1=0.20)
    print(f"Sequential test on the v15 return-request stream ({len(stream)} traces since {ROLLOUT_DAY}): H0 hand-off rate "
          f"{p0:.0%} (baseline: {sum(base)}/{len(base)}), H1 20%.")
    cum = 0
    for i, (t, flag, llr) in enumerate(zip(stream, outcomes, result["path"]), 1):
        cum += flag
        mark = " <- crosses the upper boundary" if result["stopped_at"] == i else ""
        if i <= 6 or flag or mark:
            print(f"  {d6.day_of(t)}  {t['trace_id']}  {'HAND-OFF' if flag else '-':8}  cumulative {cum:>2}  LLR {llr:6.2f}{mark}")
    print(f"  boundaries: reject H0 above {result['upper']:.2f}, accept H0 below {result['lower']:.2f} (alpha 0.05, power 0.8)")
    print(f"  decision: {result['decision']} after {result['stopped_at']} traces, on {d6.day_of(stream[result['stopped_at'] - 1])}.")
    print("  A boundary crossing on day one of a rollout is a reason to look, not a proof - two hand-offs in a row is\n"
          "  weak evidence and the test knows it (a minimum of ~10 observations before acting is a common rule). By\n"
          "  2026-09-05, with 4 of 9 return replies handed off, nobody could have argued. The complaint that actually\n"
          "  surfaced it arrived three weeks after the rollout. The metric existed in the traces the whole time.")


def main() -> None:
    header("Lab 04 - Finding the regression that the average hid")
    print("Pure Python over the traces; no API calls. The hidden `_truth` field is never read.")
    traces = d6.load_traces()

    step(1, "What the release review saw")
    step_dashboard(traces)

    step(2, "Simpson's paradox: arms that see different traffic")
    step_simpson(traces)

    step(3, "Slice by category x prompt version, then find the mechanism")
    step_slices(traces)

    step(4, "The release gate that would have caught it")
    step_gate(traces)

    step(5, "When could it have been caught? A sequential test on the stream")
    step_timeline(traces)


if __name__ == "__main__":
    main()
