"""Lab 07 - Subagents and sessions: a coordinator that delegates, specialists with isolated context.

Objective
    Split the incident investigation across two specialist subagents - a "log-analyst" that may only
    read the logs and a "runbook-checker" that may only use the deploy/runbook tools - coordinated by
    a main agent that is not allowed to read raw data at all. Then hold a multi-turn conversation with
    the same session (ClaudeSDKClient), resume it later from a new process (`resume=`), and prove from
    the transcripts on disk that each subagent worked in its own context.

Concepts
    `AgentDefinition` (description = when to use it, prompt = its system prompt, per-agent `tools`,
    `model`, `maxTurns`); the Agent tool; foreground vs background subagents; context isolation (a
    subagent starts fresh: its system prompt + the task string, never the parent's history); tool
    restrictions per agent + a hook that restricts the main thread; composing several PreToolUse hooks
    (most restrictive wins); `ClaudeSDKClient` for multi-turn sessions; `resume=session_id`;
    spawn-depth/concurrency/budget caps; per-agent attribution in hooks (`agent_type`).

Run
    python day5_mcp_agent_sdk/labs/07_agent_sdk_subagents.py

What to observe
    * Two Agent tool calls in one turn: the specialists run concurrently; their tool calls appear in the
      stream tagged [subagent] (messages carry parent_tool_use_id).
    * The coordinator's context receives two short reports, not the raw logs the log-analyst read.
    * Turn 2 (a status-page draft) uses no tools: the session remembers turn 1.
    * The resumed query starts a new CLI process yet answers from the stored session.
"""

# test: timeout=300
# test: expect=Context isolation

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    query,
)

from _agent_common import agent_runtime, show
from _mcp_common import load_lab
from labkit import MODEL, header, runs_dir, step

lab06 = load_lab("06_agent_sdk_hooks_and_custom_tools")     # reuse the SRE tools, guardrail and audit log

AGENTS = {
    "log-analyst": AgentDefinition(
        description="Reads the Kestrel Connect order-portal logs and returns a quantified UTC timeline of errors. "
                    "Use for any question that needs evidence from order-portal/*.log.",
        prompt="KESTREL-LOG-ANALYST\nYou analyse JSON-lines service logs in order-portal/. Use Grep with precise "
               "patterns and output_mode 'content' or 'count'; read whole files only if unavoidable. Return a compact "
               "UTC timeline with counts, first/last timestamps and the exact log fields that matter "
               "(status, latency_ms, pool_active, pool_max, waiting, version). No speculation about causes.",
        tools=["Read", "Grep", "Glob"],            # no MCP tools, no shell
        model="inherit",                           # try "haiku" or "sonnet" for cheaper specialists (live mode)
        maxTurns=12),
    "runbook-checker": AgentDefinition(
        description="Correlates incidents with the deploy history and the on-call runbooks. Use to find which "
                    "deploy/config change matches a symptom and what the runbooks prescribe.",
        prompt="KESTREL-RUNBOOK-CHECKER\nYou check deploy history and on-call runbooks. Use mcp__sre__get_deploys and "
               "mcp__sre__read_runbook only. Report: the most likely triggering deploy and its config diff, what "
               "each relevant runbook prescribes, and whether the response followed it.",
        tools=lab06.SRE_TOOLS,                     # only the two custom MCP tools - it cannot read log files
        model="inherit",
        maxTurns=8),
}

COORDINATOR_PROMPT = """KESTREL-SRE-COORDINATOR
You coordinate incident investigations for Kestrel Connect. You do not read logs or files yourself:
delegate evidence gathering to your subagents with the Agent tool, and run them in the foreground
(run_in_background: false) because you need their findings before you answer. Use log-analyst for
anything in the logs and runbook-checker for deploys and runbooks; launch both in parallel when the
tasks are independent. Give each subagent a self-contained task: it cannot see this conversation.
Synthesise their reports into a short answer with root cause, impact window (UTC), mitigation and evidence."""

INVESTIGATE = "Investigate the Kestrel Connect incident of 2026-09-14 and tell me what happened."
FOLLOW_UP = "Draft a short status-page update for customers about this incident. Plain language, no internals."
RESUMED = "In one sentence: what was the root cause?"


