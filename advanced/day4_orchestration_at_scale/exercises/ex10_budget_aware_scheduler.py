"""Exercise 10 starter - a budget-aware scheduler for concurrent workers.

Lab 03's gate was checked by one dispatcher between tasks. With several workers the naive gate (`spent < cap`,
checked before a claim) lets every worker that is already running finish, so the swarm overshoots by up to one task
per worker. Write a scheduler that treats the budget as a bound:

  * `reserve(worker)` before claiming: admit only if spent + reserved + estimate <= cap and the worker's own spend
    plus the estimate stays under the per-worker cap; the estimate is the p90 of the costs of completed tasks (a prior
    of $0.04 until three have completed); return the reserved amount, or None to stop that worker;
  * `cancel(ticket)` when no task was left to claim; `settle(ticket, worker, actual)` when a task finishes: release
    the reservation, add the actual cost, remember it for the next estimate.

To keep the comparison exact and repeatable, the harness measures each unit's real cost and modelled duration once
(one worker agent per unit, in mock mode), then replays the swarm on a simulated clock with N concurrent workers.

Run: python advanced/day4_orchestration_at_scale/exercises/ex10_budget_aware_scheduler.py
Solution: advanced/day4_orchestration_at_scale/solutions/ex10_budget_aware_scheduler.py
"""

from __future__ import annotations

import heapq
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import get_client, header, step  # noqa: E402

import _day4 as d4  # noqa: E402

PRIOR_USD = 0.04


@dataclass
class UnitCost:
    serial: str
    priority: int
    cost: float
    seconds: float


def measure_units(client) -> list[UnitCost]:
    """Run one worker agent per unit once and read its cost and modelled duration from its run log."""
    db = d4.fresh_db("ex10_measure.db")
    store, desk = RunStore(db), d4.RecallDesk()
    out = []
    for u in sorted(d4.units(), key=lambda u: (-d4.PRIORITY[u["risk_class"]], u["serial_number"])):
        result = d4.run_unit_worker(client, store, desk, [u["serial_number"]], run_id=f"measure:{u['serial_number']}")
        meter = d4.Meter()
        meter.add_run("worker", store, result.run_id)
        out.append(UnitCost(u["serial_number"], d4.PRIORITY[u["risk_class"]], meter.total().cost,
                            d4.run_seconds(store, result.run_id)))
    return out


class NaiveGate:
    """Lab 03's gate for several workers: a worker may claim while the SETTLED spend is under the cap."""

    def __init__(self, cap: float, per_worker_cap: float | None = None) -> None:
        self.cap, self.per_worker_cap = cap, per_worker_cap
        self.spent = 0.0

    def reserve(self, worker: str) -> float | None:
        return 0.0 if self.spent < self.cap else None

    def cancel(self, ticket: float) -> None:
        pass

    def settle(self, ticket: float, worker: str, actual: float) -> None:
        self.spent += actual


class BudgetAwareScheduler(NaiveGate):
    """TODO: admission control with reservations, a p90 estimate and a per-worker cap (see the module docstring)."""

    def estimate(self) -> float:
        raise NotImplementedError("estimate(): the p90 of completed task costs, or the $0.04 prior before three")

    def reserve(self, worker: str) -> float | None:
        raise NotImplementedError("reserve(): admit only if spent + reserved + estimate <= cap (and the per-worker cap)")

    def cancel(self, ticket: float) -> None:
        raise NotImplementedError("cancel(): give an unused reservation back")

    def settle(self, ticket: float, worker: str, actual: float) -> None:
        raise NotImplementedError("settle(): release the reservation, add the actual cost, learn from it")


def simulate(scheduler, units: list[UnitCost], workers: int) -> dict:
    """Replay the swarm on a simulated clock: each free worker reserves, claims the next unit by priority, runs for
    the unit's modelled duration, then settles its measured cost. Deterministic by construction."""
    queue = sorted(units, key=lambda u: (-u.priority, u.serial))
    clock: list[tuple[float, int, str, UnitCost | None, float]] = [(0.0, i, f"w{i + 1}", None, 0.0) for i in range(workers)]
    heapq.heapify(clock)
    done, stopped = [], set()
    while clock:
        t, i, worker, finished, ticket = heapq.heappop(clock)
        if finished is not None:
            scheduler.settle(ticket, worker, finished.cost)
            done.append(finished)
        if worker in stopped:
            continue
        ticket = scheduler.reserve(worker)
        if ticket is None:
            stopped.add(worker)
            continue
        if not queue:
            scheduler.cancel(ticket)
            stopped.add(worker)
            continue
        unit = queue.pop(0)
        heapq.heappush(clock, (t + unit.seconds, i, worker, unit, ticket))
    spent = sum(u.cost for u in done)
    return {"done": len(done), "spent": spent, "left": len(queue)}


def report(label: str, result: dict, cap: float) -> str:
    over = result["spent"] - cap
    return (f"  {label:<26} {result['done']:>2} units done, ${result['spent']:.4f} spent "
            f"({'over by $' + format(over, '.4f') if over > 0 else 'within the cap'}), {result['left']} left queued")


def main() -> None:
    header("Exercise 10 - a budget-aware scheduler (starter)")
    client = get_client()
    step(1, "Measure each unit once (one worker agent per unit)")
    units = measure_units(client)
    print(f"  {len(units)} units; cost per unit ${min(u.cost for u in units):.4f} - ${max(u.cost for u in units):.4f}; "
          f"modelled duration {min(u.seconds for u in units):.1f}-{max(u.seconds for u in units):.1f}s")

    step(2, "The naive gate with 1, 3 and 8 concurrent workers, cap $0.20")
    for workers in (1, 3, 8):
        print(report(f"naive gate, {workers} worker(s)", simulate(NaiveGate(0.20), units, workers), 0.20))

    step(3, "Your BudgetAwareScheduler")
    try:
        print(report("budget-aware, 3 workers", simulate(BudgetAwareScheduler(0.20, per_worker_cap=0.10), units, 3), 0.20))
    except NotImplementedError as exc:
        print(f"  TODO: {exc}")


if __name__ == "__main__":
    main()
