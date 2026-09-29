"""Lab 01 - What a large toolset costs: tokens, dollars and selection surface for 120 tools.

Objective
    Load Kestrel's 120-tool catalog, measure with `count_tokens` what the four ways of exposing it cost per request
    (every tool loaded, a hand-picked route, the core set plus tool search with the rest deferred, and a code-execution
    route), turn that into a daily bill at Kestrel's volume, reproduce the "doubled bill" of the first attempt, and
    measure the selection surface a request faces under each strategy.

Concepts
    tool-use system prompt, tokens per definition, `defer_loading`, deferred tools cost nothing until discovered,
    cache economics of a large tool block (write once, read at 0.1x), hand-picked routes, selection surface and
    near-duplicates, the four strategies compared

Run
    python advanced/day2_tools_at_scale/labs/01_catalog_cost.py [--quick]

What to observe
    * `count_tokens`: the 120 loaded tools cost about nine times the core set; deferred tools add ~0 until discovered.
    * The daily bill of the tool block alone, uncached and cached, at 1,600 requests/day.
    * The "first attempt" arithmetic: 11 -> 120 loaded tools almost doubles the cost of a typical request.
    * The selection surface: how many catalog tools compete for each task under each strategy, and the mock's
      selection accuracy on the same 30 tasks with 12 vs 120 tools loaded.
"""
# test: expect=four strategies
# test: args=--quick

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step  # noqa: E402

import _day2 as d2  # noqa: E402

REQUESTS_PER_DAY = 1_600          # Kestrel's copilot after field service, quality and fleet joined: ~400 conversations x 4 calls
TYPICAL_SYSTEM = 3_000            # tokens of system prompt and policy text in a typical request
TYPICAL_HISTORY = 2_500           # tokens of conversation and tool results a request carries on average
TYPICAL_OUTPUT = 800              # thinking + text per model call
QUESTION = [{"role": "user", "content": "Where is SO-10248? The consignee says the carrier has not scanned it since Friday."}]


def count(client, tools: list[dict]) -> int:
    return client.messages.count_tokens(model=MODEL, tools=tools, messages=QUESTION).input_tokens


def step_catalog() -> None:
    cat = d2.catalog()
    tools = d2.catalog_tools()
    print(f"{cat['count']} tools in {len(cat['domains'])} domains (generated {cat['generated_at']}); "
          f"{len(d2.core_names())} core (always loaded): {', '.join(d2.core_names())}")
    risk = {r: sum(1 for t in tools if t["meta"]["risk"] == r) for r in ("read", "write", "irreversible")}
    print(f"risk classes: {risk}; approval_required: {sum(1 for t in tools if t['meta']['approval_required'])}; "
          f"pii: {sum(1 for t in tools if t['meta']['pii'])}")
    print("near-duplicates on purpose (the selection traps of this day):")
    for a, b in (("get_shipment", "track_shipment"), ("issue_refund", "issue_credit_note"), ("schedule_pickup", "schedule_return_pickup")):
        print(f"  {a:<24} {d2.entry(a)['description'][:70]}")
        print(f"  {b:<24} {d2.entry(b)['description'][:70]}")
    sizes = sorted((len(__import__('json').dumps(d2.api_tool(t))) for t in tools))
    print(f"definition size: median {sizes[len(sizes) // 2]} chars, largest {sizes[-1]} chars (JSON, as sent)")


