"""Lab 02 - Tool search: regex vs BM25 over 111 deferred tools, scored on 32 labelled tasks.

Objective
    Put the catalog behind a tool search tool (nine core tools loaded, the rest deferred), send 32 natural requests
    through the API with the regex variant and again with the BM25 variant, read the `server_tool_use` /
    `tool_search_tool_result` blocks, and score recall and precision at k against the expected tool. Then look at why
    each variant misses, what an invalid pattern returns, and what a discovered tool costs once it is expanded into
    the conversation.

Concepts
    tool_search_tool_regex_20251119 vs tool_search_tool_bm25_20251119, defer_loading, the always-loaded core set,
    server_tool_use (query, limit), tool_search_tool_result with tool_reference blocks, tool_search_tool_result_error
    (invalid_tool_input), no tool_result for srvtoolu_ ids, what is searched (name, description, argument names and
    argument descriptions), recall@k / precision@k / MRR, substring matches, descriptions as the retrieval surface

Run
    python advanced/day2_tools_at_scale/labs/02_tool_search_regex_vs_bm25.py [--quick]

What to observe
    * The block shapes: a server_tool_use with the query, then tool_reference blocks in the tool_search_tool_result.
    * The score table: BM25 finds the expected tool first far more often than a three-word regex.
    * Why regex misses: substrings match inside unrelated words, and matches come back unranked.
    * Why BM25 misses: common words in the request ('seal', 'lot') outweigh the one word that mattered ('bulletin').
    * An invalid regex is a 200 with a tool_search_tool_result_error the model can fix, not a 400.
"""
# test: expect=hit@1
# test: expect=invalid_tool_input

from __future__ import annotations

import argparse
import json
import re
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
    r = client.messages.create(model=MODEL, max_tokens=1500, system=d2.cached_system(SYSTEM), tools=d2.wide_toolset(variant),
                               messages=[{"role": "user", "content": text}])
    search = next((b for b in r.content if b.type == "server_tool_use"), None)
    result = next((b for b in r.content if b.type == "tool_search_tool_result"), None)
    refs = [ref.tool_name for ref in (getattr(result.content, "tool_references", None) or [])] if result is not None else []
    return r, search, refs


def score(rankings: list[tuple[str, list[str]]]) -> dict:
    """One relevant tool per task: recall@k = hit@k; precision@k = hits / k; MRR = mean of 1/rank."""
    n = len(rankings)
    ranks = [refs.index(expected) + 1 if expected in refs else None for expected, refs in rankings]
    hit = {k: sum(1 for r in ranks if r and r <= k) / n for k in (1, 3, 5)}
    returned = sum(len(refs) for _, refs in rankings) / n
    return {"hit@1": hit[1], "hit@3": hit[3], "hit@5": hit[5], "p@5": hit[5] / 5, "mrr": sum(1 / r for r in ranks if r) / n,
            "returned": returned}


def haystack(tool: dict) -> str:
    parts = [tool["name"], tool["description"]]
    for arg, schema in tool["input_schema"].get("properties", {}).items():
        parts += [arg, schema.get("description", "")]
    return " ".join(parts)


def why_regex_matched(pattern: str, name: str) -> str:
    """Show the word a regex alternative matched inside, e.g. 'fault' inside 'default'."""
    m = re.search(pattern, haystack(d2.entry(name)), re.I)
    if not m:
        return "-"
    text = haystack(d2.entry(name))
    start = max(0, text.rfind(" ", 0, m.start()) + 1)
    end = text.find(" ", m.end())
    word = text[start: end if end != -1 else len(text)].strip(".,;:()'")
    return f"'{m.group(0)}' matched in '{word}'"


