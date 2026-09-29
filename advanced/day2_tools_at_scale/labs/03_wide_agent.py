"""Lab 03 - The wide agent: 120 tools, nine loaded, the rest found by search when a task needs them.

Objective
    Run Kestrel's copilot over the full catalog - nine core tools loaded, 111 deferred behind the BM25 tool search tool -
    on five tasks that each need a tool outside the core. Watch it discover tools, call them and answer, with the
    discovery steps, prompt tokens and cache reads of every turn printed. Then run the same five tasks on a core-only
    agent (the nine tools, no search) and compare outcomes, turns and cost.

Concepts
    tool search in an agent loop, discovery inside one response (server_tool_use -> tool_search_tool_result -> tool_use),
    discovered definitions expanded in the conversation tail, one explicit cache breakpoint on the static prefix plus
    automatic caching for the tail, cache reads across turns and across conversations, limit and precision of a search,
    escalation when no tool covers a task

Run
    python advanced/day2_tools_at_scale/labs/03_wide_agent.py

What to observe
    * W1: the search and the core call happen in the same response; the discovered tool is called on the next turn.
    * After each agent's first request, every turn reads its tools+system prefix from the cache (wide: "read" >= 1,452).
    * The stand-in searches once per response: a multi-clause task (W4) gets one combined query and a larger `limit`,
      so more definitions land in the tail. Live Claude may instead search once per clause.
    * The core-only agent finishes the tasks it can only partly, or escalates them; its requests are cheaper.
"""
# test: expect=Outcome summary
# test: expect=escalated

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day2 as d2  # noqa: E402

BASE = (d2.MARK_WIDE + "\nYou are Kestrel's operations copilot for support, field service, quality and fleet staff. Nine tools "
        "are loaded.{search} Hand anything no tool can do to a person with escalate_to_human. Answer only from tool results.")
SEARCH_SENTENCE = (" When none of them covers part of a request, search the tool catalog (it covers orders, logistics, billing, "
                   "returns, field service, inventory, products, quality, customers, fleet telemetry, knowledge and "
                   "communications) with specific words, then use what you find.")
SYSTEM = BASE.format(search=SEARCH_SENTENCE)
CORE_SYSTEM = BASE.format(search="")


def run_tasks(client, tools: list[dict], system: str, label: str) -> dict[str, tuple[d2.RunResult, str]]:
    out = {}
    for task in d2.WIDE_TASKS:
        print(f"\n[{label}] {task['id']}: {task['text']}")
        ops = d2.KestrelOps()
        run = d2.run_agent(client, system=system, tools=tools, messages=[{"role": "user", "content": task["text"]}],
                           execute=ops.run, max_turns=6)
        result = d2.outcome(run, task["needs"])
        print(f"  -> {result}; answer:")
        print(wrap(run.reply, "     | "))
        out[task["id"]] = (run, result)
    return out


def main() -> None:
    client = get_client()
    header("Lab 03 - The wide agent: 120 tools, nine loaded")
    if is_mock():
        print("[mock] the stand-in plans one tool per clause from the tools it can see, searches when none covers a clause,\n"
              "       and fills arguments from the request and earlier results. Live Claude decides for itself - the loop and\n"
              "       the block shapes are the same, the choices and the wording will differ.")

    step(1, "The two toolsets")
    wide = d2.wide_toolset(d2.SEARCH_BM25)
    core = d2.loaded_toolset(d2.core_names())
    question = [{"role": "user", "content": d2.WIDE_TASKS[0]["text"]}]
    rows = []
    for label, tools, system in (("wide: search + 9 core + 111 deferred", wide, SYSTEM), ("core only: 9 tools, no search", core, CORE_SYSTEM),
                                 ("all 120 loaded (for reference)", d2.loaded_toolset(d2.all_names()), SYSTEM)):
        rows.append([label, len(tools), sum(1 for t in tools if t.get("defer_loading")),
                     d2.count_prompt(client, tools, question, system=system)])
    d2.table(rows, ["toolset", "tools declared", "deferred", "first-request prompt"])

    step(2, "Five tasks on the wide agent (turn by turn)")
    wide_runs = run_tasks(client, wide, SYSTEM, "wide")

    step(3, "The same five tasks on the core-only agent")
    core_runs = run_tasks(client, core, CORE_SYSTEM, "core")

    step(4, "Outcome summary")
    rows = []
    for task in d2.WIDE_TASKS:
        (w, wo), (c, co) = wide_runs[task["id"]], core_runs[task["id"]]
        rows.append([task["id"], wo, w.model_calls, len(w.searches), len(w.discovered), w.total("prompt"), d2.money(w.total("cost")),
                     co, c.model_calls, c.total("prompt"), d2.money(c.total("cost"))])
    d2.table(rows, ["task", "wide", "turns", "searches", "discovered", "prompt tok", "cost", "core only", "turns", "prompt tok", "cost"])
    used = sum(len(set(w.called) & set(w.discovered)) for w, _ in wide_runs.values())
    found = sum(len(w.discovered) for w, _ in wide_runs.values())
    print(f"\nDiscovery precision: {used} of the {found} definitions the searches returned were called "
          f"({used / found:.0%}). Every returned definition is expanded into the conversation and paid for on each later turn.")

    step(5, "Where the tokens went: the cached prefix vs the tail")
    w1 = wide_runs["W1"][0]
    for t in w1.turns:
        print(f"  W1 turn {t.n}: prompt {t.prompt:,} = cache read {t.cache_read:,} + cache write {t.cache_write:,} + uncached {t.input_tokens:,}")
    prefix = min(t.cache_read for w, _ in wide_runs.values() for t in w.turns if t.cache_read)
    print(f"\nThe tools+system prefix ({prefix:,} tokens) is written once and read by every later request - in this run by\n"
          "every other task's first turn too, because the tools array is byte-identical across conversations. The search\n"
          "result and the definitions it expanded are written once in the tail (turn 2's write) and read from then on.\n"
          "With every tool loaded, the same prefix would be ~12,000 tokens - still cacheable, but one byte of change in any\n"
          "definition rewrites all of it.")
    if is_mock():
        print("[mock] cache accounting follows the API's rules (prefix match, 5-minute TTL, 512-token minimum on Opus 5);\n"
              "       absolute token counts are the mock's estimates.")
    print(f"\nModel: {MODEL}.")


if __name__ == "__main__":
    main()