def step_count_tokens(client) -> dict[str, int]:
    none = client.messages.count_tokens(model=MODEL, messages=QUESTION).input_tokens
    core = count(client, d2.loaded_toolset(d2.core_names()))
    full = count(client, d2.loaded_toolset([t["name"] for t in d2.catalog_tools()]))
    deferred = count(client, d2.wide_toolset(d2.SEARCH_BM25))
    route = count(client, d2.domain_toolset("logistics"))
    eleven = count(client, __import__("kestrel.support_tools", fromlist=["TOOLS"]).TOOLS)
    counts = {"no tools": none, "first course: 11 support tools": eleven, "core only (9 tools)": core,
              "hand-picked route: core + logistics (19)": route, "core + tool search, 111 deferred": deferred,
              "all 120 tools loaded": full}
    rows = [[label, n, n - none] for label, n in counts.items()]
    d2.table(rows, ["request shape", "prompt tokens", "tool block"])
    print(f"\nThe question alone is {none} tokens; the tool block is everything above it (tool-use system prompt + definitions).")
    print(f"Deferred definitions are still sent in every request but cost {deferred - core} tokens on top of the core set - the "
          "search tool's own definition. They are billed only when discovered, at that point in the conversation.")
    if is_mock():
        print("[mock] token counts are the mock's estimate (~3.8 chars/token, 350 tokens of tool-use system prompt); the API's "
              "count is model-specific (286 tokens of overhead on Opus 5) - expect different absolute numbers, similar ratios.")
    return counts


def step_daily_bill(counts: dict[str, int]) -> None:
    p_in = d2.input_price(MODEL)
    print(f"Tool block alone at {REQUESTS_PER_DAY:,} requests/day, {MODEL} input ${d2.get_spec(MODEL).input_price:.0f}/MTok:")
    rows = []
    none = counts["no tools"]
    for label in ("core only (9 tools)", "hand-picked route: core + logistics (19)", "core + tool search, 111 deferred", "all 120 tools loaded"):
        block = counts[label] - none
        uncached = block * REQUESTS_PER_DAY * p_in
        # cached: one 5-minute write per ~50 requests (traffic keeps the entry warm; a miss every few minutes is pessimistic)
        cached = block * p_in * (REQUESTS_PER_DAY * 0.1 + (REQUESTS_PER_DAY / 50) * 1.25)
        rows.append([label, block, d2.money(uncached), d2.money(cached)])
    d2.table(rows, ["strategy", "tool tokens/request", "per day, uncached", "per day, cached (0.1x reads)"])
    print("Caching turns the 120-tool block from a bill into a rounding error - IF the block is byte-identical on every request.\n"
          "Any per-route variation in `tools` (order, membership, a description tweak) is a full rewrite at 1.25x.")


def step_first_attempt(counts: dict[str, int]) -> None:
    p_in, p_out = d2.input_price(MODEL), d2.output_price(MODEL)
    none = counts["no tools"]
    before = counts["first course: 11 support tools"] - none
    after = counts["all 120 tools loaded"] - none
    rows = []
    for label, block in (("11 tools (before)", before), ("120 tools loaded (first attempt)", after)):
        inp = (TYPICAL_SYSTEM + TYPICAL_HISTORY + block) * p_in
        out = TYPICAL_OUTPUT * p_out
        rows.append([label, TYPICAL_SYSTEM + TYPICAL_HISTORY + block, d2.money(inp), d2.money(out), d2.money(inp + out),
                     d2.money((inp + out) * REQUESTS_PER_DAY)])
    d2.table(rows, ["request", "input tokens", "input cost", "output cost", "per request", "per day"])
    ratio = (rows[1][4], rows[0][4])
    before_total = (TYPICAL_SYSTEM + TYPICAL_HISTORY + before) * p_in + TYPICAL_OUTPUT * p_out
    after_total = (TYPICAL_SYSTEM + TYPICAL_HISTORY + after) * p_in + TYPICAL_OUTPUT * p_out
    print(f"\nThe first attempt (every tool always loaded, no caching because tools varied per route) multiplies a typical "
          f"request by x{after_total / before_total:.2f} - the 'doubled bill' of the case study, from the tool block alone.")


def _term_set(text: str) -> set[str]:
    words = re.findall(r"[a-z]{3,}", text.lower())
    return {w for w in words if w not in {"the", "and", "for", "with", "that", "this", "from", "what", "which", "them", "they"}}


