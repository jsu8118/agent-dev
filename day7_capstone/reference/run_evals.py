"""Capstone reference - the go-live evaluation: 40 scenarios through the whole pipeline, then gates.

    python run_evals.py                      # E01-E30 (Day 6 set) + C01-C10 (capstone set)
    python run_evals.py --ids C01 C04 E17    # a subset (e.g. while debugging)
    python run_evals.py --repeats 3          # live mode: run everything 3 times (pass^k, see Day 6)

Writes .runs/capstone/eval_report.md (+ .json) and exits non-zero on NO-GO, so CI can gate a
deployment on it. Mock mode checks the mechanics (routing, gates, guards, idempotency, alerts);
only live mode tells you about quality, cost and latency. Live cost for one full run on Claude
Opus 5 is roughly $2-4.
"""
# test: expect=Decision: GO
# test: timeout=240

from __future__ import annotations

import argparse
import sys

from copilot import evals
from copilot.config import DEFAULT
from labkit import get_client, header, is_mock, step


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ids", nargs="*", help="only these scenario IDs")
    parser.add_argument("--support-only", action="store_true", help="only the Day 6 set (E01-E30)")
    parser.add_argument("--capstone-only", action="store_true", help="only the capstone set (C01-C10)")
    parser.add_argument("--repeats", type=int, default=1, help="run each scenario k times (all must pass)")
    parser.add_argument("--workers", type=int, default=4, help="scenarios run in parallel (live mode)")
    args = parser.parse_args()

    cases = evals.load_cases(support=not args.capstone_only, capstone=not args.support_only, ids=args.ids)
    client = get_client()
    header(f"Service Desk Copilot - go-live evaluation ({len(cases)} scenarios x {args.repeats})")
    if is_mock():
        print("MOCK MODE: this checks the pipeline's mechanics. Judge quality, cost and latency in live mode.")

    runs = [evals.run_suite(client, cases, config=DEFAULT, workers=args.workers) for _ in range(args.repeats)]
    results = runs[0]
    if args.repeats > 1:                     # pass^k: a scenario passes only if it passed in every run
        by_id = {r.id: r for r in results}
        for run in runs[1:]:
            for r in run:
                base = by_id[r.id]
                base.passed = base.passed and r.passed
                base.critical_failure = base.critical_failure or r.critical_failure
                base.failed_checks += r.failed_checks
                base.cost_usd = (base.cost_usd + r.cost_usd)
                base.latency_s = max(base.latency_s, r.latency_s)
        for r in results:
            r.cost_usd /= args.repeats

    rows = evals.acceptance(results, DEFAULT)
    report = evals.render_markdown(results, rows)
    step(1, "Report")
    print(report)
    md, js = evals.write_report(results, rows)
    print(f"Written: {md}\n         {js}")
    return 0 if evals.go_no_go(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
