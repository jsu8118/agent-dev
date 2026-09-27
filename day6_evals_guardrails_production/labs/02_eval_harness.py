"""Lab 02 - An eval harness for the support agent: golden scenarios, code graders, a release gate.

Objective
    Measure Kestrel's reference support agent on the 30 golden scenarios with deterministic code
    graders (tool trajectory, end-state outcome, escalation queue, required / forbidden phrases),
    report per-type pass rates with confidence intervals plus cost and latency per scenario, and
    prove that the eval catches a realistic one-line bug before it ships.

Concepts
    Golden datasets; grading the END STATE (refund rows, RMAs, escalations) as well as the reply;
    critical (zero-tolerance) vs quality checks; Wilson confidence intervals; pass@k vs pass^k
    (--reps); paired comparison of two runs (flips, exact McNemar test); a release gate.

Run
    python day6_evals_guardrails_production/labs/02_eval_harness.py               # baseline, 30 cases
    python day6_evals_guardrails_production/labs/02_eval_harness.py --limit 10    # quick look
    python day6_evals_guardrails_production/labs/02_eval_harness.py --regression  # baseline + buggy candidate
    python day6_evals_guardrails_production/labs/02_eval_harness.py --regression return-window --reps 3
    python day6_evals_guardrails_production/labs/02_eval_harness.py --model claude-opus-5-5 --save-as opus-5-5 \
        --compare-to .runs/day6_reports/baseline/report.json                     # a model-migration candidate

What to observe
    * In mock mode every scenario passes, yet the 95% CI on the pass rate is 88.6-100%: 30 cases
      cannot resolve a small regression (exercise 3 does the sample-size math).
    * --regression flips E13 from PASS to FAIL with a CRITICAL outcome check (the agent now issues
      a $9,188.50 refund it must not issue).  McNemar p = 1.0 - one flip is not "significant" - yet
      the gate says BLOCK, because critical checks are zero-tolerance per case, not averaged.
    * Reports land in .runs/day6_reports/<run>/ (report.json, report.md, traces/<case>_rep<k>.json).
"""

# test: args=--regression
# test: expect=release gate: BLOCK
# test: expect=E13

from __future__ import annotations

import argparse
import contextlib
from collections import Counter
from pathlib import Path
from typing import Iterator

import _evalkit as ek
from kestrel import policy
from labkit import MODEL, get_client, get_spec, header, is_mock, print_json, step

# Realistic one-line bugs.  Each is the kind of change that sails through code review because the
# diff looks harmless - and each is invisible to unit tests of the agent loop.
REGRESSIONS = {
    "refund-limit": ("kestrel.policy.AGENT_REFUND_LIMIT = 15_000 (someone 'aligned' the agent's refund limit with "
                     "the Support Manager's)", "AGENT_REFUND_LIMIT", 15_000.00),
    "return-window": ("kestrel.policy.RETURN_WINDOW_DAYS = 90 (a holiday promotion hard-coded into the policy "
                      "module)", "RETURN_WINDOW_DAYS", 90),
}


@contextlib.contextmanager
def injected_bug(name: str) -> Iterator[str]:
    description, attribute, value = REGRESSIONS[name]
    original = getattr(policy, attribute)
    setattr(policy, attribute, value)          # tools read policy constants at call time
    try:
        yield description
    finally:
        setattr(policy, attribute, original)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, help="run only the first N scenarios")
    parser.add_argument("--ids", help="comma-separated scenario ids, e.g. E12,E13")
    parser.add_argument("--reps", type=int, default=1, help="repeat every scenario K times (pass@k / pass^k)")
    parser.add_argument("--concurrency", type=int, default=4, help="cases in flight at once")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--save-as", default="baseline", help="run name (report directory)")
    parser.add_argument("--regression", nargs="?", const="refund-limit", choices=sorted(REGRESSIONS),
                        help="also run a candidate with an injected bug and gate it against the baseline")
    parser.add_argument("--compare-to", type=Path, help="a previous report.json to compare this run with")
    return parser.parse_args()


def show_dataset(scenarios: list[ek.Scenario]) -> None:
    counts = Counter(s.type for s in scenarios)
    print(f"{len(scenarios)} scenarios: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    example = next((s for s in scenarios if s.id == "E13"), scenarios[0])
    print(f"\nExample {example.id} ({example.type}) from {example.from_email}:\n  \"{example.message}\"")
    print_json(example.expect)
    print("Graders: " + ", ".join(g.__name__ for g in ek.DEFAULT_GRADERS))


def run(client, scenarios, args, run_name: str) -> ek.EvalReport:
    report = ek.run_eval(client, scenarios, reps=args.reps, concurrency=args.concurrency, model=args.model,
                         run_name=run_name)
    for case in report.cases:
        ek.print_case_line(case)
    print()
    ek.print_summary(report)
    json_path, md_path = ek.write_reports(report)
    print(f"  reports: {json_path.parent}/ (report.json, report.md, traces/)")
    return report


def main() -> None:
    args = parse_args()
    get_spec(args.model)                                     # fail fast on a typo'd model id
    client = get_client()
    header(f"Lab 02 - eval harness: golden scenarios x code graders ({args.model})")
    if is_mock():
        print("[mock] The agent is labkit's rule-based stand-in, so these numbers test the HARNESS; run with an "
              "API key to measure Claude.")

    step(1, "Load the golden dataset")
    ids = args.ids.split(",") if args.ids else None
    scenarios = ek.load_scenarios(ids=ids, limit=args.limit)
    show_dataset(scenarios)

    step(2, f"Run the agent on every scenario (reps={args.reps}, concurrency={args.concurrency})")
    baseline = run(client, scenarios, args, args.save_as)

    if args.compare_to:
        step(3, f"Compare with {args.compare_to}")
        ek.print_comparison(ek.compare(ek.load_report(args.compare_to), baseline))

    if args.regression:
        step(4, f"Regression drill: inject '{args.regression}' and re-run the same scenarios")
        with injected_bug(args.regression) as description:
            print(f"Injected bug: {description}")
            candidate = run(client, scenarios, args, f"{args.save_as}-{args.regression}")
        step(5, "Release gate: paired comparison baseline -> candidate")
        comparison = ek.compare(baseline, candidate)
        ek.print_comparison(comparison)
        if comparison["verdict"] == "BLOCK":
            print("  REGRESSION DETECTED - the candidate must not ship. Note that the aggregate pass rate barely moved "
                  "and the McNemar test is far from significant: only the per-case critical check caught it.")


if __name__ == "__main__":
    main()
