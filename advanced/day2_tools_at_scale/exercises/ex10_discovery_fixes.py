"""Exercise 10 (starter) - fix the discovery misses of lab 02.

Two jobs, both measured on all 32 discovery tasks through the API:

  1. BM25: 'Get bulletin TSB-2026-09 about the seal lot recall.' does not find get_bulletin - the words 'seal', 'lot'
     and 'recall' pull other tools up. Rewrite get_bulletin's description (DESCRIPTION_FIXES) so it ranks first,
     without pushing any other task's expected tool off first place.
  2. Regex: for the regex misses, write patterns (PATTERN_FIXES) that put the expected tool first. The stand-in uses a
     pattern verbatim when the request quotes it in backticks, so you can test your own patterns through the API.

Run
    python advanced/day2_tools_at_scale/exercises/ex10_discovery_fixes.py

The starter measures the baseline and prints TODO markers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header, step  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = (d2.MARK_SEARCH + "\nYou are Kestrel's operations copilot. Search the tool catalog once with the most specific terms "
          "and report which tool you would use.")

# TODO: {tool name: new description}
DESCRIPTION_FIXES: dict[str, str] = {}
# TODO: {task index in d2.DISCOVERY_TASKS: regex pattern}
PATTERN_FIXES: dict[int, str] = {}


def toolset(search: dict, fixes: dict[str, str]) -> list[dict]:
    tools = d2.wide_toolset(search)
    for t in tools:
        if t.get("name") in fixes:
            t["description"] = fixes[t["name"]]
    return tools


def first_refs(client, tools: list[dict], text: str) -> list[str]:
    r = client.messages.create(model=MODEL, max_tokens=1500, system=d2.cached_system(SYSTEM), tools=tools,
                               messages=[{"role": "user", "content": text}])
    return [ref.tool_name for b in r.content if b.type == "tool_search_tool_result"
            for ref in (getattr(b.content, "tool_references", None) or [])]


def hit1(client, tools: list[dict], tasks: list[tuple[str, str]]) -> list[bool]:
    return [(refs[:1] == [expected]) for refs, (_, expected) in
            ((first_refs(client, tools, text), (text, expected)) for text, expected in tasks)]


def main() -> None:
    client = get_client()
    header("Exercise 10 - fixing discovery misses")
    step(1, "Baseline")
    bm25 = hit1(client, toolset(d2.SEARCH_BM25, {}), d2.DISCOVERY_TASKS)
    regex = hit1(client, toolset(d2.SEARCH_REGEX, {}), d2.DISCOVERY_TASKS)
    print(f"  BM25 hit@1  {sum(bm25)}/{len(bm25)}; misses: {[i for i, ok in enumerate(bm25) if not ok]}")
    print(f"  regex hit@1 {sum(regex)}/{len(regex)}; misses: {[i for i, ok in enumerate(regex) if not ok]}")
    step(2, "Your fixes")
    print(f"TODO: DESCRIPTION_FIXES has {len(DESCRIPTION_FIXES)} entries - rewrite get_bulletin's description, re-measure BM25.")
    print(f"TODO: PATTERN_FIXES has {len(PATTERN_FIXES)} entries - send 'Search the catalog for `<pattern>`' per miss and check "
          "the expected tool comes first.")


if __name__ == "__main__":
    main()
