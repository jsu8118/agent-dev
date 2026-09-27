"""Lab 04 - Orchestrator-workers: a cross-plant quality investigation.

Objective
    Nine incident reports from three plants hide two supplier-lot problems (MS-250 seal lot PS-2608-B,
    KC-2 board lot VD-2607-C) among red herrings. An orchestrator reads only the incident INDEX and
    writes a structured plan; plant workers analyse their own plant's reports in isolated contexts
    and return structured findings; a synthesizer verifies each lot hypothesis against the ERP extract
    (build records, RMAs) through a read-only SQL tool and writes the cross-plant report.

Concepts
    Orchestrator-workers; the plan as a structured artifact that code validates (spawn cap, every
    report assigned exactly once) before anything is spawned; context isolation and briefing quality;
    structured hand-offs instead of prose; a verification step with tools; a least-privilege DB tool
    (read-only connection + SQLite authorizer + row caps); per-stage model/effort; critical-path
    latency of plan -> parallel workers -> synthesis.

Run
    python day4_workflows_multi_agent/labs/04_orchestrator_workers.py [--worker-model claude-sonnet-5]
        [--max-workers 4]

What to observe
    * The plan: one task per plant, each with a brief and an out-of-scope line (prevents duplicate work).
    * Context isolation: each worker reads ~1/3 of the reports; the orchestrator reads none of them.
    * The synthesizer's SQL: it checks units built and RMAs per lot, plus a control query across all lots.
    * The final report names PS-2608-B and VD-2607-C with evidence and dismisses the red herrings.
"""

# test: expect=PS-2608-B
# test: expect=VD-2607-C

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from _common import Tally, critical_path, effort_kwargs, gather_bounded, mock_note, parsed, table, text_blocks
from _quality import EXPECTED_LOTS, QUERY_TOOL, RED_HERRINGS, IncidentReport, QualityDB, incident_index, load_reports
from labkit import MID_MODEL, MODEL, get_async_client, header, runs_dir, step, wrap

MAX_SYNTH_TURNS = 8


# ----------------------------------------------------------------------------- hand-off schemas
class WorkerTask(BaseModel):
    worker_id: str = Field(description="e.g. 'plant-P2'")
    plant: str
    report_ids: list[str]
    objective: str = Field(description="What this worker must find out")
    questions: list[str]
    out_of_scope: str = Field(description="What NOT to investigate (covered by another worker or by synthesis)")


class InvestigationPlan(BaseModel):
    summary: str
    tasks: list[WorkerTask]
    hypotheses: list[str] = Field(description="Cross-plant hypotheses the synthesis step must verify")


class Finding(BaseModel):
    report_id: str
    product_quality_related: bool
    issue: str
    part: str | None
    supplier: str | None
    lot: str | None = Field(description="Lot ID exactly as written in the report")
    shipped_exposure: Literal["none", "possible", "confirmed", "unknown"]
    severity: Literal["low", "medium", "high"]
    evidence: str = Field(description="Short verbatim quote from the report")


class PlantFindings(BaseModel):
    plant: str
    findings: list[Finding]
    lots_to_verify: list[str] = Field(description="Lots whose field exposure should be checked in ERP data")
    notes: str


class LotProblem(BaseModel):
    lot: str
    supplier: str
    part: str
    failure_mode: str
    evidence: list[str] = Field(description="Report IDs, RMA IDs and DB facts supporting the finding")
    units_built_in_sample: int
    affected_orders: list[str]
    customers: list[str]
    field_failures: list[str] = Field(description="RMA IDs linked to units from this lot")
    recommended_actions: list[str]


class Dismissal(BaseModel):
    report_id: str
    reason: str


class CrossPlantReport(BaseModel):
    executive_summary: str
    problems: list[LotProblem]
    dismissed: list[Dismissal]
    open_questions: list[str]


