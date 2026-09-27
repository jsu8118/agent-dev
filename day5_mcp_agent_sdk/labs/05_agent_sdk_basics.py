"""Lab 05 - Claude Code as a library: a read-only incident investigator with the Claude Agent SDK.

Objective
    Kestrel's on-call engineers spend the first 30 minutes of every incident reading logs. Build an
    agent that investigates the 2026-09-14 Kestrel Connect incident on its own - searching 48 h of
    JSON logs from four services, the deploy history and the runbooks - and returns a structured
    incident report. Then grade the report against data/ops_logs/incident_ground_truth.json.

Concepts
    `query()` = one agent run of the Claude Code harness (agent loop, context management, built-in
    tools) in a child process. `tools` (what exists) vs `allowed_tools` (what is pre-approved) vs
    `disallowed_tools` (deny rules) vs `permission_mode` ("dontAsk" for headless runs); `cwd` as the
    agent's workspace; `setting_sources=[]` for hermetic runs; the `claude_code` system-prompt preset
    + `append`; structured output (`output_format`); the message stream (SystemMessage init,
    AssistantMessage, UserMessage tool results, ResultMessage with turns and cost).

Run
    python day5_mcp_agent_sdk/labs/05_agent_sdk_basics.py
    (mock mode needs no key: the real CLI runs, the "model" is labkit's mock served over HTTP)

What to observe
    * You write no loop and no tools: the harness plans, calls Glob/Grep/Read on real files, and stops.
    * Every tool call is visible in the stream; each result is fed back to the model automatically.
    * The ResultMessage carries num_turns, duration and an *estimated* cost (computed client-side).
    * The grader compares the structured report with the ground truth field by field, and checks
      that the agent never opened the answer key.
"""

# test: timeout=240
# test: expect=Graded against incident_ground_truth.json

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from pydantic import BaseModel, ConfigDict, Field

from _agent_common import agent_runtime, show
from labkit import DATA_DIR, MODEL, header, step, wrap

OPS_LOGS = DATA_DIR / "ops_logs"
ANSWER_KEY = OPS_LOGS / "incident_ground_truth.json"

PROMPT = ("Kestrel Connect customers saw errors on 2026-09-14. Investigate the incident end to end: what failed, "
          "when (UTC), why, what fixed it, and which alarming-looking signals were unrelated.")

# Appended to Claude Code's own system prompt (the `claude_code` preset), which already teaches the
# model how to use Glob/Grep/Read well. The first line is a unique marker the offline mock keys on.
APPEND = """KESTREL-SRE-INVESTIGATOR
You are the on-call investigation assistant for Kestrel Connect, Kestrel's B2B order portal.
The current directory holds everything you may use:
- order-portal/*.log: 48 h of JSON-lines logs (api-gateway, order-service, payment-service, inventory-service)
- deploys.csv: deploy history with config diffs
- runbooks/*.md: on-call runbooks
Never open incident_ground_truth.json: it is the answer key used to grade you.
Method: quantify before you conclude (counts, first/last timestamps); correlate errors with deploys; check the
runbooks; examine alarming-looking signals and say explicitly why they are or are not related; cite evidence
(file and timestamp). Timestamps are UTC. When done, return the structured report."""


class IncidentReport(BaseModel):
    """The structured output the agent must return (validated by the harness)."""
    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(description="One or two sentences: the causal chain")
    trigger_deploy_id: str = Field(description="Deploy ID that triggered the incident, e.g. D-1234")
    affected_service: str
    config_change: str = Field(description="The configuration change involved, e.g. 'x: 1 -> 2'")
    impact_start_utc: str = Field(description="ISO-8601 UTC timestamp when user impact began")
    impact_end_utc: str = Field(description="ISO-8601 UTC timestamp when user impact ended")
    error_count_5xx: int
    mitigation: str
    red_herrings: list[str] = Field(description="Signals that looked relevant but were not, with the reason")
    follow_ups: list[str]
    evidence: list[str]


def build_options(env: dict[str, str], stderr_lines: list[str]) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt={"type": "preset", "preset": "claude_code", "append": APPEND},
        tools=["Read", "Grep", "Glob"],            # availability: the only built-in tools in the model's context
        allowed_tools=["Read", "Grep", "Glob"],    # permission: pre-approved, so no prompt is ever needed
        disallowed_tools=["Read(./incident_ground_truth.json)"],   # deny rule: also applied to Grep/Glob (best effort)
        permission_mode="dontAsk",                 # headless: anything that would need approval is denied
        cwd=str(OPS_LOGS),                         # the agent's workspace: relative paths resolve here
        setting_sources=[],                        # hermetic: ignore ~/.claude and project settings/CLAUDE.md
        output_format={"type": "json_schema", "schema": IncidentReport.model_json_schema()},
        max_turns=30,                              # hard stop for runaway loops
        max_budget_usd=3.0,                        # hard stop for runaway spend (estimate-based)
        env=env,
        stderr=stderr_lines.append,
    )


