"""Exercise 9 starter - a context budget guard for tool-heavy agent loops.

The guard is called right before every model request (the `before_request` hook of the lab loop). Today it only
measures. Make it trim: when the request is over budget, replace the oldest tool results with short digests
(tools.digest_telemetry does it for telemetry) until it fits - without touching the newest results, and without
breaking tool_use / tool_result pairing.
Run: python day3_context_rag_memory/exercises/ex09_context_budget_guard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header  # noqa: E402

import _day3 as d3  # noqa: E402
import _tools as tools  # noqa: E402

BUDGET = 25_000


class BudgetGuard:
    def __init__(self, client, *, system, tools_: list[dict], budget: int) -> None:
        self.client, self.system, self.tools, self.budget = client, system, tools_, budget
        self.log: list[list] = []

    def count(self, messages: list[dict]) -> int:
        return self.client.messages.count_tokens(model=MODEL, system=self.system, tools=self.tools,
                                                 messages=messages).input_tokens

    def __call__(self, messages: list[dict], turn: int) -> None:
        size = self.count(messages)
        # TODO: if size > self.budget, digest the oldest tool results (whole turns at a time) until it fits
        self.log.append([turn, size, "yes" if size <= self.budget else "NO"])


def main() -> None:
    client = get_client()
    header("Exercise 9 - context budget guard (starter)")
    system = [{"type": "text", "text": tools.FLEET_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    guard = BudgetGuard(client, system=system, tools_=tools.FLEET_TOOLS, budget=BUDGET)
    d3.run_agent(client.messages.create,
                 params=dict(model=MODEL, max_tokens=12000, system=system, tools=tools.FLEET_TOOLS),
                 messages=[{"role": "user", "content": tools.FLEET_REQUEST}],
                 tools=tools.FLEET_TOOL_FUNCS, max_turns=10, before_request=guard)
    d3.table(guard.log, ["turn", "tokens sent", f"within {BUDGET:,}"])
    print("\nTODO: make the guard trim so every row says 'yes', then check the final report still covers all 12 "
          "pumps. Solution: day3_context_rag_memory/solutions/ex09_context_budget_guard.py")


if __name__ == "__main__":
    main()