ORCHESTRATOR_SYSTEM = """<day4_quality_orchestrator>
You lead a cross-plant quality investigation at Kestrel Pumps & Controls. You see only an index of the
incident reports, not their contents. Plan the work for analyst workers; each worker reads its reports in
its own context and returns structured findings. Rules:
- One worker per plant (plants own their reports); at most {max_workers} workers in total.
- Assign every report to exactly one worker. Workers see nothing except their brief and their reports,
  so each brief must state the objective, the questions, and what is out of scope.
- List the cross-plant hypotheses the synthesis step must verify against ERP data (build records, RMAs).
</day4_quality_orchestrator>"""

WORKER_SYSTEM = """<day4_quality_worker>
You are a quality engineer analysing the incident reports of one Kestrel plant. Follow your brief. For
each report state: whether it is a product-quality issue; the part, supplier and lot exactly as written;
whether affected product has shipped (none / possible / confirmed / unknown - from the report alone);
severity; and a short verbatim quote as evidence. Say explicitly when facts in a report rule it out
as a cause of something else. List the lots whose field exposure should be verified in ERP data.
</day4_quality_worker>"""

SYNTHESIZER_SYSTEM = """<day4_quality_synthesizer>
You write Kestrel's cross-plant quality report from the workers' findings. Before claiming any field
exposure, verify it with query_quality_db: which units were built with the lot, which orders and
customers received them, and which came back as RMAs. Use a control comparison (other lots of the same
part) to separate a lot problem from background noise. Dismiss red herrings with the concrete reason.
The ERP extract is a representative sample; say so when counts matter. Finish with the report as JSON.
</day4_quality_synthesizer>"""


# ----------------------------------------------------------------------------- stages
async def make_plan(client, index: str, max_workers: int, tally: Tally) -> InvestigationPlan:
    response = await client.messages.parse(
        model=MODEL, max_tokens=8000, system=ORCHESTRATOR_SYSTEM.format(max_workers=max_workers),
        messages=[{"role": "user", "content": "<incident_index>\n" + index + "\n</incident_index>\n\nQuestion: are "
                   "there supplier-lot problems that span plants or reach customers? Plan the investigation."}],
        output_format=InvestigationPlan, **effort_kwargs(MODEL, "low"),
    )
    tally.add(response)
    return parsed(response, "plan")


def validate_plan(plan: InvestigationPlan, reports: list[IncidentReport],
                  max_workers: int) -> tuple[list[WorkerTask], list[str]]:
    """Code checks the plan BEFORE anything is spawned: cap, unknown IDs, duplicates, coverage."""
    known = {r.report_id: r for r in reports}
    warnings: list[str] = []
    tasks = plan.tasks
    if len(tasks) > max_workers:
        warnings.append(f"plan asked for {len(tasks)} workers; spawn cap is {max_workers} - extra tasks merged")
        head, tail = tasks[:max_workers], tasks[max_workers:]
        for extra in tail:
            head[-1].report_ids += extra.report_ids
        tasks = head
    assigned: set[str] = set()
    for task in tasks:
        cleaned = []
        for rid in task.report_ids:
            if rid not in known:
                warnings.append(f"{task.worker_id}: unknown report {rid} dropped")
            elif rid in assigned:
                warnings.append(f"{task.worker_id}: {rid} already assigned - duplicate work avoided")
            else:
                cleaned.append(rid)
                assigned.add(rid)
        task.report_ids = cleaned
    for rid, report in known.items():
        if rid not in assigned:
            owner = next((t for t in tasks if t.plant == report.plant), tasks[-1])
            owner.report_ids.append(rid)
            warnings.append(f"{rid} was not assigned - given to {owner.worker_id}")
    return [t for t in tasks if t.report_ids], warnings


def worker_messages(task: WorkerTask, reports: dict[str, IncidentReport]) -> list[dict]:
    """The worker's whole world: its brief and its plant's reports - nothing from other contexts."""
    docs = "\n\n".join(f'<incident_report id="{rid}">\n{reports[rid].text.strip()}\n</incident_report>'
                       for rid in task.report_ids)
    return [{"role": "user", "content": f"<brief>\n{task.model_dump_json(indent=1)}\n</brief>\n\n{docs}"}]


