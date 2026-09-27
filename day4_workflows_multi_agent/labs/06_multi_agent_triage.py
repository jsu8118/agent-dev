"""Lab 06 - Multi-agent triage: a lead agent delegates pump analysis to subagents vs. one agent alone.

Objective
    Triage 12 monitored pumps from 30 days of telemetry. Architecture A: a lead agent with a
    `delegate_to_analyst` tool; each call spawns an analyst subagent - its own tool loop, its own
    context window, a `query_telemetry` tool scoped to its pumps, and a hard budget. The lead merges
    the analysts' structured reports. Architecture B: a single agent with the same telemetry tool.
    Compare accuracy (vs data/maintenance/ground_truth.json), tokens, cost and latency.

Concepts
    Lead/orchestrator + subagents; delegation as a tool; briefing (the subagent sees ONLY the brief);
    context isolation; spawn caps, per-subagent budgets and an assignment ledger enforced by the harness
    (Claude Opus 5 delegates readily - the cap must be code, not a request); parallel execution of
    delegations issued in one turn; structured reports as the hand-off; token multiplication; when a
    single agent with parallel tool calls is the better design.

Run
    python day4_workflows_multi_agent/labs/06_multi_agent_triage.py [--spawn-cap 3] [--analyst-model claude-opus-5]

What to observe
    * The lead issues its delegations in ONE turn; the harness runs them concurrently.
    * Budgets: every analyst has a turn/tool-call/token budget; the lead has a spawn cap.
    * The comparison table: on 12 pumps both find the same faults here, and the multi-agent run
      spends more tokens - briefs, re-reads and the lead's merge are overhead. Multi-agent pays off
      when the work is broad, parallel and too big for one context; see the README's break-even discussion.
    * The largest prompt any single call saw - the context-isolation benefit in numbers.
"""

# test: expect=spawn cap
# test: expect=faults found

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import anthropic
from pydantic import BaseModel, Field

from _common import Tally, Timer, critical_path, effort_kwargs, gather_bounded, mock_note, table, text_blocks
from _telemetry import asset_ids, fleet_table, ground_truth, query_telemetry, telemetry_tool
from labkit import MODEL, get_async_client, header, step

Issue = Literal["none", "bearing_wear", "cavitation", "misalignment", "sensor_fault", "overload_right_of_bep",
                "imbalance", "other"]


class AssetAssessment(BaseModel):
    asset_id: str
    status: Literal["healthy", "watch", "action_required"]
    issue: Issue
    evidence: str = Field(description="The numbers and records that support the call")
    recommended_action: str
    confidence: float = Field(description="0.0-1.0")


class AnalystReport(BaseModel):
    assets: list[AssetAssessment]
    notes: str


class FleetTriage(BaseModel):
    summary: str
    assets: list[AssetAssessment]
    priorities: list[str] = Field(description="Ordered list of actions, most urgent first")


ANALYSIS_GUIDE = """How to analyse a pump:
- Start with the 'summary' view. Compare the weekly windows: a trend (vibration and bearing temperature
  rising together, accelerating) points to bearing wear; a step change that then stays flat points to
  maintenance-induced problems (check work orders - misalignment after coupling work, imbalance after
  impeller work); a large spread with a normal mean points to an intermittent problem (check the
  hour-of-day profile - cavitation shows as erratic vibration with pressure fluctuation and lower flow).
- Motor current rising with flow while bearing temperature stays flat: operation drifting right of BEP.
- Exactly 0.0 mm/s while running is a sensor fault, not a quiet machine; isolated spikes above 50 mm/s
  are electrical noise; standby pumps with a few test hours are not evidence of a fault.
- Judge against the zone limits in the summary; state the numbers you relied on."""

ANALYST_SYSTEM = """<day4_fleet_analyst>
You are a reliability-engineering analyst at Kestrel Pumps & Controls. You triage the pumps named in your
brief - and only those - using the query_telemetry tool, then return your report as JSON.
{guide}
Budget: at most {max_turns} turns and {max_tool_calls} tool calls; batch independent queries into one turn.
</day4_fleet_analyst>"""