async def investigate(options: ClaudeAgentOptions) -> tuple[ResultMessage | None, list[ToolUseBlock], set[str]]:
    result, tool_uses, failed_ids = None, [], set()
    async for message in query(prompt=PROMPT, options=options):
        show(message)
        if isinstance(message, AssistantMessage):
            tool_uses += [b for b in message.content if isinstance(b, ToolUseBlock)]
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            failed_ids |= {b.tool_use_id for b in message.content if isinstance(b, ToolResultBlock) and b.is_error}
        elif isinstance(message, ResultMessage):
            result = message
    return result, tool_uses, failed_ids


# --------------------------------------------------------------------------- grading
def _minutes(value: str) -> int | None:
    m = re.search(r"(\d{2}):(\d{2})", value or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def grade(report: IncidentReport, truth: dict) -> list[tuple[str, bool, str]]:
    """Field-by-field comparison with the ground truth (expected values are parsed from the truth file)."""
    deploy = re.search(r"\bD-\d{4}\b", truth["root_cause"]).group(0)
    service = re.search(r"\b[a-z]+-service\b", truth["root_cause"]).group(0)
    before, after = re.search(r"from (\d+) to (\d+)", truth["root_cause"]).groups()
    start, end = (int(h) * 60 + int(m) for h, m in re.findall(r"(\d{2}):(\d{2})", truth["impact"])[:2])
    mitigation_deploy = re.search(r"\bD-\d{4}\b", truth["mitigation"]).group(0)
    got_start, got_end = _minutes(report.impact_start_utc), _minutes(report.impact_end_utc)
    herring_keys = {"TLS": r"tls|certificate", "slow query": r"slow[- ]quer", "bot scan": r"wp-admin|bot"}
    herrings = " ".join(report.red_herrings).lower()
    return [
        ("trigger deploy", report.trigger_deploy_id == deploy, f"expected {deploy}, got {report.trigger_deploy_id}"),
        ("affected service", report.affected_service == service, f"expected {service}, got {report.affected_service}"),
        ("config change", bool(re.search(rf"{before}\D+{after}\b", report.config_change)),
         f"expected {before} -> {after}, got {report.config_change!r}"),
        ("impact start (+/-10 min)", got_start is not None and abs(got_start - start) <= 10,
         f"truth ~{start // 60:02d}:{start % 60:02d}, got {report.impact_start_utc}"),
        ("impact end (+/-10 min)", got_end is not None and abs(got_end - end) <= 10,
         f"truth ~{end // 60:02d}:{end % 60:02d}, got {report.impact_end_utc}"),
        ("mitigation", mitigation_deploy in report.mitigation or "rollback" in report.mitigation.lower(),
         f"expected rollback {mitigation_deploy}"),
        *[(f"red herring: {name}", bool(re.search(pattern, herrings)), "mentioned and dismissed"
           if re.search(pattern, herrings) else "not addressed") for name, pattern in herring_keys.items()],
    ]


async def run() -> None:
    header(f"Lab 05 - Agent SDK: read-only incident investigator ({MODEL})")
    stderr_lines: list[str] = []
    with agent_runtime("lab05") as env:
        options = build_options(env, stderr_lines)
        step(1, "The contract we give the harness")
        print(f"cwd={options.cwd}\ntools={options.tools}  allowed={options.allowed_tools}  "
              f"denied={options.disallowed_tools}  permission_mode={options.permission_mode}")
        print(f"system prompt = claude_code preset + {len(APPEND)} chars appended; output = JSON schema with "
              f"{len(IncidentReport.model_fields)} fields; limits: max_turns={options.max_turns}, "
              f"max_budget_usd={options.max_budget_usd}")

        step(2, "Run the agent and stream every message")
        started = datetime.now()
        try:
            result, tool_uses, failed_ids = await investigate(options)
        except Exception:
            print("CLI stderr (last lines):\n" + "\n".join(stderr_lines[-20:]))
            raise
        elapsed = (datetime.now() - started).total_seconds()

    step(3, "The structured report")
    if result is None or result.structured_output is None:
        print("No structured output (the run stopped early). Final text:\n" + wrap((result and result.result) or "-"))
        return
    report = IncidentReport.model_validate(result.structured_output)
    print(json.dumps(report.model_dump(), indent=2))

    step(4, "Graded against incident_ground_truth.json")
    truth = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))
    checks = grade(report, truth)
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name:<26} {detail}")
    # A denied attempt is fine (the deny rule did its job); a successful read would contaminate the grade.
    attempts = [b for b in tool_uses if "incident_ground_truth" in json.dumps(b.input)]
    leaked = [b for b in attempts if b.id not in failed_ids]
    print(f"  [{'PASS' if not leaked else 'FAIL'}] {'answer key not read':<26} "
          f"{len(tool_uses)} tool calls; {len(attempts)} targeted the answer key, {len(leaked)} succeeded")
    passed = sum(ok for _, ok, _ in checks) + (not leaked)
    print(f"\nScore: {passed}/{len(checks) + 1}.  Wall time {elapsed:.1f} s, {result.num_turns} turns, "
          f"estimated cost ${result.total_cost_usd or 0:.4f}. An on-call engineer needs ~30 minutes for the same triage.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
