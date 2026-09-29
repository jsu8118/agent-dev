"""Run a representative subset of labs against the REAL Claude API, with a cost cap.

    ANTHROPIC_API_KEY=sk-... python scripts/smoke_live.py [--cap 3.00] [--model claude-opus-5] [--only day1]

Every lab prints a usage summary; this script parses each lab's TOTAL line and stops launching labs once
the cumulative (estimated, list-price) spend reaches --cap. A lab already running is not interrupted, so
the total can end slightly above the cap.  Use it after upgrading the SDK or changing
the default model, to confirm that the course still works end-to-end in live mode.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (script, extra args) - fast, representative, cheap.
LABS = [
    ("day1_foundations/labs/01_first_call.py", []),
    ("day1_foundations/labs/03_structured_extraction.py", []),
    ("day1_foundations/labs/04_triage_pipeline.py", ["--limit", "10"]),
    ("day1_foundations/labs/06_thinking_and_effort.py", []),
    ("day1_foundations/labs/07_errors_retries_refusals.py", []),
    ("day2_tools_agent_loop/labs/02_agent_loop_from_scratch.py", []),
    ("day2_tools_agent_loop/labs/06_support_agent.py", []),
    ("day3_context_rag_memory/labs/02_prompt_caching.py", []),
    ("day3_context_rag_memory/labs/03_rag_with_citations.py", []),
    ("day4_workflows_multi_agent/labs/01_prompt_chaining_invoices.py", ["--limit", "5"]),
    ("day5_mcp_agent_sdk/labs/03_claude_with_mcp_tools.py", []),
    ("day6_evals_guardrails_production/labs/02_eval_harness.py", ["--limit", "8"]),
    ("day7_capstone/reference/run_pipeline.py", ["--ticket", "T-1301", "--ticket", "T-1801"]),
    # the advanced course (select with --only advanced; ~$5 on claude-opus-5)
    ("advanced/day1_durable_agents/labs/02_crash_and_resume.py", []),
    ("advanced/day2_tools_at_scale/labs/03_wide_agent.py", []),
    ("advanced/day2_tools_at_scale/labs/05_programmatic_tool_calling.py", []),
    ("advanced/day3_long_horizon_context/labs/03_preserved_thinking_binding.py", []),
    ("advanced/day3_long_horizon_context/labs/07_cache_engineering_at_scale.py", []),
    ("advanced/day4_orchestration_at_scale/labs/05_managed_agents_sessions.py", []),
    ("advanced/day5_security_engineering/labs/02_injection_defense_in_depth.py", []),
    ("advanced/day6_eval_science_release/labs/03_pairwise_judges_and_bradley_terry.py", []),
    ("advanced/day7_capstone/reference/run_campaign.py", ["--fresh", "--days", "2", "--decide", "approve", "--db", "smoke.db"]),
]
TOTAL_RE = re.compile(r"^\s*TOTAL\s+calls=\d+\s+\$(\d+\.\d+)", re.M)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cap", type=float, default=3.00, help="stop before estimated spend exceeds this (USD)")
    parser.add_argument("--model", default=None, help="override LABKIT_MODEL for all labs")
    parser.add_argument("--only", default=None, help="substring filter, e.g. day1")
    args = parser.parse_args()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("No ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN set - this script only makes sense in live mode.")
        return 2
    env = {**os.environ, "LABKIT_MODE": "live"}
    if args.model:
        env["LABKIT_MODEL"] = args.model
    spent, results = 0.0, []
    for script, extra in LABS:
        if args.only and args.only not in script:
            continue
        path = ROOT / script
        if not path.exists():
            results.append((script, "missing", 0.0, 0.0))
            continue
        if spent >= args.cap:
            results.append((script, "skipped (cap)", 0.0, 0.0))
            continue
        start = time.time()
        proc = subprocess.run([sys.executable, str(path), *extra], cwd=path.parent, env=env,
                              capture_output=True, text=True, timeout=900)
        match = TOTAL_RE.search(proc.stdout)
        cost = float(match.group(1)) if match else 0.0
        spent += cost
        status = "ok" if proc.returncode == 0 else f"FAILED ({proc.returncode})"
        results.append((script, status, cost, time.time() - start))
        print(f"{status:<14} ${cost:7.4f}  {time.time() - start:6.1f}s  {script}")
        if proc.returncode != 0:
            print(proc.stdout[-2000:], proc.stderr[-3000:], sep="\n")
    print(f"\nEstimated total spend: ${spent:.4f} (list prices; see each lab's usage summary)")
    return 0 if all(r[1] in ("ok", "skipped (cap)", "missing") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