SINGLE_SYSTEM = """<day4_fleet_single>
You are a reliability-engineering analyst at Kestrel Pumps & Controls. Triage every pump in the fleet using
the query_telemetry tool, then return the fleet triage as JSON (one assessment per pump, priorities first).
{guide}
Batch independent queries into one turn.
</day4_fleet_single>"""

LEAD_SYSTEM = """<day4_fleet_lead>
You lead the weekly condition-monitoring triage for Kestrel's monitored pump fleet. You do not read
telemetry yourself: you delegate analysis to analyst subagents with delegate_to_analyst, then merge their
reports into the fleet triage (JSON: one assessment per pump, priorities ordered by urgency and criticality).

Delegating to subagents
- Subagents multiply cost and time: each one re-establishes context, explores and reports back. Delegate
  only sizeable, independent groups of pumps (for example one site per analyst); never one pump per analyst.
- At most {spawn_cap} analysts per run and at most {max_assets} pumps per analyst; every pump goes to exactly
  one analyst. Issue all delegations in ONE message so they run in parallel.
- Brief each analyst precisely the first time: it sees only your brief, not this conversation.
- Commit to the delegation: do not re-derive an analyst's findings; merge them.
</day4_fleet_lead>"""


# ----------------------------------------------------------------------------- a generic, budgeted tool loop
@dataclass
class Budget:
    max_turns: int
    max_tool_calls: int = 1_000
    max_tokens: int = 10_000_000          # input + output tokens across the whole run


@dataclass
class AgentRun:
    name: str
    tally: Tally
    turns: int = 0
    tool_calls: int = 0
    status: str = "running"
    critical_s: float = 0.0               # modelled time on this agent's critical path (incl. awaited subagents)
    largest_prompt: int = 0
    children: list["AgentRun"] = field(default_factory=list)


ToolExecutor = Callable[[list[Any]], Awaitable[tuple[list[dict], float]]]


