"""Capstone reference - the acceptance suite: three campaigns (uninterrupted, crashed-and-resumed, tiny budget)
and the gates from the brief -> GO / NO-GO.

    python run_evals.py                # prints every scenario check and gate; writes .runs/advanced_capstone/eval_report.md

In mock mode this proves the mechanics (screening, scoping, idempotency, approvals, budgets, durability), not
the quality of a live model's emails; the operations memo says how to get the live evidence.
"""
# test: expect=Decision: GO
# test: timeout=300

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from labkit import get_client, header, is_mock, runs_dir   # noqa: E402
from recall import evals                                    # noqa: E402
from recall.config import DEFAULT                           # noqa: E402


def main() -> int:
    header("Acceptance suite - Recall Campaign Orchestrator" + (" (mock mode)" if is_mock() else " (LIVE)"))
    start = time.time()
    report = evals.run_suite(get_client(), DEFAULT)
    print(evals.render(report))
    path = evals.write_report(report, runs_dir("advanced_capstone") / "eval_report.md")
    print(f"\nReport written to {path}  ({time.time() - start:.1f}s)")
    return 0 if report.go else 1


if __name__ == "__main__":
    sys.exit(main())
