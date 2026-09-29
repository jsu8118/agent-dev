"""Lab 04 - Finding the regression: slices, Simpson's paradox, a derived metric, and the gate that catches it.

Objective
    Reproduce Kestrel's v15 incident from the traces alone. Start from the A/B dashboard the week-1 review saw
    (success held, CSAT flat, policy findings down), show a real Simpson's reversal between two arms that see
    different traffic, slice by category x prompt version with an exact test and multiple-comparison
    corrections, derive the metric that did not exist yet - hand-off language in a reply whose trace never
    called escalate_to_human - notice that the reviewers had already written it down, and run the release gate
    (non-inferiority on the difference of rates, a minimum slice size, a zero-tolerance check) that would have
    blocked v15 in its first week.

Concepts
    aggregate vs slice, concurrent comparisons, two changes in one arm, Simpson's paradox and standardisation,
    Fisher's exact test, Bonferroni / Holm / Benjamini-Hochberg, mechanism before verdict, derived metrics from
    reply text x tool calls, reviewer findings as a signal, non-inferiority margins with a Newcombe interval,
    minimum slice size, sequential detection on a live stream.

Run
    python advanced/day6_eval_science_release/labs/04_finding_the_regression.py

What to observe
    * Step 1 is the dashboard that said "keep going": on the concurrent A/B, v15 held success and CSAT within
      their intervals and cut policy findings - at more than twice the cost per trace, because v15 also changed
      model.
    * Step 2: the canary beats the opus-v15 arm by 11.5 points overall and ties or loses in every category it
      serves - Simpson's paradox, caused by the traffic mix.
    * Step 3: on the generic metric only return_request is under alpha = 0.05, and no correction keeps it. The
      derived hand-off metric (5/18 vs 0/44) survives every correction, and the reviewers had filed the same
      finding with passing scores.
    * Step 4: on the data available by 2026-09-06 the gate already says BLOCK (the zero-tolerance check) while
      every other slice is INSUFFICIENT; two weeks later several slices read REVIEW, their intervals still
      straddling the margin.
    * Step 5: a sequential test on the hand-off stream crosses its boundary on the first day of the rollout; the
      complaint that actually surfaced the bug came two weeks later.
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

MERGED_DAY = "2026-08-24"           # v15 merged after the offline golden set passed (narrative, not in the traces)
ROLLOUT_DAY = "2026-08-31"          # v15 went to 50% of traffic (deployments.json)
WEEK1_END = "2026-09-06"            # the data the week-1 A/B review looked at
COMPLAINT_DAY = "2026-09-14"        # the customer complaint that started the post-mortem (narrative)
MARGIN = 0.10                       # non-inferiority margin on a category slice (absolute points)
MIN_SLICE = 10                      # below this many candidate traces a slice cannot pass or fail
EASY = ("order_status", "product_inquiry")


def unexpected_handoff(trace: dict) -> bool:
    """The reply hands the customer off, but the trace never called escalate_to_human: a claim the trace contradicts."""
    return d6.handoff_language(trace) and "escalate_to_human" not in trace["tools"]


def v14(traces: list[dict]) -> list[dict]:
    return [t for t in traces if d6.version_of(t) == "v14"]


def v15(traces: list[dict]) -> list[dict]:
    return [t for t in traces if d6.version_of(t) == "v15"]


def window(traces: list[dict], lo: str, hi: str) -> list[dict]:
    return [t for t in traces if lo <= d6.day_of(t) <= hi]


# ------------------------------------------------------------------------------------------ step 1
def dashboard_rows(group: list[dict], name: str) -> list:
    succ = [d6.observable_success(t) for t in group]
    csat = [t["outcome"]["csat"] for t in group if t["outcome"]["csat"] is not None]
    reviews = [t["review"] for t in group if t["review"]]
    policy = sum("wrong_policy" in r["issues"] or "missing_citation" in r["issues"] for r in reviews)
    return [name, len(group), d6.pct(d6.rate(succ)), d6.ci_text(*d6.wilson(sum(succ), len(succ))),
            f"{statistics.fmean(csat):.2f}", f"{policy}/{len(reviews)}", d6.money(statistics.fmean(t["cost_usd"] for t in group))]


def step_dashboard(traces: list[dict]) -> None:
    headers = ["arm (A/B window)", "traces", "success", "95% CI", "mean CSAT", "policy/citation findings", "cost/trace"]
    for label, hi in (("at the week-1 review", WEEK1_END), ("on the post-mortem day", d6.TODAY)):
        ab = window(traces, ROLLOUT_DAY, hi)
        last = max(d6.day_of(t) for t in ab)
        print(f"The A/B dashboard {label}, concurrent traffic {ROLLOUT_DAY}..{last}:")
        d6.table([dashboard_rows(v14(ab), "v14 (baseline)"), dashboard_rows(v15(ab), "v15 (all v15 arms)")], headers)
    models = {v: sorted({t["deployment"]["model"] for t in traces if d6.version_of(t) == v}) for v in ("v14", "v15")}
    ratio = statistics.fmean(t["cost_usd"] for t in v15(traces)) / statistics.fmean(t["cost_usd"] for t in v14(window(traces, ROLLOUT_DAY, d6.TODAY)))
    print(f"  Release notes for v15: '{d6.load_deployments()['release_notes']['v15']}'")
    print("  The two things v15 set out to fix improved, success and CSAT held within their intervals, and the week-1\n"
          "  review kept it at 50%. Note the comparison is CONCURRENT (both arms since the rollout day): comparing v15\n"
          "  with all of v14's history would mix in the weeks before caching and a different ticket mix.")
    print(f"  Two changes in one arm: v14 runs on {', '.join(models['v14'])}, v15 on {', '.join(models['v15'])}. The cost\n"
          f"  column ({ratio:.1f}x) is mostly the model, and no v14/v15 comparison can separate what the prompt did from\n"
          "  what the model did. Only a mechanism (step 3) can attribute a finding to one of them.")


# ------------------------------------------------------------------------------------------ step 2
def step_simpson(traces: list[dict]) -> None:
    canary = [t for t in traces if d6.arm_of(t) == "haiku-fastpath"]
    opus = [t for t in traces if d6.arm_of(t) == "opus-v15"]
    k_c, k_o = sum(map(d6.observable_success, canary)), sum(map(d6.observable_success, opus))
    rows, weights = [], Counter(t["category"] for t in canary)
    opus_rates = {}
    for cat in EASY:
        c = [d6.observable_success(t) for t in canary if t["category"] == cat]
        o = [d6.observable_success(t) for t in opus if t["category"] == cat]
        opus_rates[cat] = d6.rate(o)
        rows.append([cat, f"{sum(c)}/{len(c)}", d6.pct(d6.rate(c)), f"{sum(o)}/{len(o)}", d6.pct(d6.rate(o)),
                     f"{d6.rate(c) - d6.rate(o):+.1%}"])
    rows.append(["ALL traffic", f"{k_c}/{len(canary)}", d6.pct(k_c / len(canary)), f"{k_o}/{len(opus)}",
                 d6.pct(k_o / len(opus)), f"{k_c / len(canary) - k_o / len(opus):+.1%}"])
    d6.table(rows, ["category", "haiku-fastpath", "rate", "opus-v15", "rate", "canary - opus"])
    easy_share = sum(t["category"] in EASY for t in opus) / len(opus)
    standardised = sum(weights[c] * opus_rates[c] for c in EASY) / sum(weights.values())
    print(f"  Simpson's paradox: the canary is {k_c / len(canary) - k_o / len(opus):+.1%} better overall and ties or loses in "
          "every category it serves.\n"
          f"  Cause: the router sends the canary only {' and '.join(EASY)} (100% of its traffic) while opus-v15\n"
          f"  gets them for {d6.pct(easy_share, 0)} of its traffic. On the canary's own traffic mix, opus-v15 scores "
          f"{d6.pct(standardised)} against the canary's {d6.pct(k_c / len(canary))}.")
    print("  The per-category counts are tiny (the reversal is arithmetic, not significance), but the rule is general:\n"
          "  compare arms on the same traffic - per category, or standardised to one mix (lab 06 uses observed /\n"
          "  expected) - never on whatever the router happened to send each of them.")


# ------------------------------------------------------------------------------------------ step 3
def slice_table(traces: list[dict], metric, label: str, alpha: float = 0.05) -> dict[str, float]:
    base, cand = v14(traces), v15(traces)
    pvalues, counts = {}, {}
    for cat in d6.CATEGORIES:
        a = [metric(t) for t in base if t["category"] == cat]
        b = [metric(t) for t in cand if t["category"] == cat]
        pvalues[cat] = d6.fisher_exact(sum(b), len(b) - sum(b), sum(a), len(a) - sum(a))
        counts[cat] = (a, b)
    holm, bh = d6.holm(pvalues, alpha), d6.benjamini_hochberg(pvalues, q=0.10)
    rows = []
    for cat in d6.CATEGORIES:
        a, b = counts[cat]
        rows.append([cat, f"{sum(a)}/{len(a)}", d6.pct(d6.rate(a)), f"{sum(b)}/{len(b)}", d6.pct(d6.rate(b)),
                     f"{d6.rate(b) - d6.rate(a):+.1%}", f"{pvalues[cat]:.4f}", "*" if pvalues[cat] < alpha else "",
                     "*" if holm[cat] else "", "*" if bh[cat] else ""])
    d6.table(rows, ["category", f"v14 {label}", "rate", f"v15 {label}", "rate", "delta", "Fisher p",
                    f"p<{alpha}", "Holm", "BH q=.10"])
    return pvalues


def step_slices(traces: list[dict]) -> None:
    print("Generic metric - observable success (no failure, no human rewrite):")
    p = slice_table(traces, d6.observable_success, "ok")
    raw = [c for c, v in p.items() if v < 0.05]
    worst = sorted(p, key=p.get)[:2]
    print(f"  Under alpha = 0.05: {', '.join(raw) or 'none'}; no correction keeps it. Next in line: {worst[1]} "
          f"(p = {p[worst[1]]:.2f}).\n  With ten slices, one of them is usually red by chance - before a verdict, "
          "find a mechanism.")

    print("\nWhat does a v15 return-request reply look like?")
    normal = [t for t in v14(traces) if t["category"] == "return_request"][:1]
    odd = [t for t in v15(traces) if t["category"] == "return_request" and unexpected_handoff(t)][:1]
    for t in normal + odd:
        print(f"  {t['trace_id']} ({d6.version_of(t)}, tools {t['tools']}):\n    \"{t['reply']}\"")
    print("  The v15 reply says it escalated the return to a colleague, and the tool list shows no escalate_to_human\n"
          "  call: nobody was assigned. It does show create_rma - an RMA exists that the customer was never told\n"
          "  about. The reply claims an action the trace did not take. That is a metric nobody had:")

    print("\nDerived metric - hand-off language in a reply whose trace never escalated:")
    p = slice_table(traces, unexpected_handoff, "hand-offs")
    returns = [t for t in v15(traces) if t["category"] == "return_request"]
    sentences = sum(d6.handoff_language(t) for t in returns)
    strict = sum(unexpected_handoff(t) for t in returns)
    print(f"  return_request: {strict}/{len(returns)} vs 0/{sum(t['category'] == 'return_request' for t in v14(traces))}, "
          f"Fisher p = {p['return_request']:.4f}, flagged under every correction. {sentences} replies use hand-off "
          f"language;\n  {sentences - strict} of them did call escalate_to_human (a ticket that needed a human), so the "
          "strict metric leaves them out.")
    print("  The mechanism matches the release note ('hand off ambiguous returns to a colleague'): the model read\n"
          "  'ambiguous' generously, and nothing in the prompt tied the sentence to the escalation tool.")

    print("\nThe reviewers had seen it. Finding types in the QA reviews, per prompt version:")
    kinds = sorted({i for t in traces if t["review"] for i in t["review"]["issues"]})
    counts, pvalues = {}, {}
    for kind in kinds:
        a = [kind in t["review"]["issues"] for t in v14(traces) if t["review"]]
        b = [kind in t["review"]["issues"] for t in v15(traces) if t["review"]]
        counts[kind] = (a, b)
        pvalues[kind] = d6.fisher_exact(sum(b), len(b) - sum(b), sum(a), len(a) - sum(a))
    holm = d6.holm(pvalues)
    d6.table([[kind, f"{sum(a)}/{len(a)}", f"{sum(b)}/{len(b)}", f"{pvalues[kind]:.4f}", "*" if holm[kind] else "",
               "new in v15" if sum(b) and not sum(a) else ""] for kind, (a, b) in counts.items()],
             ["finding", "v14 reviews", "v15 reviews", "Fisher p", "Holm", ""])
    flagged = sorted((t for t in v15(traces) if t["review"] and "unnecessary_escalation" in t["review"]["issues"]),
                     key=lambda t: t["ts"])
    scores = Counter(t["review"]["score"] for t in flagged)
    on_returns = [t for t in flagged if t["category"] == "return_request"]
    print(f"  A finding type that never appeared in {len(counts['unnecessary_escalation'][0])} v14 reviews shows up "
          f"{len(flagged)} times in {len(counts['unnecessary_escalation'][1])} v15 reviews, with\n  review scores "
          f"{dict(sorted(scores.items()))} - passing scores, so the score dashboard stayed green. The first one "
          f"({flagged[0]['trace_id']}, {d6.day_of(flagged[0])})\n  was on a {flagged[0]['category']} reply that hands "
          f"nobody off - reviewers over-flag too; the first on a return request came on {d6.day_of(on_returns[0])}.\n"
          "  Count findings by TYPE and alert on a type that is new for a version.")
    ha, hb = counts["hallucinated_id"]
    print(f"  hallucinated_id is up as well ({sum(hb)}/{len(hb)} vs {sum(ha)}/{len(ha)}), but it is an existing failure type "
          "and does not survive\n  Holm across seven types: monitor it (lab 07 watches it on the canary). technical_support "
          "moved on the generic\n  metric with no mechanism at all: same issue mix, same tools -> monitor, don't block.")


# ------------------------------------------------------------------------------------------ step 4
def gate(traces: list[dict], as_of: str) -> str:
    """The release gate: per-slice non-inferiority on the difference of rates, a minimum slice size, and a
    zero-tolerance derived check. Verdict order: BLOCK > FAIL > REVIEW > INSUFFICIENT > PASS."""
    ab = window(traces, ROLLOUT_DAY, as_of)
    base, cand = v14(ab), v15(ab)
    print(f"Gate on the concurrent A/B up to {as_of}: {len(base)} baseline traces, {len(cand)} candidate traces")
    rows, statuses = [], []
    for cat in d6.CATEGORIES:
        a = [d6.observable_success(t) for t in base if t["category"] == cat]
        b = [d6.observable_success(t) for t in cand if t["category"] == cat]
        critical = sum(unexpected_handoff(t) for t in cand if t["category"] == cat)
        lo, hi = d6.newcombe_diff_ci(sum(a), len(a), sum(b), len(b))
        if critical:
            status = "BLOCK"
        elif len(b) < MIN_SLICE or len(a) < MIN_SLICE:
            status = "INSUFFICIENT"
        elif hi < -MARGIN:
            status = "FAIL"
        elif lo < -MARGIN:
            status = "REVIEW"
        else:
            status = "ok"
        statuses.append(status)
        rows.append([cat, len(a), len(b), f"{d6.rate(b) - d6.rate(a):+.1%}" if a and b else "-",
                     f"{lo:+.0%}..{hi:+.0%}" if a and b else "-", critical, status])
    d6.table(rows, ["slice", "n v14", "n v15", "v15 - v14", "95% CI of difference", "critical", "status"])
    order = ["BLOCK", "FAIL", "REVIEW", "INSUFFICIENT"]
    verdict = next((s for s in order if s in statuses), "PASS")
    print(f"  release gate: {verdict}   ({', '.join(f'{s} {statuses.count(s)}' for s in order + ['ok'] if statuses.count(s))})")
    return verdict


def step_gate(traces: list[dict]) -> None:
    print(f"Rules, written down before the rollout:\n"
          f"  * a slice with fewer than {MIN_SLICE} traces in either arm is INSUFFICIENT - it can neither pass nor fail;\n"
          f"    hold the rollout percentage until it fills;\n"
          f"  * non-inferiority on the DIFFERENCE v15 - v14 with a {MARGIN:.0%} margin (95% Newcombe interval): ok if the\n"
          f"    whole interval lies above -{MARGIN:.0%}, FAIL if it lies below, REVIEW if it straddles the margin;\n"
          "  * a zero-tolerance derived check - a reply that claims a hand-off the trace never made - is BLOCK on its\n"
          "    own, no statistics needed: baseline has none, and one is a customer waiting for nobody.\n")
    gate(traces, WEEK1_END)
    print()
    gate(traces, d6.TODAY)
    ab = window(traces, ROLLOUT_DAY, d6.TODAY)
    a = [d6.observable_success(t) for t in v14(ab) if t["category"] == "return_request"]
    b = [d6.observable_success(t) for t in v15(ab) if t["category"] == "return_request"]
    lo, hi = d6.newcombe_diff_ci(sum(a), len(a), sum(b), len(b))
    print("  After one week most slices are INSUFFICIENT - honest, and a reason to keep the rollout small until they\n"
          f"  are not. Even today, return_request's generic interval ({lo:+.0%}..{hi:+.0%}) straddles the margin: "
          f"{len(a)} and {len(b)} traces\n  cannot prove a drop bigger than {MARGIN:.0%}. The critical check said BLOCK "
          "in the first week. Averages need\n  samples; a contradiction needs one trace.")


# ------------------------------------------------------------------------------------------ step 5
def step_timeline(traces: list[dict]) -> None:
    stream = sorted((t for t in v15(traces) if t["category"] == "return_request"), key=lambda t: t["ts"])
    outcomes = [d6.handoff_language(t) for t in stream]
    base = [d6.handoff_language(t) for t in v14(traces) if t["category"] == "return_request"]
    p0 = max(d6.rate(base), 0.02)
    result = d6.sprt(outcomes, p0=p0, p1=0.20)
    print(f"Sequential test on the v15 return-request stream ({len(stream)} traces since {ROLLOUT_DAY}): H0 hand-off rate "
          f"{p0:.0%} (baseline: {sum(base)}/{len(base)}, floored at 2%), H1 20%.")
    cum = 0
    for i, (t, flag, llr) in enumerate(zip(stream, outcomes, result["path"]), 1):
        cum += flag
        mark = " <- crosses the upper boundary" if result["stopped_at"] == i else ""
        if i <= 6 or flag or mark:
            print(f"  {d6.day_of(t)}  {t['trace_id']}  {'HAND-OFF' if flag else '-':8}  cumulative {cum:>2}  LLR {llr:6.2f}{mark}")
    print(f"  boundaries: reject H0 above {result['upper']:.2f}, accept H0 below {result['lower']:.2f} (alpha 0.05, power 0.8)")
    print(f"  decision: {result['decision']} after {result['stopped_at']} traces, on {d6.day_of(stream[result['stopped_at'] - 1])}.")
    by_week1 = [f for t, f in zip(stream, outcomes) if d6.day_of(t) <= "2026-09-05"]
    print(f"  A crossing after two traces is a reason to look, not a proof - many teams also require a minimum sample\n"
          f"  before acting. By 2026-09-05, {sum(by_week1)} of {len(by_week1)} v15 return replies had handed off and "
          "nobody could have argued.")
    reviewed = sorted((t for t in stream if t["review"] and "unnecessary_escalation" in t["review"]["issues"]),
                      key=lambda t: t["ts"])
    print(f"  Timeline: merged {MERGED_DAY}, rollout {ROLLOUT_DAY}, first reviewer finding on a return "
          f"{d6.day_of(reviewed[0])}, week-1 review\n  green, customer complaint {COMPLAINT_DAY}: three weeks after the "
          "merge, two weeks into the rollout. The metric existed\n  in the traces the whole time; nobody had defined it.")


def main() -> None:
    header("Lab 04 - Finding the regression that the average hid")
    print("Pure Python over the traces; no API calls. The hidden `_truth` field is never read.")
    traces = d6.load_traces()

    step(1, "What the A/B review saw")
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