def step_shapes(client) -> None:
    text, expected = d2.DISCOVERY_TASKS[0]
    print(f"request: {text!r}  (expected: {expected})")
    for variant in (d2.SEARCH_REGEX, d2.SEARCH_BM25):
        r, search, refs = discover(client, variant, text)
        print(f"\n{variant['type']} - response blocks: {d2.blocks_of(r)}")
        if search is not None:
            print(f"  server_tool_use  id={search.id}  name={search.name}  query={d2.search_input(search)!r}")
        result = next((b for b in r.content if b.type == "tool_search_tool_result"), None)
        if result is not None:
            print_json({"type": result.type, "tool_use_id": result.tool_use_id, "content": result.content.model_dump()})
        u = r.usage
        print(f"  usage: prompt {d2.prompt_size(u):,} tokens (cache write {u.cache_creation_input_tokens or 0:,}, read "
              f"{u.cache_read_input_tokens or 0:,}, uncached {u.input_tokens:,}); stop_reason={r.stop_reason}")
        print(f"  reply: {text_of(r)}")
    print("\nWhat the shapes say: the search is a server tool - its id starts with srvtoolu_, the API ran it and attached the\n"
          "result in the same response, and you never send a tool_result for it. The result names tools by reference; the\n"
          "API expands the definitions for the model. You keep sending the same `tools` array on every request.")
    if is_mock():
        print("[mock] the mock labels the regex variant's input `pattern`; the documented input is {\"query\": ..., \"limit\"?}\n"
              "       for both variants - read it defensively (d2.search_input) as this lab does.")


def step_score(client, tasks: list[tuple[str, str]]) -> dict[str, list[tuple[str, list[str]]]]:
    rankings: dict[str, list[tuple[str, list[str]]]] = {"regex": [], "bm25": []}
    queries: dict[str, list[str]] = {"regex": [], "bm25": []}
    for text, expected in tasks:
        for key, variant in (("regex", d2.SEARCH_REGEX), ("bm25", d2.SEARCH_BM25)):
            _, search, refs = discover(client, variant, text)
            rankings[key].append((expected, refs))
            queries[key].append(d2.search_input(search) if search is not None else "-")
    rows = []
    for i, (text, expected) in enumerate(tasks):
        rr, rb = rankings["regex"][i][1], rankings["bm25"][i][1]
        rows.append([d2.clip(text, 44), expected, str(rr.index(expected) + 1) if expected in rr else "-",
                     str(rb.index(expected) + 1) if expected in rb else "-", d2.clip(queries["regex"][i], 30),
                     d2.clip(queries["bm25"][i], 40)])
    d2.table(rows, ["task", "expected tool", "regex rank", "bm25 rank", "regex query", "bm25 query"])
    print()
    d2.table([[key, f"{s['hit@1']:.0%}", f"{s['hit@3']:.0%}", f"{s['hit@5']:.0%}", f"{s['p@5']:.2f}", f"{s['mrr']:.2f}",
               f"{s['returned']:.1f}"] for key, s in ((k, score(v)) for k, v in rankings.items())],
             ["variant", "hit@1", "hit@3", "hit@5 (recall@5)", "precision@5", "MRR", "refs returned"])
    print("\nOne relevant tool per task, so recall@k is 'the expected tool is among the first k references' and precision@5\n"
          "is at most 0.20: the other references are distractors whose definitions the model still receives and pays for.")
    return rankings


def step_failures(rankings: dict[str, list[tuple[str, list[str]]]], tasks: list[tuple[str, str]], client) -> None:
    for key in ("regex", "bm25"):
        misses = [(tasks[i][0], exp, refs) for i, (exp, refs) in enumerate(rankings[key]) if exp not in refs[:1]]
        print(f"\n{key}: {len(misses)} of {len(tasks)} tasks without the expected tool first")
        for text, expected, refs in misses[:4]:
            print(f"  - {d2.clip(text, 76)}")
            print(f"    expected {expected}; got {refs[:5] or 'nothing'}")
            if key == "regex" and refs:
                _, search, _ = discover(client, d2.SEARCH_REGEX, text)
                print(f"    why {refs[0]} matched: {why_regex_matched(d2.search_input(search), refs[0])}")
    print("\nA regex is a filter: every tool whose name, description or argument text contains one of the alternatives\n"
          "matches, inside other words too, and a match is a match - nothing ranks the fifth one below the first. Keep\n"
          "patterns narrow (word boundaries, the tool's own vocabulary) and let BM25 do ranking when requests are prose.\n"
          "BM25 weighs rare words up and common ones down, so a request dominated by domain words ('seal', 'lot') can bury\n"
          "the one word that named the tool ('bulletin'). Both read only the definitions: a description written in the\n"
          "words users use is the retrieval surface (lab 07 measures it; exercise 10 fixes the BM25 miss and writes better\n"
          "patterns for the regex ones).")


