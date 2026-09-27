"""Solution to exercise 9 - a context budget guard that trims old tool results before each request.

The guard runs client-side, right before every model call (the `before_request` hook of the lab loop):
1. count the request with client.messages.count_tokens (the model's own tokenizer - never estimate by chars);
2. if it is over budget, replace the OLDEST tool results with short digests computed in code, one whole turn at a
   time (natural boundaries, fewer cache breaks), never touching the newest results the model has not read yet;
3. re-count; stop as soon as the request fits; if it still does not fit, say so loudly - that is the signal to
   escalate to compaction or to split the work across subagents, not to silently drop more.
tool_use / tool_result pairs are kept (only the result *content* changes), so the request stays valid.

Run: python day3_context_rag_memory/solutions/ex09_context_budget_guard.py [--budget 25000]
"""
# test: expect=Budget guard

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header, step, wrap  # noqa: E402

import _day3 as d3  # noqa: E402
import _tools as tools  # noqa: E402


def default_digest(content: str) -> str:
    if content.startswith("# asset "):
        return tools.digest_telemetry(content)
    if len(content) <= 400:
        return content
    return content[:300] + f" ... [trimmed by client: {len(content) - 300:,} more characters]"


class BudgetGuard:
    def __init__(self, client, *, model: str, system, tools_: list[dict], budget: int,
                 digest: Callable[[str], str] = default_digest, exclude_tools: set[str] = frozenset(),
                 min_chars: int = 2000) -> None:
        self.client, self.model, self.system, self.tools = client, model, system, tools_
        self.budget, self.digest = budget, digest
        self.exclude_tools, self.min_chars = set(exclude_tools), min_chars   # small or vital results stay verbatim
        self.log: list[dict] = []

    @staticmethod
    def _tool_names(messages: list[dict]) -> dict[str, str]:
        """tool_use id -> tool name (assistant content may hold SDK objects or dicts)."""
        names = {}
        for m in messages:
            if m["role"] != "assistant" or not isinstance(m["content"], list):
                continue
            for b in m["content"]:
                kind = b.get("type") if isinstance(b, dict) else getattr(b, "type", "")
                if kind == "tool_use":
                    bid = b.get("id") if isinstance(b, dict) else b.id
                    names[bid] = b.get("name") if isinstance(b, dict) else b.name
        return names

    def count(self, messages: list[dict]) -> int:
        return self.client.messages.count_tokens(model=self.model, system=self.system, tools=self.tools,
                                                 messages=messages).input_tokens

    def __call__(self, messages: list[dict], turn: int) -> None:
        before = size = self.count(messages)
        trimmed = 0
        if size > self.budget:
            names = self._tool_names(messages)
            for message in messages[:-1]:                   # oldest first; the last message holds unseen results
                if message["role"] != "user" or not isinstance(message["content"], list):
                    continue
                changed = False
                for block in message["content"]:
                    content = block.get("content") if isinstance(block, dict) else None
                    if (block.get("type") == "tool_result" and isinstance(content, str)
                            and len(content) >= self.min_chars and not content.startswith("[trimmed")
                            and names.get(block.get("tool_use_id")) not in self.exclude_tools):
                        short = self.digest(content)
                        if len(short) < len(content):
                            block["content"] = short
                            changed = True
                            trimmed += 1
                if changed:
                    size = self.count(messages)
                    if size <= self.budget:
                        break
        self.log.append({"turn": turn, "before": before, "after": size, "trimmed": trimmed})
        if size > self.budget:
            print(f"   ! turn {turn}: still {size:,} tokens after trimming everything trimmable - escalate "
                  "(compaction, or split the work across subagents)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=int, default=25_000)
    args = parser.parse_args()
    client = get_client()
    header("Solution 9 - Budget guard for a tool-heavy agent loop")
    system = [{"type": "text", "text": tools.FLEET_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    guard = BudgetGuard(client, model=MODEL, system=system, tools_=tools.FLEET_TOOLS, budget=args.budget,
                        exclude_tools={"list_assets"})   # the asset register is small and needed until the end

    step(1, f"The lab 06 fleet review with a {args.budget:,}-token budget")
    run = d3.run_agent(client.messages.create,
                       params=dict(model=MODEL, max_tokens=12000, system=system, tools=tools.FLEET_TOOLS,
                                   cache_control={"type": "ephemeral"}),
                       messages=[{"role": "user", "content": f"(review run: budget guard)\n{tools.FLEET_REQUEST}"}],
                       tools=tools.FLEET_TOOL_FUNCS, max_turns=10, before_request=guard)
    rows = [[e["turn"], e["before"], e["after"], e["trimmed"], "yes" if e["after"] <= args.budget else "NO"]
            for e in guard.log]
    d3.table(rows, ["turn", "tokens before", "tokens sent", "results trimmed", "within budget"])

    step(2, "Did the model keep what it needed?")
    reported = set()
    for line in run.final_text.splitlines():
        for a in re.findall(r"\b[A-Z]{2}-KP\d{3}X?-\d{2}\b", line):
            if re.search(r"\d+\.\d", line.replace(a, "")):
                reported.add(a)
    print(f"Assets reported with numbers: {len(reported)}/{len(d3.assets())} (standby units have none). "
          f"Cost {d3.money(run.cost)}, peak prompt {run.peak_prompt_tokens:,} tokens.")
    head = next((i for i, line in enumerate(run.final_text.splitlines()) if "fleet report" in line.lower()), 0)
    print(wrap("\n".join(run.final_text.splitlines()[head:head + 4])))
    print("\nDesign notes: trim whole turns (one cache break per prune, not per block); keep the newest results; "
          "never trim small or vital results (exclude_tools - a first version of this guard trimmed the asset "
          "register and the agent silently reviewed 8 pumps instead of 12); digest in code (free, deterministic) "
          "rather than asking the model to summarise; and treat 'still over budget' as an escalation signal.")


if __name__ == "__main__":
    main()
