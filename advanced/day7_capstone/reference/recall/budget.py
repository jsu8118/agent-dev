"""Swarm economics and safety: the campaign's model-spend budget, a circuit breaker and loop detection.

The orchestrator checks the budget at run boundaries (every run is capped by max_turns and max_tokens, so
the overshoot is bounded), the `Guard` wraps a desk's executor so that inside a run a tool that keeps
failing or a model that keeps repeating the same call is stopped and told to escalate instead of burning
the budget.  All three are deliberately simple: the point is that they exist, trip visibly, and are logged.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any, Callable

from advanced.lib.durable import ApprovalRequired
from labkit import LEDGER

from .config import Settings


class BudgetExceeded(Exception):
    pass


class SwarmBudget:
    """Campaign-level cap on model spend: what earlier processes spent (persisted by the orchestrator) plus what
    this process has spent since, measured from labkit's ledger (the same numbers the labs print)."""

    def __init__(self, cap_usd: float, *, carried_usd: float = 0.0) -> None:
        self.cap_usd = cap_usd
        self.carried_usd = carried_usd
        self.baseline_usd = LEDGER.total_cost

    @property
    def spent_usd(self) -> float:
        return self.carried_usd + (LEDGER.total_cost - self.baseline_usd)

    def checkpoint(self) -> float:
        """Fold this process's spend into the carried total (the caller persists the returned value)."""
        self.carried_usd = self.spent_usd
        self.baseline_usd = LEDGER.total_cost
        return self.carried_usd

    @property
    def remaining_usd(self) -> float:
        return self.cap_usd - self.spent_usd

    def check(self, *, reserve_usd: float = 0.0) -> None:
        """Raise once the cap is reached. `reserve_usd` is what the next unit of work may cost at most: the check runs
        before each run, so the overshoot is bounded by one run, and the message says so."""
        if self.spent_usd >= self.cap_usd:
            raise BudgetExceeded(f"model spend ${self.spent_usd:.4f} has reached the cap ${self.cap_usd:.2f} "
                                 f"(each run is capped at about ${reserve_usd:.2f}, so the overshoot is bounded)")


class Guard:
    """Wraps a desk's `execute` with loop detection and a circuit breaker; records what tripped."""

    def __init__(self, execute: Callable[[str, dict, Any], Any], settings: Settings) -> None:
        self.execute = execute
        self.settings = settings
        self.seen: dict[str, int] = {}
        self.window: deque[bool] = deque(maxlen=settings.circuit_breaker_window)
        self.tripped: list[dict] = []
        self.open = False

    def __call__(self, name: str, tool_input: dict, ctx: Any) -> Any:
        signature = name + ":" + json.dumps(tool_input, sort_keys=True, default=str)
        self.seen[signature] = self.seen.get(signature, 0) + 1
        if self.seen[signature] > self.settings.loop_repeat_limit:
            self.tripped.append({"kind": "loop", "tool": name, "repeats": self.seen[signature]})
            return {"error": f"loop detected: {name} called {self.seen[signature]} times with the same input. Stop and escalate to a human "
                             f"with what you know; do not call it again."}
        if self.open:
            return {"error": "circuit open: too many tool failures in this run. Escalate to a human instead of retrying."}
        try:
            result = self.execute(name, tool_input, ctx)
        except ApprovalRequired:
            raise
        failed = isinstance(result, dict) and "error" in result
        self.window.append(failed)
        if len(self.window) == self.window.maxlen and sum(self.window) / len(self.window) >= self.settings.circuit_breaker_error_rate:
            self.open = True
            self.tripped.append({"kind": "circuit_breaker", "tool": name, "window": list(self.window)})
        return result
