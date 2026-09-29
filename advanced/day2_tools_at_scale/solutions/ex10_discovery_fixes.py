"""Solution to exercise 10 - fix lab 02's discovery misses, measured through the API.

Objective
    Rewrite get_bulletin's description so BM25 finds it for the request that missed, without costing any other task
    its first place; and write a narrow regex for every task the regex variant missed, sending each pattern through
    the API (quoted in backticks, so the stand-in uses it verbatim) and checking the expected tool comes back first.

Concepts
    descriptions as the retrieval surface, BM25 term weighting, regex as a filter (word boundaries, the catalog's own
    vocabulary, catalog order), measuring a fix on the whole set, not only on the task it targets

Run
    python advanced/day2_tools_at_scale/solutions/ex10_discovery_fixes.py

What to observe
    * BM25 hit@1 goes to 32/32 with one description changed; no other task loses its first place.
    * With patterns written in the catalog's vocabulary, the regex variant finds every missed tool first.
"""
# test: expect=BM25 hit@1
# test: expect=regex

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))

from labkit import get_client, header, step  # noqa: E402

import ex10_discovery_fixes as ex  # noqa: E402

d2 = ex.d2

DESCRIPTION_FIXES = {
    "get_bulletin": "A technical or safety bulletin by ID (e.g. TSB-2026-09): the recall or safety notice sent to customers, "
                    "with the affected lots and SKUs, the hazard, the remedy and the interim measures. Use for 'get bulletin "
                    "TSB-...', 'what does the recall bulletin say'.",
}
PATTERN_FIXES = {
    0: r"\bscan history\b", 2: r"\bmeaning\b", 3: r"\bvibration\b.*\btrend\b", 5: r"\bevery serial number\b",
    6: r"\breserve\b", 7: r"\bon-hand\b", 8: r"\blead time\b", 9: r"\bquote carriers\b", 12: r"\bwaive late fees\b",
    14: r"\bopen a return authorisation\b", 15: r"\brestocking fee\b.*\bwould\b", 16: r"\binspection findings\b",
    18: r"\bpast service visits\b", 19: r"\bdatasheet\b", 20: r"\bbill of materials\b", 21: r"\bblock shipment\b",
    25: r"\broll-up\b", 26: r"\bbulletin by id\b", 27: r"\brunbook\b", 28: r"\bteams channel\b",
    29: r"\bcontacts at a customer\b", 31: r"\bavailable credit\b",
}


def main() -> None:
    client = get_client()
    header("Exercise 10 (solution) - fixing discovery misses")

    step(1, "BM25: one description rewritten")
    before = ex.hit1(client, ex.toolset(d2.SEARCH_BM25, {}), d2.DISCOVERY_TASKS)
    after = ex.hit1(client, ex.toolset(d2.SEARCH_BM25, DESCRIPTION_FIXES), d2.DISCOVERY_TASKS)
    print(f"  BM25 hit@1 before {sum(before)}/{len(before)}, after {sum(after)}/{len(after)}; "
          f"tasks that lost first place: {[i for i in range(len(before)) if before[i] and not after[i]] or 'none'}")
    print(f"  get_bulletin now reads: {DESCRIPTION_FIXES['get_bulletin']}")

    step(2, "Regex: patterns in the catalog's own vocabulary, sent through the API")
    regex_before = ex.hit1(client, ex.toolset(d2.SEARCH_REGEX, {}), d2.DISCOVERY_TASKS)
    fixed = 0
    for i, pattern in PATTERN_FIXES.items():
        _, expected = d2.DISCOVERY_TASKS[i]
        refs = ex.first_refs(client, ex.toolset(d2.SEARCH_REGEX, {}), f"Search the catalog for `{pattern}`.")
        ok = refs[:1] == [expected]
        fixed += ok
        print(f"  {i:>2} {expected:<26} {pattern:<36} -> {refs[:2]} {'ok' if ok else 'MISS'}")
    print(f"  regex hit@1: {sum(regex_before)}/{len(regex_before)} with the stand-in's own queries; the {len(PATTERN_FIXES)} "
          f"rewritten patterns put the expected tool first {fixed} times")
    print("  Most of these patterns quote the catalog's wording ('every serial number', 'contacts at a customer') - a\n"
          "  regex rewards knowing how the tool author wrote the description. That is why BM25 is the default for\n"
          "  prose requests, and why the catalog's descriptions should use the words users use.")


if __name__ == "__main__":
    main()