async def run_worker(client, task: WorkerTask, reports: dict[str, IncidentReport], model: str):
    response = await client.messages.parse(
        model=model, max_tokens=8000, system=WORKER_SYSTEM, messages=worker_messages(task, reports),
        output_format=PlantFindings, **effort_kwargs(model, "medium"),
    )
    return task, parsed(response, task.worker_id), response


async def context_tokens(client, model: str, messages: list[dict]) -> int:
    """Real token counts come from the token-counting endpoint (free; never a third-party tokenizer)."""
    counted = await client.messages.count_tokens(model=model, system=WORKER_SYSTEM, messages=messages)
    return counted.input_tokens


async def synthesize(client, plan: InvestigationPlan, findings: list[PlantFindings], db: QualityDB,
                     tally: Tally) -> tuple[CrossPlantReport, list[float]]:
    messages: list[dict] = [{"role": "user", "content": (
        "<investigation_plan>\n" + plan.model_dump_json(indent=1) + "\n</investigation_plan>\n\n<worker_findings>\n" +
        json.dumps([f.model_dump() for f in findings], indent=1) + "\n</worker_findings>\n\nVerify the lot hypotheses "
        "and write the cross-plant report.")}]
    output_config = {"format": {"type": "json_schema", "schema": anthropic.transform_schema(CrossPlantReport)},
                     **effort_kwargs(MODEL, "medium").get("output_config", {})}
    durations: list[float] = []
    for _ in range(MAX_SYNTH_TURNS):
        response = await client.messages.create(
            model=MODEL, max_tokens=16000, system=SYNTHESIZER_SYSTEM, tools=[QUERY_TOOL], messages=messages,
            output_config=output_config, cache_control={"type": "ephemeral"})
        durations.append(tally.add(response))
        if response.stop_reason == "refusal":
            raise RuntimeError("synthesizer declined")
        messages.append({"role": "assistant", "content": response.content})     # full content, thinking included
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "tool_use" and tool_uses:
            results = []
            for block in tool_uses:
                content, is_error = db.run(block.input)
                print(wrap(f"SQL ({block.input.get('purpose', '')}): {block.input.get('sql', '')}", "    "))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content,
                                "is_error": is_error})
            messages.append({"role": "user", "content": results})               # all results in ONE message
            continue
        if response.stop_reason == "max_tokens":
            raise RuntimeError("synthesizer ran out of max_tokens - raise it rather than parse a truncated report")
        return CrossPlantReport.model_validate_json(text_blocks(response)), durations
    raise RuntimeError(f"synthesizer did not finish within {MAX_SYNTH_TURNS} turns")


def render(report: CrossPlantReport) -> str:
    lines = ["# Cross-plant quality report", "", report.executive_summary, ""]
    for p in report.problems:
        lines += [f"## Lot {p.lot} - {p.part} ({p.supplier})",
                  f"*Failure mode:* {p.failure_mode}",
                  "",
                  f"*Units built in ERP sample:* {p.units_built_in_sample}; "
                  f"*orders:* {', '.join(p.affected_orders) or '-'}",
                  f"*Customers:* {', '.join(p.customers) or '-'}",
                  f"*Field failures (RMAs):* {', '.join(p.field_failures) or 'none recorded'}",
                  "*Evidence:*"]
        lines += [f"- {e}" for e in p.evidence]
        lines += ["*Actions:*"] + [f"- {a}" for a in p.recommended_actions] + [""]
    lines += ["## Dismissed"] + [f"- {d.report_id}: {d.reason}" for d in report.dismissed]
    if report.open_questions:
        lines += ["", "## Open questions"] + [f"- {q}" for q in report.open_questions]
    return "\n".join(lines)