def step_invalid(client) -> None:
    text = "Search the catalog for tools matching the pattern `(carrier|track` and tell me what you find."
    r, _, _ = discover(client, d2.SEARCH_REGEX, text)
    print(f"response blocks: {d2.blocks_of(r)}")
    results = [b for b in r.content if b.type == "tool_search_tool_result"]
    print_json({"type": results[0].type, "tool_use_id": results[0].tool_use_id, "content": results[0].content.model_dump()})
    if len(results) > 1:
        print(f"the retry inside the same response found: {[ref.tool_name for ref in results[1].content.tool_references]}")
    print(f"HTTP 200 and stop_reason={r.stop_reason}: an invalid pattern is a result the model reads and fixes, not a request\n"
          "error. The error codes are invalid_tool_input, unavailable, too_many_requests and execution_time_exceeded.")


def step_expansion(client) -> None:
    text, _ = d2.DISCOVERY_TASKS[2]
    tools = d2.wide_toolset(d2.SEARCH_BM25)
    before = d2.count_prompt(client, tools, [{"role": "user", "content": text}])
    r, _, refs = discover(client, d2.SEARCH_BM25, text)
    history = [{"role": "user", "content": text}, {"role": "assistant", "content": r.content}, {"role": "user", "content": "go on"}]
    after = d2.count_prompt(client, tools, history)
    chars = sum(len(json.dumps(d2.api_tool(d2.entry(n)))) for n in refs)
    print(f"prompt before the search: {before:,} tokens; with the search turn in the history: {after:,} (+{after - before:,}) -\n"
          f"the assistant turn plus the {len(refs)} expanded definitions ({chars:,} characters of JSON).")
    print("The expansion sits in the messages, after the cached tools+system prefix: discovery grows the tail of the prompt\n"
          "and invalidates nothing. The references are expanded again on every later request of the conversation, so a\n"
          "tool discovered once stays callable without another search.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="score 12 tasks instead of 32 (live mode: fewer paid calls)")
    args = parser.parse_args()
    client = get_client()
    header("Lab 02 - Tool search: regex vs BM25")
    if is_mock():
        print("[mock] the stand-in writes the queries: the rarest content words of the request as a regex alternation, all of\n"
              "       them as the BM25 query. The search itself runs as the API documents it (regex over name, description\n"
              "       and argument text; BM25 ranking). Live Claude writes its own queries; rerun with a key to compare.")
    tasks = d2.DISCOVERY_TASKS[:12] if args.quick else d2.DISCOVERY_TASKS
    core = set(d2.core_names())
    print(f"{len(tasks)} tasks; expected tools inside the core set: {sum(1 for _, e in tasks if e in core)} "
          "(every task needs a deferred tool, so every task must be found by search).")

    step(1, "The block shapes: one request through each variant")
    step_shapes(client)

    step(2, f"Score both variants on {len(tasks)} labelled tasks")
    rankings = step_score(client, tasks)

    step(3, "Why each variant misses")
    step_failures(rankings, tasks, client)

    step(4, "An invalid regex: a 200 with tool_search_tool_result_error")
    step_invalid(client)

    step(5, "What a discovered tool costs once expanded")
    step_expansion(client)


if __name__ == "__main__":
    main()
