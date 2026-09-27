"""Solution to exercise 10 - a resilient worker: retry with backoff, fall back to another model, degrade explicitly.

Lab 04's plant workers run in parallel; a production run must survive one of them failing. Policy:
    1. Retry retryable errors (429, 5xx, 529 overloaded, timeouts, connection resets) with exponential
       backoff + jitter - but only a few times: the SDK already retried each HTTP call (max_retries).
    2. After the retries, fall back to a DIFFERENT model: overload and rate limits are per model, so the
       same request often succeeds elsewhere. (Quality may differ - the result records which model ran.)
    3. Non-retryable errors (400 bad request, 401/403, a schema the model could not satisfy) are not
       retried on the same input: they fail fast.
    4. If every option fails, return an explicit partial result. The synthesis still runs, and the
       report states which plant is missing instead of silently reading as "nothing found".

Faults are injected by a deterministic `Chaos` wrapper at the harness level (not by the mock), so the
same demonstration runs live and offline: plant-P2's worker hits two 529s on the primary model and
recovers on the fallback; plant-P3's worker fails everywhere and becomes a documented gap.

Run
    python day4_workflows_multi_agent/solutions/ex10_retry_fallback_worker.py
"""

# test: expect=fell back
# test: expect=INCOMPLETE

from __future__ import annotations

import asyncio
import random

import anthropic
import httpx2

import _labs
from _common import Tally, gather_bounded
from _quality import QualityDB, load_reports
from labkit import MID_MODEL, MODEL, get_async_client, header, step, wrap

lab04 = _labs.load("04_orchestrator_workers")

RETRYABLE = (anthropic.RateLimitError, anthropic.OverloadedError, anthropic.InternalServerError,
             anthropic.ServiceUnavailableError, anthropic.APITimeoutError, anthropic.APIConnectionError)


class Chaos:
    """Raise scripted API errors before the real call: failures[key] = [status, status, ...]."""

    def __init__(self, failures: dict[str, list[int]]) -> None:
        self.failures = {k: list(v) for k, v in failures.items()}

    def maybe_fail(self, key: str) -> None:
        queue = self.failures.get(key)
        if not queue:
            return
        status = queue.pop(0)
        response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
        error = {429: anthropic.RateLimitError, 529: anthropic.OverloadedError,
                 503: anthropic.ServiceUnavailableError}.get(status, anthropic.InternalServerError)
        raise error(f"simulated {status}", response=response, body=None)


async def resilient_worker(client, task, reports, *, models: list[str], attempts_per_model: int = 2,
                           base_delay: float = 0.05, chaos: Chaos | None = None, log: list[str]):
    """Returns (task, findings | None, response | None, model_used | None)."""
    for model in models:
        for attempt in range(1, attempts_per_model + 1):
            try:
                if chaos:
                    chaos.maybe_fail(task.worker_id)
                _, findings, response = await lab04.run_worker(client, task, reports, model)
                if model != models[0]:
                    log.append(f"{task.worker_id}: fell back to {model} and succeeded")
                return task, findings, response, model
            except RETRYABLE as exc:
                delay = base_delay * 2 ** (attempt - 1) * (1 + random.random())     # exponential backoff + jitter
                log.append(f"{task.worker_id}: {model} attempt {attempt} failed ({type(exc).__name__}); "
                           f"retry in {delay:.2f}s")
                await asyncio.sleep(delay)
            except (anthropic.BadRequestError, anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                log.append(f"{task.worker_id}: {model} non-retryable {type(exc).__name__} - not retrying this input")
                break
        log.append(f"{task.worker_id}: giving up on {model}")
    return task, None, None, None


async def main_async() -> None:
    header("Exercise 10 - resilient plant workers: retry, model fallback, explicit partial results")
    reports = load_reports()
    by_id = {r.report_id: r for r in reports}
    chaos = Chaos({"plant-P2": [529, 529], "plant-P3": [429, 529, 529, 500]})
    log: list[str] = []
    tally = Tally("run")
    async with get_async_client() as client:
        step(1, "Plan (as in lab 04)")
        plan = await lab04.make_plan(client, lab04.incident_index(reports), 4, tally)
        tasks, _ = lab04.validate_plan(plan, reports, 4)
        print(f"  {len(tasks)} workers: {', '.join(t.worker_id for t in tasks)}; chaos: {chaos.failures}")

        step(2, f"Workers with retries (x2) on {MID_MODEL}, then fallback to {MODEL}")
        results = await gather_bounded([lambda t=t: resilient_worker(client, t, by_id, models=[MID_MODEL, MODEL],
                                                                     chaos=chaos, log=log) for t in tasks], 4)
        for line in log:
            print(f"  {line}")
        findings, missing = [], []
        for task, found, response, model in results:
            if found is None:
                missing.append(task.plant)
                print(f"  {task.worker_id}: FAILED on every model -> partial result")
            else:
                tally.add(response)
                findings.append(found)
                print(f"  {task.worker_id}: ok on {model}; lots to verify {found.lots_to_verify or '-'}")

        step(3, "Synthesis on what we have - and a report that says what it is missing")
        report, _ = await lab04.synthesize(client, plan, findings, QualityDB(), tally)
        if missing:
            report.open_questions.insert(0, f"INCOMPLETE: plant(s) {', '.join(missing)} not analysed (worker failure); "
                                            "re-run before acting on 'no problem found' for those plants.")
            report.executive_summary = "INCOMPLETE - " + report.executive_summary
        print(wrap(report.executive_summary, "  "))
        for q in report.open_questions:
            print(wrap(f"open question: {q}", "  "))
        print(f"  lots identified: {sorted(p.lot for p in report.problems)} "
              "(VD-2607-C is missing because plant P3 was never analysed - exactly why the gap must be explicit)")


def main() -> None:
    random.seed(4)                      # deterministic jitter for the printed log
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
