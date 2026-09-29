"""Lab 01 - What a large toolset costs: tokens, cache behaviour and selection for 120 tools.

Objective
    Load Kestrel's 120-tool catalog and measure what each way of exposing it costs: every tool loaded, a hand-picked
    route per task type, the core set plus tool search with the rest deferred, and code execution. Count the tokens of
    each request shape with `count_tokens`, simulate a working day of Kestrel's traffic to see what the prompt cache does
    with each shape, reproduce the arithmetic of the "doubled bill", and watch the mock's tool selection degrade as the
    loaded set grows.

Concepts
    the tool-use system prompt, tokens per definition, `defer_loading` (deferred tools cost nothing until discovered),
    discovery cost (the search turn and the expanded definitions), prefix caching of the tools block (one entry per
    distinct tools array, 5-minute TTL, reads at 0.1x, writes at 1.25x), cache fragmentation across routes, a silent
    invalidator (tool order), selection accuracy vs toolset size, the four strategies compared

Run
    python advanced/day2_tools_at_scale/labs/01_catalog_cost.py

What to observe
    * `count_tokens`: 120 loaded tools cost about nine times the core set; 111 deferred tools add nothing.
    * The day simulation: one shared prefix stays warm all day; eight route prefixes miss far more often.
    * The first attempt: the same 120 tools cost little extra while the cache hits - and roughly double the bill once
      the tools array changes order between requests.
    * Selection accuracy of the stand-in falls as more tools are loaded, on the same 30 tasks.
"""
# test: expect=four strategies
# test: expect=doubled

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step  # noqa: E402

import _day2 as d2  # noqa: E402

CONVERSATIONS_PER_DAY = 400          # Kestrel's copilot after field service, quality and fleet joined
REQUESTS_PER_CONVERSATION = 4        # model calls per conversation (tool loop turns)
DAY_MINUTES = 600                    # a ten-hour working day
SYSTEM_TOKENS = 3_000                # system prompt and policy text (same in every request)
FIRST_USER = 500                     # the first user message
TURN_GROWTH = 700                    # tokens each turn appends (tool call + result)
OUTPUT = 800                         # output tokens per model call (thinking + text)
SEARCHING_SHARE = 0.7                # conversations that need a non-core tool (deferred strategy searches once)
WORKERS = 8                          # worker processes behind the copilot (the first attempt's deployment)
ROUTES = {"logistics": 0.25, "billing": 0.15, "field_service": 0.15, "fleet": 0.15, "quality": 0.10, "returns": 0.10,
          "customers": 0.05, "orders": 0.05}
QUESTION = [{"role": "user", "content": "Where is SO-10290? The customer says the carrier has not scanned it since Friday."}]


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
        print(f"  {a:<24} {d2.clip(d2.entry(a)['description'], 72)}")
        print(f"  {b:<24} {d2.clip(d2.entry(b)['description'], 72)}")
    sizes = sorted(len(json.dumps(d2.api_tool(t))) for t in tools)
    print(f"definition size: median {sizes[len(sizes) // 2]} characters, largest {sizes[-1]} (JSON, as sent)")


