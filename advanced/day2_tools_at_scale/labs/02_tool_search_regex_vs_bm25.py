"""Lab 02 - Tool search: regex vs BM25 discovery over 111 deferred tools, scored on 32 labelled tasks.

Objective
    Put the whole catalog behind a tool search tool (core loaded, the rest deferred), send 32 natural requests through
    the API with the regex variant and again with the BM25 variant, read the `server_tool_use` /
    `tool_search_tool_result` blocks the API returns, and score precision and recall at k against the expected tool.
    Then look at the failure modes of each variant, the error block for an invalid pattern, and what a discovered
    tool costs once it is expanded into the conversation.

Concepts
    tool_search_tool_regex_20251119 vs tool_search_tool_bm25_20251119, defer_loading, server_tool_use (pattern/query,
    limit), tool_search_tool_result with tool_reference blocks, tool_search_tool_result_error (invalid_tool_input),
    no tool_result for srvtoolu_ ids, the searched surface (name + description + argument names + argument
    descriptions), hit@k / precision@k / MRR, deferred definitions expanded in place (usage shows it)

Run
    python advanced/day2_tools_at_scale/labs/02_tool_search_regex_vs_bm25.py [--quick]

What to observe
    * The block shapes: `server_tool_use{pattern}` vs `server_tool_use{query}`, and `tool_references` in the result.
    * Regex returns every match in catalog order (no ranking) and truncates at `limit`; BM25 ranks by relevance.
    * hit@1 / hit@5 / MRR per variant and which tasks each variant misses, with the reason.
    * The invalid-regex error comes back as a 200 with `tool_search_tool_result_error`, not a 400.
    * A discovered tool's definition shows up in `usage.input_tokens` of the turn that discovered it.
"""
# test: expect=hit@1
# test: args=--quick

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, print_json, step, text_of  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = (d2.MARK_SEARCH + "\nYou are Kestrel's operations copilot. Nine tools are loaded; about 110 more are in a searchable "
          "catalog covering orders, logistics, billing, returns, field service, inventory, products, quality, customers, fleet "
          "telemetry, knowledge and communications. When no loaded tool fits the request, search the catalog once with the most "
          "specific terms, then report which tool you would use. Do not call the discovered tool yet.")


def discover(client, variant: dict, text: str):
    tools = d2.wide_toolset(variant)
    r = client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM, tools=tools, messages=[{"role": "user", "content": text}])
    search = next((b for b in r.content if b.type == "server_tool_use"), None)
    result = next((b for b in r.content if b.type == "tool_search_tool_result"), None)
    refs = [ref.tool_name for ref in (getattr(result.content, "tool_references", None) or [])] if result is not None else []
    error = result.content if result is not None and result.content.type == "tool_search_tool_result_error" else None
    return r, search, refs, error


def score(rankings: list[tuple[str, list[str]]]) -> dict:
    n = len(rankings)
    out = {"hit@1": 0, "hit@3": 0, "hit@5": 0, "mrr": 0.0, "p@5": 0.0, "empty": 0}
    for expected, refs in rankings:
        if not refs:
            out["empty"] += 1
        if expected in refs:
            pos = refs.index(expected) + 1
            out["mrr"] += 1 / pos
            out["hit@1"] += pos <= 1
            out["hit@3"] += pos <= 3
            out["hit@5"] += pos <= 5
            out["p@5"] += 1 / 5
    return {"hit@1": out["hit@1"] / n, "hit@3": out["hit@3"] / n, "hit@5": out["hit@5"] / n, "MRR": out["mrr"] / n,
            "precision@5": out["p@5"] / n, "empty results": out["empty"]}


def step_shapes(client) -> None:
    text, expected = d2.DISCOVERY_TASKS[0]
    for variant in (d2.SEARCH_REGEX, d2.SEARCH_BM25):
        r, search, refs, _ = discover(client, variant, text)
        print(f"\n{variant['name']} - blocks: {d2.blocks_of(r)}")
        if search is not None:
            print_json({"type": search.type, "id": search.id, "name": search.name, "input": search.input})
        result = next((b for b in r.content if b.type == "tool_search_tool_result"), None)
        if result is not None:
            print_json({"type": result.type, "tool_use_id": result.tool_use_id, "content": result.content.model_dump()})
        print(f"expected {expected}; discovered {refs}; usage: {r.usage.input_tokens:,} uncached input tokens")
        print("reply: " + text_of(r)[:160])
    print("\nRules the shapes show: the search is a server tool (id srvtoolu_..., never answered with a tool_result); the regex "
          "variant takes `pattern`, BM25 takes `query`; results are tool_reference blocks the API expands for the model - you "
          "never send the definitions again differently, you keep sending the same `tools` array.")