def step_selection_surface(client, quick: bool) -> None:
    """How many tools compete for a request, and how the mock's selection accuracy moves with the number loaded."""
    tasks = d2.SELECTION_TASKS[:12] if quick else d2.SELECTION_TASKS
    all_names = [t["name"] for t in d2.catalog_tools()]
    docs = {n: _term_set(d2.entry(n)["name"].replace("_", " ") + " " + d2.entry(n)["description"]) for n in all_names}
    rows = []
    for text, expected in tasks[:6]:
        q = _term_set(text)
        competing_all = sum(1 for n in all_names if len(q & docs[n]) >= 2)
        competing_12 = sum(1 for n in d2.SELECTION_TOOLS if len(q & docs[n]) >= 2)
        rows.append([text[:58] + ("..." if len(text) > 58 else ""), expected, competing_12, competing_all])
    d2.table(rows, ["task", "expected", "competing of 12", "competing of 120"])
    print("(competing = tools sharing at least two content words with the request: the surface the model must discriminate)")

    system = d2.MARK_SELECT + "\nYou are Kestrel's operations copilot. Answer with exactly one tool call when a tool fits."
    results = {}
    for label, toolset in (("12 tools loaded", d2.loaded_toolset(d2.SELECTION_TOOLS)), ("120 tools loaded", d2.loaded_toolset(all_names))):
        hits = 0
        for text, expected in tasks:
            r = client.messages.create(model=MODEL, max_tokens=600, system=system, tools=toolset, messages=[{"role": "user", "content": text}])
            first = next((b.name for b in r.content if b.type == "tool_use"), None)
            hits += first == expected
        results[label] = hits
    d2.table([[label, f"{hits}/{len(tasks)}", f"{hits / len(tasks):.0%}"] for label, hits in results.items()],
             ["toolset", "first call correct", "accuracy"])
    if is_mock():
        print("[mock] the stand-in picks tools by lexical fit to the descriptions, so its accuracy is a proxy: more loaded tools = "
              "more near-duplicates competing. Anthropic reports the same direction for Claude: selection accuracy degrades once a "
              "request carries more than roughly 30-50 tools. Measure your own with lab 07's eval.")


def step_four_strategies(counts: dict[str, int]) -> None:
    none = counts["no tools"]
    rows = [
        ["all tools loaded", counts["all 120 tools loaded"] - none, "one write, then reads", "120", "none", "tiny catalogs (<10 tools), every tool used every time"],
        ["hand-picked per route", counts["hand-picked route: core + logistics (19)"] - none, "one entry PER route", "~19", "a router; cross-route tasks fail",
         "few, stable routes; a cheap classifier upstream"],
        ["deferred + tool search", counts["core + tool search, 111 deferred"] - none, "one entry for all", "9 + discovered", "one search turn (+latency, ~300 tokens)",
         "large or growing catalogs; multi-domain tasks"],
        ["code execution (PTC)", counts["core only (9 tools)"] - none + 120, "one entry for all", "9 + code-callable", "container startup; a script per task",
         "fan-out and chained calls with big intermediate results"],
    ]
    d2.table(rows, ["strategy", "tool tokens", "cache entries", "selection surface", "extra cost", "use when"])
    print("\nThe four strategies compose: Kestrel's copilot ends up with core tools loaded, the catalog deferred behind BM25 search,\n"
          "and the fleet/quality tools code-callable for the recall fan-out (labs 03 and 05).")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="score 12 selection tasks instead of 30")
    args = parser.parse_args()
    client = get_client()
    header("Lab 01 - What a large toolset costs")
    if is_mock():
        print("(mock mode: count_tokens and usage are the mock's estimates; ratios and mechanics are what to learn)")

    step(1, "The catalog: 120 tools, 12 domains, 9 core, near-duplicates on purpose")
    step_catalog()

    step(2, "count_tokens: core only vs a route vs deferred + search vs all 120 loaded")
    counts = step_count_tokens(client)

    step(3, "The daily bill of the tool block at Kestrel's volume")
    step_daily_bill(counts)

    step(4, "The first attempt: 11 -> 120 loaded tools on a typical request")
    step_first_attempt(counts)

    step(5, "Selection surface: how many tools compete for a request")
    step_selection_surface(client, args.quick)

    step(6, "The four strategies compared")
    step_four_strategies(counts)


if __name__ == "__main__":
    main()