def step_count(client) -> dict[str, int]:
    from kestrel.support_tools import TOOLS as SUPPORT_TOOLS
    shapes = {"no tools": [],
              "first course: 11 support tools": SUPPORT_TOOLS,
              "core only (9 tools)": d2.loaded_toolset(d2.core_names()),
              "route: core + logistics (19)": d2.domain_toolset("logistics"),
              "core + BM25 search, 111 deferred": d2.wide_toolset(d2.SEARCH_BM25),
              "core + code execution (9 + 1)": d2.loaded_toolset(d2.core_names()) + [dict(d2.CODE_EXECUTION)],
              "all 120 tools loaded": d2.loaded_toolset(d2.all_names())}
    counts = {label: d2.count_prompt(client, tools, QUESTION) for label, tools in shapes.items()}
    none = counts["no tools"]
    d2.table([[label, n, n - none] for label, n in counts.items()], ["request shape", "prompt tokens", "tool block"])
    print(f"\nThe question alone is {none} tokens; the tool block is the rest (the tool-use system prompt + definitions).")
    print("Deferred definitions are sent in every request but cost nothing until a search surfaces them - they are\n"
          "billed from that point in the conversation, where the reference is expanded.")

    # one discovery: what a search turn adds to the conversation
    r = client.messages.create(model=MODEL, max_tokens=1500, tools=d2.wide_toolset(d2.SEARCH_BM25), messages=QUESTION,
                               system=d2.MARK_SEARCH + "\nSearch the tool catalog for the tool this request needs.")
    refs = [ref.tool_name for b in r.content if b.type == "tool_search_tool_result"
            for ref in (getattr(b.content, "tool_references", None) or [])]
    after = QUESTION + [{"role": "assistant", "content": r.content}, {"role": "user", "content": "go on"}]
    counts["one discovery"] = d2.count_prompt(client, d2.wide_toolset(d2.SEARCH_BM25), after) - counts["core + BM25 search, 111 deferred"]
    print(f"One search turn (it returned {len(refs)} references: {', '.join(refs)}) adds {counts['one discovery']:,} tokens "
          "to the conversation tail - the search blocks plus the expanded definitions.")
    if is_mock():
        print("[mock] counts are the mock's estimates (~3.8 characters per token, 350 tokens of tool-use system prompt). The\n"
              "       real count is model-specific: ratios transfer, absolute numbers do not. Where the live count_tokens\n"
              "       endpoint rejects a server tool, count_prompt() falls back to a max_tokens=1 request (a paid call).")
    return counts


# ------------------------------------------------------------------------------------ a day of traffic
def day_of_requests(seed: int = 2026) -> list[tuple[float, int, str, int]]:
    """(minute, conversation, route, turn) for every request of the day - deterministic."""
    rng = random.Random(seed)
    routes, weights = list(ROUTES), list(ROUTES.values())
    out = []
    for conv in range(CONVERSATIONS_PER_DAY):
        start = rng.uniform(0, DAY_MINUTES - 5)
        route = rng.choices(routes, weights)[0]
        out += [(start + turn * 1.0, conv, route, turn) for turn in range(REQUESTS_PER_CONVERSATION)]
    return sorted(out)


def simulate(requests: list, prefix_of, *, discovery: int = 0, workers_with_own_order: int = 0, seed: int = 7) -> dict:
    """Bill a day of requests under the prompt cache's rules. `prefix_of(route)` gives a request's tools-block tokens and
    the identity of its tools array. The prefix (tools + system) is read at 0.1x when an identical prefix was used in the
    last 5 minutes, else written at 1.25x; the conversation tail is read up to the previous turn and the new turn is
    written (automatic caching). `workers_with_own_order=N` models the first attempt's deployment: N worker processes,
    each building the tools array in its own order, requests load-balanced at random - a request can only read what an
    earlier request on the same worker wrote."""
    rng = random.Random(seed)
    p_in, p_out = d2.input_price(MODEL), d2.output_price(MODEL)
    searching = {c for c in range(CONVERSATIONS_PER_DAY) if random.Random(c).random() < SEARCHING_SHARE}
    last_use: dict = {}
    previous: dict[int, object] = {}
    cost = hits = 0
    for minute, conv, route, turn in requests:
        tokens, key = prefix_of(route)
        if workers_with_own_order:
            key = (key, rng.randrange(workers_with_own_order))
        warm = key in last_use and minute - last_use[key] <= 5
        hits += warm
        last_use[key] = minute
        found = discovery if conv in searching else 0
        tail = FIRST_USER + turn * TURN_GROWTH + (found if turn >= 1 else 0)
        new = FIRST_USER if turn == 0 else TURN_GROWTH + (found if turn == 1 else 0)
        tail_warm = previous.get(conv) == key
        previous[conv] = key
        cost += ((tokens + SYSTEM_TOKENS) * (0.1 if warm else 1.25) + (tail - new) * (0.1 if tail_warm else 1.25)
                 + new * 1.25) * p_in + OUTPUT * p_out
    return {"cost": cost, "hit_rate": hits / len(requests)}


