"""Solution to exercise 10 - a budget-aware scheduler: reservations, a p90 estimate, a per-worker cap.

The naive gate compares the budget with what has been SETTLED, so every task already running when the cap is crossed
still runs: with N workers the overshoot is up to N tasks. Admission control spends the money before the work starts:
a worker reserves an estimate, the reservation counts against the cap until the task settles, and the estimate is
learned from the costs actually observed (p90, so one cheap early task does not make the scheduler optimistic). The
per-worker cap stops one misbehaving agent from eating the swarm's budget (lab 03's per-agent guard, at dispatch).

Run: python advanced/day4_orchestration_at_scale/solutions/ex10_budget_aware_scheduler.py
"""
# test: expect=within the cap

from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "labs"))
sys.path.insert(0, str(HERE.parents[1] / "exercises"))

from labkit import get_client, header, step  # noqa: E402

import ex10_budget_aware_scheduler as starter  # noqa: E402


class BudgetAwareScheduler(starter.NaiveGate):
    def __init__(self, cap: float, per_worker_cap: float | None = None) -> None:
        super().__init__(cap, per_worker_cap)
        self.reserved = 0.0
        self.samples: list[float] = []
        self.by_worker: dict[str, float] = {}
        self.denied: list[str] = []

    def estimate(self) -> float:
        if len(self.samples) < 3:
            return starter.PRIOR_USD
        ordered = sorted(self.samples)
        return ordered[math.ceil(0.9 * len(ordered)) - 1]

    def reserve(self, worker: str) -> float | None:
        estimate = self.estimate()
        if self.spent + self.reserved + estimate > self.cap:
            self.denied.append(f"{worker}: ${self.spent:.4f} spent + ${self.reserved:.4f} reserved + ${estimate:.4f} > cap")
            return None
        if self.per_worker_cap is not None and self.by_worker.get(worker, 0.0) + estimate > self.per_worker_cap:
            self.denied.append(f"{worker}: its own ${self.by_worker.get(worker, 0.0):.4f} + ${estimate:.4f} > per-worker cap")
            return None
        self.reserved += estimate
        return estimate

    def cancel(self, ticket: float) -> None:
        self.reserved -= ticket

    def settle(self, ticket: float, worker: str, actual: float) -> None:
        self.reserved -= ticket
        self.spent += actual
        self.samples.append(actual)
        self.by_worker[worker] = self.by_worker.get(worker, 0.0) + actual


def main() -> None:
    header("Exercise 10 - a budget-aware scheduler (solution)")
    units = starter.measure_units(get_client())
    print(f"  {len(units)} units measured; ${min(u.cost for u in units):.4f} - ${max(u.cost for u in units):.4f} each")

    step(1, "Same cap ($0.20), 1, 3 and 8 concurrent workers: the naive gate vs admission control")
    for workers in (1, 3, 8):
        print(starter.report(f"naive gate, {workers} worker(s)", starter.simulate(starter.NaiveGate(0.20), units, workers), 0.20))
        scheduler = BudgetAwareScheduler(0.20)
        print(starter.report(f"budget-aware, {workers} worker(s)", starter.simulate(scheduler, units, workers), 0.20))
        print(f"    first denial: {scheduler.denied[0] if scheduler.denied else '-'}")

    step(2, "A per-worker cap: no single agent may spend more than $0.08")
    scheduler = BudgetAwareScheduler(0.30, per_worker_cap=0.08)
    print(starter.report("budget-aware, 3 workers", starter.simulate(scheduler, units, 3), 0.30))
    print("    spend per worker: " + ", ".join(f"{w} ${v:.4f}" for w, v in sorted(scheduler.by_worker.items())))
    print(f"    first denial: {scheduler.denied[0] if scheduler.denied else '-'}")

    step(3, "What the reservation costs you")
    scheduler = BudgetAwareScheduler(0.20)
    result = starter.simulate(scheduler, units, 3)
    print(f"  stranded budget: ${0.20 - result['spent']:.4f} of $0.20 left unspent because the next unit's p90 estimate "
          f"(${scheduler.estimate():.4f}) did not fit. That is the price of never overshooting: at most one estimate's worth.")


if __name__ == "__main__":
    main()