def build_options(env: dict[str, str], audit: Any, subagent_runs: list[dict], stderr_lines: list[str]) -> ClaudeAgentOptions:
    async def delegate_only(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        # Hook inputs carry agent_id only inside subagents: no agent_id = the coordinator itself.
        if "agent_id" not in input_data and input_data["tool_name"] not in ("Agent", "Task"):
            reason = "The coordinator does not read data itself: delegate to log-analyst or runbook-checker."
            audit.write(event="decision", decision="deny", reason=reason, agent="main",
                        tool=input_data["tool_name"], tool_use_id=tool_use_id)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        return {}

    async def subagent_stopped(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        subagent_runs.append({"agent": input_data["agent_type"], "agent_id": input_data["agent_id"],
                              "transcript": input_data.get("agent_transcript_path"),
                              "session_transcript": input_data.get("transcript_path")})
        return {}

    hooks = lab06.make_hooks(audit)                                  # path guard + audit, for every agent
    hooks["PreToolUse"].append(HookMatcher(hooks=[delegate_only]))   # parallel hooks: any deny wins
    hooks["SubagentStop"] = [HookMatcher(hooks=[subagent_stopped])]
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=COORDINATOR_PROMPT,
        agents=AGENTS,
        tools=["Read", "Grep", "Glob", "Agent"],       # the session's tool pool; subagents pick subsets of it
        mcp_servers={"sre": lab06.sre_server()},
        allowed_tools=["Read", "Grep", "Glob", "Agent", *lab06.SRE_TOOLS],
        disallowed_tools=["Read(./incident_ground_truth.json)"],
        permission_mode="dontAsk",
        hooks=hooks,
        cwd=str(lab06.OPS_LOGS),
        setting_sources=[],
        max_turns=20,
        max_budget_usd=5.0,
        env=env,
        stderr=stderr_lines.append,
    )


async def drain(client: ClaudeSDKClient) -> ResultMessage | None:
    """Print one turn. If Claude chose background subagents, keep reading until they have reported."""
    pending: set[str] = set()
    result = None
    while True:
        async for message in client.receive_response():
            show(message)
            if isinstance(message, TaskStartedMessage) and message.data.get("is_backgrounded"):
                pending.add(message.task_id)
            elif isinstance(message, TaskNotificationMessage):
                pending.discard(message.task_id)
            elif isinstance(message, ResultMessage):
                result = message
        if not pending:
            return result


def _entries(path: str | None) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _content(entry: dict) -> Any:
    return (entry.get("message") or {}).get("content")


def _tool_result_chars(entries: list[dict]) -> int:
    total = 0
    for e in entries:
        content = _content(e)
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                total += len(json.dumps(block.get("content"), ensure_ascii=False))
    return total


def report_isolation(subagent_runs: list[dict]) -> None:
    print(f"{'context':<16} {'entries':>7} {'tool calls':>10} {'tool-result chars':>17}  first user message / parent prompt visible?")
    for run in subagent_runs:
        entries = _entries(run["transcript"])
        first_user = next((_content(e) for e in entries if e.get("type") == "user"), "")
        first_user = first_user if isinstance(first_user, str) else json.dumps(first_user)[:200]
        calls = sum(1 for e in entries for b in (_content(e) if isinstance(_content(e), list) else [])
                    if isinstance(b, dict) and b.get("type") == "tool_use")
        text = json.dumps(entries, ensure_ascii=False)
        print(f"{run['agent']:<16} {len(entries):>7} {calls:>10} {_tool_result_chars(entries):>17}  "
              f"{first_user[:60]!r}... / {INVESTIGATE in text}")
    main = _entries(subagent_runs[0]["session_transcript"]) if subagent_runs else []
    agent_calls = sum(1 for e in main for b in (_content(e) if isinstance(_content(e), list) else [])
                      if isinstance(b, dict) and b.get("type") == "tool_use")
    print(f"{'coordinator':<16} {len(main):>7} {agent_calls:>10} {_tool_result_chars(main):>17}  "
          f"(its tool results are the subagents' final reports only)")


async def run() -> None:
    header(f"Lab 07 - Agent SDK: subagents, multi-turn sessions and resume ({MODEL})")
    audit = lab06.AuditLog(runs_dir("day5_audit") / f"lab07-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}.jsonl")
    subagent_runs: list[dict] = []
    stderr_lines: list[str] = []
    # Depth 1: subagents cannot spawn subagents. Concurrency cap: at most 4 at once.
    with agent_runtime("lab07", CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH="1",
                       CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS="4") as env:
        options = build_options(env, audit, subagent_runs, stderr_lines)
        try:
            async with ClaudeSDKClient(options=options) as client:
                step(1, f"Turn 1 - {INVESTIGATE}")
                await client.query(INVESTIGATE)
                first = await drain(client)
                step(2, f"Turn 2 (same session, no new evidence needed) - {FOLLOW_UP}")
                await client.query(FOLLOW_UP)
                second = await drain(client)
            session_id = first.session_id if first else None

            step(3, f"Resume the stored session from a new CLI process - {RESUMED}")
            resumed = None
            async for message in query(prompt=RESUMED, options=replace(options, resume=session_id)):
                show(message)
                if isinstance(message, ResultMessage):
                    resumed = message
        except Exception:
            print("CLI stderr (last lines):\n" + "\n".join(stderr_lines[-20:]))
            raise

    step(4, "Context isolation - evidence from the transcripts on disk")
    report_isolation(subagent_runs)

    step(5, "Who did what (audit log, attributed per agent)")
    records = audit.records()
    by_agent = Counter((r.get("agent", "main"), r["tool"],
                        f"hook-{r['decision']}" if r["event"] == "decision" else r["event"]) for r in records)
    for (agent, tool_name, event), n in sorted(by_agent.items()):
        print(f"  {agent:<16} {tool_name:<24} {event:<10} x{n}")

    step(6, "Cost across the session (estimates computed by the CLI)")
    previous = 0.0
    for label, res in (("turn 1", first), ("turn 2", second), ("resumed", resumed)):
        if res is not None and res.total_cost_usd is not None:
            # In a multi-turn session, and after resume, total_cost_usd is the session's running total.
            print(f"  {label:<8} session={res.session_id[:8]} turns={res.num_turns} running total ${res.total_cost_usd:.4f} "
                  f"(+${res.total_cost_usd - previous:.4f})")
            previous = res.total_cost_usd


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
