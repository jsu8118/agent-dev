"""Solution to exercise 11 - a hard dollar budget for the orchestrator-workers pipeline of lab 04.

Policy (all in code; the model never sees the budget):
    1. Estimate every call BEFORE making it: input tokens from the token-counting endpoint (exact and
       free) + an assumed output (thinking + answer) priced with labkit's model catalog. The output
       assumptions below are planning numbers - calibrate them from your own runs' p90.
    2. Reserve a share of the budget for synthesis (it runs last and matters most).
    3. Rank worker tasks by expected value: plants with a High-severity *quality* report first.
       Spawn on the primary model if the estimate fits, else on the fast model, else skip the task.
    4. Budget the synthesizer's tool loop turn by turn. If the next turn does not fit, stop and ship
       the workers' findings marked UNVERIFIED instead of overspending.
    5. After the run, print estimate vs actual - the calibration loop.

Run
    python day4_workflows_multi_agent/solutions/ex11_orchestrator_budget.py [--budget 0.08]
"""

# test: expect=within budget
# test: expect=downgraded

from __future__ import annotations

import argparse
import asyncio
import json

import anthropic

import _labs
from _common import Tally, effort_kwargs, gather_bounded, table, text_blocks
from _quality import QUERY_TOOL, QualityDB, load_reports
from labkit import FAST_MODEL, MID_MODEL, MODEL, get_async_client, get_spec, header, step

lab04 = _labs.load("04_orchestrator_workers")
EXPECTED_OUTPUT = {"orchestrator": 1_500, "worker": 1_500, "synth_turn": 1_500}   # tokens, incl. thinking
SYNTH_RESERVE = 0.55             # share of the budget the workers may not touch


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    spec = get_spec(model)
    return (input_tokens * spec.input_price + output_tokens * spec.output_price) / 1_000_000


def priority(task, reports_by_id) -> tuple:
    reps = [reports_by_id[r] for r in task.report_ids]
    high_quality = sum(r.category.startswith("Quality") and r.severity.startswith("High") for r in reps)
    return (-high_quality, -sum(r.category.startswith("Quality") for r in reps), task.worker_id)


class Wallet:
    def __init__(self, budget: float) -> None:
        self.budget, self.spent = budget, 0.0
        self.tallies = {name: Tally(name) for name in ("orchestrator", "workers", "synthesizer")}

    def charge(self, stage: str, response) -> None:
        before = self.tallies[stage].cost_usd
        self.tallies[stage].add(response)
        self.spent += self.tallies[stage].cost_usd - before

    def fits(self, estimate: float, keep: float = 0.0) -> bool:
        return self.spent + estimate <= self.budget - keep


async def budgeted_synthesis(client, plan, findings, wallet: Wallet) -> tuple[object | None, str]:
    """Lab 04's synthesis loop with a budget check before every turn."""
    db = QualityDB()
    system = lab04.SYNTHESIZER_SYSTEM
    messages: list = [{"role": "user", "content": (
        "<investigation_plan>\n" + plan.model_dump_json(indent=1) + "\n</investigation_plan>\n\n<worker_findings>\n" +
        json.dumps([f.model_dump() for f in findings], indent=1) + "\n</worker_findings>\n\nVerify the lot hypotheses "
        "and write the cross-plant report.")}]
    output_config = {"format": {"type": "json_schema", "schema": anthropic.transform_schema(lab04.CrossPlantReport)},
                     **effort_kwargs(MODEL, "medium").get("output_config", {})}
    for turn in range(1, lab04.MAX_SYNTH_TURNS + 1):
        counted = await client.messages.count_tokens(model=MODEL, system=system, tools=[QUERY_TOOL], messages=messages)
        estimate = price(MODEL, counted.input_tokens, EXPECTED_OUTPUT["synth_turn"])
        if not wallet.fits(estimate):
            return None, f"stopped before synthesis turn {turn}: estimate ${estimate:.4f} > remaining " \
                         f"${wallet.budget - wallet.spent:.4f}"
        response = await client.messages.create(model=MODEL, max_tokens=16000, system=system, tools=[QUERY_TOOL],
                                                messages=messages, output_config=output_config)
        wallet.charge("synthesizer", response)
        messages.append({"role": "assistant", "content": response.content})
        uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "tool_use" and uses:
            results = []
            for block in uses:
                content, is_error = db.run(block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            continue
        return lab04.CrossPlantReport.model_validate_json(text_blocks(response)), f"verified in {turn} turn(s)"
    return None, "synthesis turn limit reached"


async def run(budget: float) -> None:
    reports = load_reports()
    by_id = {r.report_id: r for r in reports}
    wallet = Wallet(budget)
    async with get_async_client() as client:
        index = lab04.incident_index(reports)
        counted = await client.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": index}],
                                                     system=lab04.ORCHESTRATOR_SYSTEM)
        if not wallet.fits(price(MODEL, counted.input_tokens, EXPECTED_OUTPUT["orchestrator"])):
            print("  budget too small even for the plan - nothing run")
            return
        plan = await lab04.make_plan(client, index, 4, wallet.tallies["orchestrator"])
        wallet.spent += wallet.tallies["orchestrator"].cost_usd
        tasks, _ = lab04.validate_plan(plan, reports, 4)

        rows, approved, committed = [], [], 0.0
        for task in sorted(tasks, key=lambda t: priority(t, by_id)):
            chosen = None
            for model in (MID_MODEL, FAST_MODEL):
                tokens = await lab04.context_tokens(client, model, lab04.worker_messages(task, by_id))
                estimate = price(model, tokens, EXPECTED_OUTPUT["worker"])
                if wallet.fits(committed + estimate, keep=budget * SYNTH_RESERVE):
                    chosen = (model, estimate)
                    break
            if chosen is None:
                rows.append([task.worker_id, "SKIPPED (budget)", "-", "-"])
                continue
            committed += chosen[1]
            approved.append((task, chosen[0]))
            rows.append([task.worker_id, "run" if chosen[0] == MID_MODEL else "downgraded", chosen[0], f"${chosen[1]:.4f}"])
        print(table(rows, ["worker", "decision", "model", "estimate"]))
        results = await gather_bounded([lambda t=t, m=m: lab04.run_worker(client, t, by_id, m) for t, m in approved], 4)
        findings = []
        for result in results:
            if not isinstance(result, BaseException):
                findings.append(result[1])
                wallet.charge("workers", result[2])
        print(f"  workers: estimated ${committed:.4f}, actual ${wallet.tallies['workers'].cost_usd:.4f}")
        report, status = await budgeted_synthesis(client, plan, findings, wallet)

    print(f"  synthesis: {status}")
    if report is not None:
        print(f"  lots verified against the ERP extract: {sorted(p.lot for p in report.problems)}")
    else:
        flagged = sorted({lot for f in findings for lot in f.lots_to_verify})
        print(f"  UNVERIFIED lots flagged by the workers (no DB check - budget): {flagged}")
    skipped = [r[0] for r in rows if r[1].startswith("SKIPPED")]
    print(f"  plants skipped: {skipped or 'none'}")
    print(f"  spent ${wallet.spent:.4f} of ${budget:.3f} -> {'within budget' if wallet.spent <= budget else 'OVER BUDGET'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--budget", type=float, default=None, help="run once with this budget (USD)")
    args = parser.parse_args()
    header("Exercise 11 - a hard dollar budget for the orchestrator-workers pipeline")
    for n, budget in enumerate([args.budget] if args.budget else [0.50, 0.14, 0.07], 1):
        step(n, f"Run with a ${budget:.3f} budget")
        asyncio.run(run(budget))


if __name__ == "__main__":
    main()