def step_score(client, tasks: list[tuple[str, str]]) -> dict[str, list[tuple[str, list[str]]]]:
    rankings: dict[str, list[tuple[str, list[str]]]] = {"regex": [], "bm25": []}
    queries: dict[str, list[str]] = {"regex": [], "bm25": []}
    for text, expected in tasks:
        for key, variant in (("regex", d2.SEARCH_REGEX), ("bm25", d2.SEARCH_BM25)):
            _, search, refs, _ = discover(client, variant, text)
            rankings[key].append((expected, refs))
            queries[key].append(json.dumps(search.input) if search is not None else "-")
    rows = []
    for i, (text, expected) in enumerate(tasks):
        rr = rankings["regex"][i][1]
        rb = rankings["bm25"][i][1]
        rows.append([text[:46] + ("..." if len(text) > 46 else ""), expected,
                     str(rr.index(expected) + 1) if expected in rr else "-", str(rb.index(expected) + 1) if expected in rb else "-",
                     queries["regex"][i][:34], queries["bm25"][i][:44]])
    d2.table(rows, ["task", "expected tool", "regex rank", "bm25 rank", "regex pattern", "bm25 query"])
    print()
    d2.table([[key, f"{s['hit@1']:.0%}", f"{s['hit@3']:.0%}", f"{s['hit@5']:.0%}", f"{s['MRR']:.2f}", f"{s['precision@5']:.2f}", s["empty results"]]
              for key, s in ((k, score(v)) for k, v in rankings.items())],
             ["variant", "hit@1", "hit@3", "hit@5", "MRR", "precision@5", "empty"])
    print("\nhit@k = the expected tool is within the first k references (recall at k for a single relevant tool); precision@5 is "
          "hits/5 because at most one of the five is relevant - the rest are distractors the model still has to read and pay for.")
    return rankings


def step_failure_modes(rankings: dict[str, list[tuple[str, list[str]]]], tasks: list[tuple[str, str]]) -> None:
    for key in ("regex", "bm25"):
        misses = [(tasks[i][0], exp, refs) for i, (exp, refs) in enumerate(rankings[key]) if exp not in refs[:1]]
        print(f"\n{key}: {len(misses)} task(s) without the expected tool at rank 1")
        for text, expected, refs in misses[:4]:
            print(f"  - {text[:70]}")
            print(f"    expected {expected}; got {refs[:5] or 'nothing'}")
    print("\nWhy they differ: the regex variant matches substrings anywhere in name, description, argument names and argument\n"
          "descriptions and returns matches in catalog order - a broad pattern fills the 5 slots with the first matches, a\n"
          "narrow one misses paraphrases. BM25 tokenises and ranks by term rarity, so rare words in the request (`waive`, `decode`)\n"
          "dominate, common ones (`order`, `customer`) barely count, and morphology matters (`shipments` is not `shipment`).\n"
          "Both search only what is in the definitions: a description written in the users' words is the retrieval surface.")


def step_invalid_pattern(client) -> None:
    text = "Search the catalog for tools matching the pattern `(carrier|track` and tell me what you find."
    r, search, refs, error = discover(client, d2.SEARCH_REGEX, text)
    print(f"blocks: {d2.blocks_of(r)}")
    results = [b for b in r.content if b.type == "tool_search_tool_result"]
    if results:
        print_json({"type": results[0].type, "tool_use_id": results[0].tool_use_id, "content": results[0].content.model_dump()})
    if len(results) > 1:
        second = results[1].content
        print(f"retry: {[ref.tool_name for ref in (getattr(second, 'tool_references', None) or [])]}")
    print(f"HTTP status was 200 and stop_reason={r.stop_reason}: a bad pattern is a tool result error the model reads and fixes, "
          "not a request error. Error codes: invalid_tool_input, unavailable, too_many_requests, execution_time_exceeded.")
    print("reply: " + text_of(r)[:200])


def step_expansion_cost(client) -> None:
    text, expected = d2.DISCOVERY_TASKS[2]
    core_only = client.messages.count_tokens(model=MODEL, tools=d2.wide_toolset(d2.SEARCH_BM25), messages=[{"role": "user", "content": text}]).input_tokens
    r, search, refs, _ = discover(client, d2.SEARCH_BM25, text)
    history = [{"role": "user", "content": text}, {"role": "assistant", "content": r.content}]
    with_refs = client.messages.count_tokens(model=MODEL, tools=d2.wide_toolset(d2.SEARCH_BM25), messages=history + [{"role": "user", "content": "go on"}]).input_tokens
    defs = sum(len(json.dumps(d2.api_tool(d2.entry(n)))) for n in refs)
    print(f"prompt before the search: {core_only:,} tokens; after the search turn is in history: {with_refs:,} tokens "
          f"(+{with_refs - core_only:,}): the assistant turn plus {len(refs)} expanded definitions ({defs:,} chars of JSON).")
    print("The expansion sits INSIDE the messages, after the cached prefix - so discovery adds tokens to the tail of the prompt\n"
          "without invalidating the tools/system cache, and the references are re-expanded on every later request of the\n"
          "conversation (no need to search again for a tool already discovered).")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="score 12 tasks instead of 32")
    args = parser.parse_args()
    client = get_client()
    header("Lab 02 - Tool search: regex vs BM25")
    if is_mock():
        print("[mock] the stand-in writes the queries: the three rarest content words of the request as a regex alternation, all\n"
              "       content words as the BM25 query. Live Claude writes its own; rerun with a key to compare its discovery rate.")
    tasks = d2.DISCOVERY_TASKS[:12] if args.quick else d2.DISCOVERY_TASKS

    step(1, "The block shapes: one request through each variant")
    step_shapes(client)

    step(2, f"Score both variants on {len(tasks)} labelled tasks")
    rankings = step_score(client, tasks)

    step(3, "Failure modes of each variant")
    step_failure_modes(rankings, tasks)

    step(4, "An invalid regex pattern: a 200 with tool_search_tool_result_error")
    step_invalid_pattern(client)

    step(5, "What a discovered tool costs once expanded")
    step_expansion_cost(client)


if __name__ == "__main__":
    main()