async def run_agent(client, *, name: str, model: str, system: str, tools: list[dict], task: str,
                    schema: type[BaseModel], budget: Budget, execute: ToolExecutor, effort: str | None = "medium"
                    ) -> tuple[BaseModel | None, AgentRun]:
    """Manual agent loop: full assistant content appended, all tool results in one user message, budgets in code."""
    run = AgentRun(name=name, tally=Tally(name))
    messages: list[dict] = [{"role": "user", "content": task}]
    output_config = {"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
                     **effort_kwargs(model, effort).get("output_config", {})}
    while True:
        if run.turns >= budget.max_turns or run.tally.input_tokens + run.tally.output_tokens >= budget.max_tokens:
            run.status = "budget_exhausted"                  # hard stop: the report is lost, the lead is told
            return None, run
        response = await client.messages.create(model=model, max_tokens=16000, system=system, tools=tools,
                                                messages=messages, output_config=output_config,
                                                cache_control={"type": "ephemeral"})
        run.turns += 1
        run.critical_s += run.tally.add(response)
        run.largest_prompt = max(run.largest_prompt, response.usage.input_tokens
                                 + (response.usage.cache_read_input_tokens or 0)
                                 + (response.usage.cache_creation_input_tokens or 0))
        if response.stop_reason == "refusal":
            run.status = "refused"
            return None, run
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "tool_use" and tool_uses:
            # Soft landing: calls beyond the tool budget are answered with an error instead of run, and the
            # agent is told to finish - a partial report beats a killed run.
            allowed = max(0, budget.max_tool_calls - run.tool_calls)
            run.tool_calls += min(allowed, len(tool_uses))
            results, waited_s = await execute(tool_uses[:allowed]) if allowed else ([], 0.0)
            results += [{"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                         "content": "Tool budget exhausted - not executed. Write your report from the data you have."}
                        for b in tool_uses[allowed:]]
            run.critical_s += waited_s
            content: list[dict] = list(results)
            if run.turns == budget.max_turns - 1:
                content.append({"type": "text", "text": "Budget: this is your last turn. Return your report now."})
            messages.append({"role": "user", "content": content})     # tool results first, then any text
            continue
        if response.stop_reason == "max_tokens":
            run.status = "max_tokens"
            return None, run
        run.status = "done"
        return schema.model_validate_json(text_blocks(response)), run


def telemetry_executor(allowed: set[str]) -> ToolExecutor:
    async def execute(tool_uses: list[Any]) -> tuple[list[dict], float]:
        results = []
        for block in tool_uses:
            try:
                if block.input.get("asset_id") not in allowed:      # scope re-checked in code, not just in the schema
                    raise PermissionError(f"{block.input.get('asset_id')} is not assigned to you")
                content, is_error = json.dumps(query_telemetry(block.input["asset_id"], block.input["view"])), False
            except (KeyError, ValueError, PermissionError) as exc:
                content, is_error = json.dumps({"error": str(exc)}), True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
        return results, 0.0
    return execute


# ----------------------------------------------------------------------------- the lead's delegation tool
class Delegator:
    """Spawns analyst subagents - and enforces the spawn cap, pump limits and the assignment ledger."""

    def __init__(self, client, *, cap: int, max_assets: int, model: str, budget: Budget, concurrency: int) -> None:
        self.client, self.cap, self.max_assets, self.model, self.budget = client, cap, max_assets, model, budget
        self.concurrency = concurrency
        self.assigned: dict[str, str] = {}          # asset -> analyst
        self.runs: list[AgentRun] = []
        self.rejections: list[str] = []

    def tool(self) -> dict:
        return {
            "name": "delegate_to_analyst",
            "description": (
                "Spawn ONE analyst subagent that triages the given pumps with telemetry tools in its own context and "
                "returns a structured report (status, issue, evidence, action per pump). Every call starts a new "
                f"analyst and costs a full tool loop. Limits: {self.cap} analysts per run, {self.max_assets} pumps per "
                "analyst, each pump to exactly one analyst. Call it for all groups in one message to run them in "
                "parallel. The analyst sees only `brief`."),
            "strict": True,
            "input_schema": {"type": "object",
                             "properties": {"asset_ids": {"type": "array", "items": {"type": "string", "enum": asset_ids()}},
                                            "brief": {"type": "string", "description": "Self-contained task description"}},
                             "required": ["asset_ids", "brief"], "additionalProperties": False},
        }

    def validate(self, assets: list[str], pending: int = 0) -> str | None:
        if len(self.runs) + pending >= self.cap:
            return (f"Rejected: spawn cap reached ({self.cap} analysts per run). Merge groups into the analysts you "
                    "already have, or finish with the reports you have.")
        if not assets or len(assets) > self.max_assets:
            return f"Rejected: an analyst takes 1-{self.max_assets} pumps; you sent {len(assets)}."
        taken = [a for a in assets if a in self.assigned]
        if taken:
            return f"Rejected: {', '.join(taken)} already assigned ({', '.join(sorted({self.assigned[a] for a in taken}))})."
        if len(set(assets)) != len(assets):
            return "Rejected: duplicate pumps in one delegation."
        return None

    async def execute(self, tool_uses: list[Any]) -> tuple[list[dict], float]:
        results: dict[str, dict] = {}
        approved: list[tuple[Any, str]] = []
        for block in tool_uses:
            assets = list(block.input.get("asset_ids", []))
            problem = self.validate(assets, pending=len(approved))
            if problem:
                self.rejections.append(problem)
                results[block.id] = {"type": "tool_result", "tool_use_id": block.id, "content": problem, "is_error": True}
                continue
            analyst = f"analyst-{len(self.runs) + len(approved) + 1}"
            for a in assets:
                self.assigned[a] = analyst                      # the ledger: no pump is analysed twice
            approved.append((block, analyst))
        outcomes = await gather_bounded([lambda b=b, n=n: self._spawn(n, b.input) for b, n in approved], self.concurrency)
        waited = []
        for (block, analyst), outcome in zip(approved, outcomes):
            if isinstance(outcome, BaseException):
                content, is_error = f"{analyst} failed: {outcome}", True
            else:
                report, run = outcome
                self.runs.append(run)
                waited.append(run.critical_s)
                if report is None:
                    content, is_error = f"{analyst} stopped ({run.status}) without a report.", True
                else:
                    content, is_error = report.model_dump_json(), False
            results[block.id] = {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}
        # The lead waits for the slowest analyst in this batch: that is the critical path, not the sum.
        return [results[b.id] for b in tool_uses], critical_path(waited, self.concurrency)

    async def _spawn(self, analyst: str, tool_input: dict):
        assets = list(tool_input["asset_ids"])
        system = ANALYST_SYSTEM.format(guide=ANALYSIS_GUIDE, max_turns=self.budget.max_turns,
                                       max_tool_calls=self.budget.max_tool_calls)
        task = f"<brief>\n{tool_input['brief']}\n</brief>\n\nYour pumps: {', '.join(assets)}"
        return await run_agent(self.client, name=analyst, model=self.model, system=system,
                               tools=[telemetry_tool(assets)], task=task, schema=AnalystReport, budget=self.budget,
                               execute=telemetry_executor(set(assets)))


# ----------------------------------------------------------------------------- scoring
def score(assessments: list[AssetAssessment]) -> dict:
    truth = ground_truth()
    by_asset = {a.asset_id: a for a in assessments}
    correct = sum((by_asset[a].issue if a in by_asset else "missing") == truth.get(a, {}).get("issue", "none")
                  for a in asset_ids())
    found = sum(a in by_asset and by_asset[a].issue == t["issue"] for a, t in truth.items())
    false_alarms = sum(a not in truth and a in by_asset and by_asset[a].status == "action_required" for a in asset_ids())
    return {"correct": correct, "found": found, "faults": len(truth), "false_alarms": false_alarms,
            "missing": [a for a in asset_ids() if a not in by_asset]}


def print_triage(triage: FleetTriage | None) -> None:
    if triage is None:
        print("  (no triage produced)")
        return
    truth = ground_truth()
    rows = [[a.asset_id, a.status, a.issue, truth.get(a.asset_id, {}).get("issue", "none"), a.evidence[:78]]
            for a in sorted(triage.assets, key=lambda x: x.asset_id)]
    print(table(rows, ["pump", "status", "issue", "ground truth", "evidence (truncated)"]))


async def main_async(args) -> None:
    header("Lab 06 - Multi-agent triage: lead + analyst subagents vs. a single agent")
    mock_note("the lead, analysts and single agent are rule-based stand-ins reading real tool results; budgets, "
              "caps, the ledger and the telemetry tool are the real harness.")
    fleet = fleet_table()
    analyst_budget = Budget(max_turns=5, max_tool_calls=20, max_tokens=120_000)

    step(1, "The fleet (what the lead sees: identity and context, no telemetry)")
    print(table([[p["asset_id"], p["site"], p["model"], p["criticality"], p["notes"][:60]] for p in fleet],
                ["pump", "site", "model", "crit", "notes"]))
    task = ("Triage these pumps for developing faults (data to 2026-09-14):\n<fleet>\n" + json.dumps(fleet, indent=1) +
            "\n</fleet>")

    async with get_async_client() as client:
        step(2, f"Architecture A - lead ({MODEL}) delegates to analysts ({args.analyst_model}); spawn cap {args.spawn_cap}")
        delegator = Delegator(client, cap=args.spawn_cap, max_assets=args.max_assets, model=args.analyst_model,
                              budget=analyst_budget, concurrency=args.spawn_cap)
        with Timer() as t_multi:
            lead_triage, lead_run = await run_agent(
                client, name="lead", model=MODEL, system=LEAD_SYSTEM.format(spawn_cap=args.spawn_cap,
                                                                             max_assets=args.max_assets),
                tools=[delegator.tool()], task=task, schema=FleetTriage, budget=Budget(max_turns=4),
                execute=delegator.execute)
        for run in delegator.runs:
            assets = [a for a, owner in delegator.assigned.items() if owner == run.name]
            print(f"  {run.name}: pumps {', '.join(assets)} | turns={run.turns} tool_calls={run.tool_calls} "
                  f"status={run.status} | {run.tally.input_tokens:,} in / {run.tally.output_tokens:,} out")
        for r in delegator.rejections:
            print(f"  rejected delegation: {r}")
        print_triage(lead_triage)

        step(3, f"Architecture B - one agent ({MODEL}) with the same telemetry tool")
        with Timer() as t_single:
            single_triage, single_run = await run_agent(
                client, name="single", model=MODEL, system=SINGLE_SYSTEM.format(guide=ANALYSIS_GUIDE),
                tools=[telemetry_tool(asset_ids())], task=task, schema=FleetTriage, budget=Budget(max_turns=8),
                execute=telemetry_executor(set(asset_ids())))
        print(f"  single agent: turns={single_run.turns} tool_calls={single_run.tool_calls} status={single_run.status}")
        print_triage(single_triage)

    step(4, "Compare against data/maintenance/ground_truth.json")
    multi_tally = Tally("multi")
    multi_tally.absorb(lead_run.tally)
    for run in delegator.runs:
        multi_tally.absorb(run.tally)
    rows = []
    for name, triage, tally, run, wall in (("A lead + analysts", lead_triage, multi_tally, lead_run, t_multi.seconds),
                                           ("B single agent", single_triage, single_run.tally, single_run, t_single.seconds)):
        s = score(triage.assets if triage else [])
        largest = max([run.largest_prompt] + [c.largest_prompt for c in delegator.runs] if run is lead_run
                      else [run.largest_prompt])
        rows.append([name, f"{s['correct']}/12", f"{s['found']}/{s['faults']}", s["false_alarms"], tally.calls,
                     f"{tally.input_tokens:,}", f"{tally.output_tokens:,}", f"${tally.cost_usd:.4f}",
                     f"{run.critical_s:.0f}s", f"{wall:.2f}s", f"{largest:,}"])
    print(table(rows, ["architecture", "issues right", "faults found", "false alarms", "calls", "input tok",
                       "output tok", "cost", "latency*", "wall-clock", "largest prompt"]))
    print("  * modelled critical path (subagents in one batch overlap); wall-clock is measured (~0 in mock mode)")
    if single_run.tally.input_tokens:
        ratio = multi_tally.input_tokens / single_run.tally.input_tokens
        print(f"  token multiplication: the multi-agent run read {ratio:.1f}x the input tokens of the single agent")

    step(5, "Harness guardrails (checked in code, whatever the model asks for)")
    probe = Delegator(None, cap=args.spawn_cap, max_assets=args.max_assets, model=args.analyst_model,
                      budget=analyst_budget, concurrency=1)
    probe.runs = list(delegator.runs)
    probe.assigned = dict(delegator.assigned)
    print(f"  a further delegation after {len(delegator.runs)} analysts -> {probe.validate(['GB-KP400-01'])}")
    probe.runs = []
    print(f"  one analyst for {args.max_assets + 1} pumps -> {probe.validate(asset_ids()[: args.max_assets + 1])}")
    print(f"  a pump that is already assigned -> {probe.validate(['HF-KP250-03'])}")
    print(f"  analyst budget: {analyst_budget.max_turns} turns, {analyst_budget.max_tool_calls} tool calls, "
          f"{analyst_budget.max_tokens:,} tokens; the analysts' telemetry tool is scoped to their own pumps")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--spawn-cap", type=int, default=3, help="max analyst subagents per run (enforced in code)")
    parser.add_argument("--max-assets", type=int, default=5, help="max pumps per analyst")
    parser.add_argument("--analyst-model", default=MODEL)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