async def main_async(args) -> None:
    header("Lab 04 - Orchestrator-workers: cross-plant quality investigation")
    mock_note("orchestrator, workers and synthesizer are rule-based stand-ins reading the same reports and query "
              "results a model would see; the DB tool and plan validation are the real code.")
    reports = load_reports()
    by_id = {r.report_id: r for r in reports}
    orch_t, worker_t, synth_t = Tally("orchestrator"), Tally("workers"), Tally("synthesizer")

    async with get_async_client() as client:
        step(1, f"Orchestrator ({MODEL}) plans from the index only")
        index = incident_index(reports)
        print(wrap(index, "    "))
        plan = await make_plan(client, index, args.max_workers, orch_t)
        tasks, warnings = validate_plan(plan, reports, args.max_workers)
        print(f"\n  plan: {plan.summary}")
        print(table([[t.worker_id, t.plant, ", ".join(t.report_ids), t.out_of_scope[:70]] for t in tasks],
                    ["worker", "plant", "reports", "out of scope"]))
        for h in plan.hypotheses:
            print(wrap(f"hypothesis: {h}", "  "))
        for w in warnings:
            print(f"  plan check: {w}")
        if not warnings:
            print("  plan check: cap respected, every report assigned exactly once")

        step(2, f"Workers ({args.worker_model}) run in parallel, each in its own context")
        results = await gather_bounded([lambda t=t: run_worker(client, t, by_id, args.worker_model) for t in tasks],
                                       args.max_workers)
        findings: list[PlantFindings] = []
        worker_durations = []
        for task, result in zip(tasks, results):
            if isinstance(result, BaseException):
                print(f"  {task.worker_id}: FAILED ({result}) - synthesis proceeds without this plant")
                continue
            _, found, response = result
            findings.append(found)
            worker_durations.append(worker_t.add(response))
            for f in found.findings:
                print(f"  [{task.worker_id}] {f.report_id}: {f.issue} | lot={f.lot or '-'} | shipped={f.shipped_exposure}"
                      f" | quality={f.product_quality_related}")
            print(f"  [{task.worker_id}] lots to verify: {found.lots_to_verify or '-'}")
        everything = WorkerTask(worker_id="all", plant="all", report_ids=list(by_id), objective="", questions=[],
                                out_of_scope="")
        rows = [[t.worker_id, len(t.report_ids),
                 f"{await context_tokens(client, args.worker_model, worker_messages(t, by_id)):,}"] for t in tasks]
        rows.append(["(one agent reading everything)", len(reports),
                     f"{await context_tokens(client, args.worker_model, worker_messages(everything, by_id)):,}"])
        print(table(rows, ["context", "reports read", "input tokens (count_tokens)"]))

        step(3, f"Synthesizer ({MODEL}) verifies lot hypotheses against the ERP extract")
        db = QualityDB()
        report, synth_durations = await synthesize(client, plan, findings, db, synth_t)

    step(4, "The cross-plant report")
    markdown = render(report)
    path = runs_dir("day4") / "cross_plant_quality_report.md"
    path.write_text(markdown, encoding="utf-8")
    print(wrap(markdown, "  "))
    print(f"\n  saved to {path}")

    step(5, "Check against the answer key")
    found_lots = {p.lot for p in report.problems}
    dismissed = {d.report_id for d in report.dismissed}
    for lot in sorted(EXPECTED_LOTS):
        print(f"  lot {lot}: {'identified' if lot in found_lots else 'MISSED'}")
    for rid, what in RED_HERRINGS.items():
        print(f"  red herring {rid} ({what}): {'dismissed' if rid in dismissed else 'NOT dismissed'}")
    extra = found_lots - EXPECTED_LOTS
    if extra:
        print(f"  false positives: {sorted(extra)}")

    step(6, "Cost and critical path per stage")
    for t in (orch_t, worker_t, synth_t):
        print(f"  {t.line()}")
    total = sum(t.cost_usd for t in (orch_t, worker_t, synth_t))
    path_s = orch_t.modelled_s + critical_path(worker_durations, args.max_workers) + sum(synth_durations)
    serial_s = orch_t.modelled_s + sum(worker_durations) + sum(synth_durations)
    print(f"  total ${total:.4f}; modelled latency {path_s:.1f}s with parallel workers vs {serial_s:.1f}s if the "
          f"workers ran one after another")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--worker-model", default=MID_MODEL, help="model for the plant workers")
    parser.add_argument("--max-workers", type=int, default=4, help="spawn cap enforced in code")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