def step_day(counts: dict[str, int], client) -> dict[str, dict]:
    none = counts["no tools"]
    route_tokens = {r: d2.count_prompt(client, d2.domain_toolset(r), QUESTION) - none for r in ROUTES}
    requests = day_of_requests()
    block = {k: counts[k] - none for k in counts if k != "one discovery"}
    runs = {
        "all 120 loaded": simulate(requests, lambda r: (block["all 120 tools loaded"], "all")),
        "hand-picked routes (8)": simulate(requests, lambda r: (route_tokens[r], r)),
        "core + search, 111 deferred": simulate(requests, lambda r: (block["core + BM25 search, 111 deferred"], "deferred"),
                                                discovery=counts["one discovery"]),
        "core only (for reference)": simulate(requests, lambda r: (block["core only (9 tools)"], "core")),
    }
    print(f"{CONVERSATIONS_PER_DAY} conversations x {REQUESTS_PER_CONVERSATION} requests over {DAY_MINUTES // 60} hours "
          f"({len(requests):,} requests); system {SYSTEM_TOKENS:,}, first message {FIRST_USER}, +{TURN_GROWTH} per turn, "
          f"{OUTPUT} output tokens per call; {MODEL} at ${d2.get_spec(MODEL).input_price:.0f} / ${d2.get_spec(MODEL).output_price:.0f} "
          "per MTok; 5-minute cache.")
    rows = []
    for label, res in runs.items():
        tokens = {"all 120 loaded": block["all 120 tools loaded"], "hand-picked routes (8)": sum(route_tokens[r] * w for r, w in ROUTES.items()),
                  "core + search, 111 deferred": block["core + BM25 search, 111 deferred"],
                  "core only (for reference)": block["core only (9 tools)"]}[label]
        rows.append([label, round(tokens), f"{res['hit_rate']:.0%}", d2.money(res["cost"]), d2.money(res["cost"] / len(requests))])
    d2.table(rows, ["strategy", "tool tokens / request", "prefix cache hits", "per day", "per request"])
    print(f"\nRoute sizes: " + ", ".join(f"{r} {route_tokens[r]:,}" for r in ROUTES))
    print("One shared prefix is read by every conversation; eight route prefixes split the traffic eight ways, so each\n"
          "goes cold between its own requests and pays the 1.25x write again. The deferred strategy pays its discovery\n"
          f"({counts['one discovery']:,} tokens, in {SEARCHING_SHARE:.0%} of conversations) in the conversation tail, where it is cached too.")
    return runs


def step_first_attempt(counts: dict[str, int]) -> None:
    none = counts["no tools"]
    requests = day_of_requests()
    before = simulate(requests, lambda r: (counts["first course: 11 support tools"] - none, "eleven"))
    stable = simulate(requests, lambda r: (counts["all 120 tools loaded"] - none, "all"))
    shuffled = simulate(requests, lambda r: (counts["all 120 tools loaded"] - none, "all"), workers_with_own_order=WORKERS)
    d2.table([["11 tools, fixed order (before)", f"{before['hit_rate']:.0%}", d2.money(before["cost"]), "1.00x"],
              ["120 tools, fixed order", f"{stable['hit_rate']:.0%}", d2.money(stable["cost"]), f"{stable['cost'] / before['cost']:.2f}x"],
              [f"120 tools, order per worker ({WORKERS} workers, as deployed)", f"{shuffled['hit_rate']:.0%}", d2.money(shuffled["cost"]),
               f"{shuffled['cost'] / before['cost']:.2f}x"]],
             ["the copilot's day", "prefix hits", "per day", "vs before"])
    print(f"\nLoading all 120 tools costs +{stable['cost'] / before['cost'] - 1:.0%} while the cache holds: the big prefix is read at 0.1x.\n"
          "The first attempt built its tools array from a set of registered plugins, so each worker process had its own\n"
          "order; a request could only read what the same worker had written, and a conversation whose next turn landed on\n"
          f"another worker re-wrote its whole history at 1.25x. The bill doubled ({shuffled['cost'] / before['cost']:.2f}x). Build the tools array in a\n"
          "fixed order, and check cache_read_input_tokens after every deploy.")


