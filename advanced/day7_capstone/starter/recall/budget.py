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
        # TODO(M5): raise BudgetExceeded once spent_usd has reached cap_usd (mention the per-run reserve in the message)
        return None


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
        # TODO(M5): loop detection - the same (name, input) more than settings.loop_repeat_limit times returns an error
        #           result telling the model to stop and escalate, and records {"kind": "loop", ...} in self.tripped
        # TODO(M5): circuit breaker - once settings.circuit_breaker_error_rate of the last circuit_breaker_window calls
        #           failed, set self.open and return an error result for every further call (record the trip)
        #           (let ApprovalRequired propagate: a paused run is not a failure)
        return self.execute(name, tool_input, ctx)