def step_selection(client, quick: bool = False) -> None:
    system = d2.MARK_SELECT + "\nYou are Kestrel's operations copilot. Call the one tool that fits the request."
    rng = random.Random(7)
    others = [n for n in d2.all_names() if n not in d2.SELECTION_TOOLS]
    rng.shuffle(others)
    rows = []
    for extra in (0, 18, 48, 108):
        chosen = set(d2.SELECTION_TOOLS + others[:extra])
        tools = d2.loaded_toolset([n for n in d2.all_names() if n in chosen])
        hits = 0
        for text, expected in d2.SELECTION_TASKS:
            r = client.messages.create(model=MODEL, max_tokens=800, system=d2.cached_system(system), tools=tools,
                                       messages=[{"role": "user", "content": text}])
            hits += next((b.name for b in r.content if b.type == "tool_use"), None) == expected
        rows.append([len(tools), f"{hits}/{len(d2.SELECTION_TASKS)}", f"{hits / len(d2.SELECTION_TASKS):.0%}"])
    d2.table(rows, ["tools loaded", "first call correct", "accuracy"])
    if is_mock():
        print("[mock] the stand-in picks by the vocabulary of the descriptions, so every added tool is one more candidate\n"
              "       that can share a request's words. The direction matches what the tool search documentation reports\n"
              "       for Claude - selection degrades once a request carries more than a few dozen tools - but the size of\n"
              "       the drop is the mock's. Lab 07 builds the eval you would run live.")


def step_four(counts: dict[str, int], runs: dict[str, dict]) -> None:
    none = counts["no tools"]
    rows = [
        ["all tools loaded", counts["all 120 tools loaded"] - none, d2.money(runs["all 120 loaded"]["cost"]), "one entry, large",
         "120", "small catalogs; every tool used often"],
        ["hand-picked per route", counts["route: core + logistics (19)"] - none, d2.money(runs["hand-picked routes (8)"]["cost"]),
         "one entry per route", "~19", "few stable task types and a reliable router"],
        ["deferred + tool search", counts["core + BM25 search, 111 deferred"] - none, d2.money(runs["core + search, 111 deferred"]["cost"]),
         "one entry, small", "9 + found", "large or growing catalogs, mixed tasks"],
        ["code execution (PTC)", counts["core + code execution (9 + 1)"] - none, "lab 05", "one entry + container",
         "9 + code-callable", "fan-out, loops, big intermediate results"],
    ]
    d2.table(rows, ["strategy", "tool tokens", "day (sim.)", "cache", "visible tools", "use when"])
    print("\nThe four strategies compose. Kestrel's copilot keeps nine core tools loaded, defers the other 111 behind BM25\n"
          "search (labs 02-03), changes phase-specific tools with tool_addition (lab 04), and moves the recall fan-out into\n"
          "code (lab 05). Code execution does not shrink the definitions it calls; it shrinks what the calls return into\n"
          "the context.")


def main() -> None:
    client = get_client()
    header("Lab 01 - What a large toolset costs")
    if is_mock():
        print("(mock mode: count_tokens and usage are the mock's estimates; ratios and mechanics are what to learn)")

    step(1, "The catalog: 120 tools, 12 domains, 9 core, near-duplicates on purpose")
    step_catalog()

    step(2, "count_tokens for each request shape")
    counts = step_count(client)

    step(3, "A working day of traffic: what the prompt cache does with each shape")
    runs = step_day(counts, client)

    step(4, "The first attempt: 11 -> 120 tools, and the tools array that changed order")
    step_first_attempt(counts)

    step(5, "Selection: the same 30 tasks with more and more tools loaded")
    step_selection(client)

    step(6, "The four strategies compared")
    step_four(counts, runs)


if __name__ == "__main__":
    main()
