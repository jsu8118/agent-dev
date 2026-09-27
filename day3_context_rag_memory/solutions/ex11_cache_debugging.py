"""Solution to exercise 11 - find and fix the silent cache invalidators in a prompt builder.

The buggy builder (same as exercises/ex11_cache_debugging.py) never gets a cache read, and nothing errors. The
method that finds it every time:
1. confirm from `usage` (cache_read_input_tokens stays 0 on identical-looking requests);
2. render two consecutive requests in API order (tools -> system -> messages), strip cache_control, and find the
   first byte where they diverge - that is the invalidation point; fix it and repeat until the prefixes match;
3. make the fix permanent with a regression check: the second identical request must show cache reads.

Run: python day3_context_rag_memory/solutions/ex11_cache_debugging.py
"""
# test: expect=invalidator

from __future__ import annotations

import datetime as dt
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header, step  # noqa: E402

import _day3 as d3  # noqa: E402
import _tools as tools  # noqa: E402

HEAD = ("<day3_manual_library>\nYou are Kestrel's field-service assistant. Answer only from the manuals below and cite "
        "the manual code and section, e.g. [IOM-KP250 §6].\n</day3_manual_library>\n")
LIBRARY = d3.library_text(d3.load_docs(("manual",)))
SITE = {"site": "Harbor Foods Plant 2", "customer_id": "C-1005", "units": "metric", "language": "en", "shift": "day"}
QUESTION = "How often do I regrease the KP-250 bearings?"


def build_buggy(question: str) -> dict:
    """What the exercise shipped with: three invalidators, none of them obvious in code review."""
    settings = dict(random.sample(sorted(SITE.items()), len(SITE)))       # config service: key order not guaranteed
    tool_list = random.sample([tools.SEARCH_MANUALS, tools.READ_SECTION, tools.QUERY_TELEMETRY], 3)  # plugin order
    system = (f"{HEAD}Generated at {dt.datetime.now().isoformat()}\n"      # a timestamp at the top of the prefix
              f"Site settings: {json.dumps(settings)}\n\n{LIBRARY}")
    return dict(model=MODEL, max_tokens=2000, tools=tool_list,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": question}])


def build_fixed(question: str) -> dict:
    """Stable things first and byte-identical; volatile things after the last breakpoint."""
    tool_list = sorted([tools.SEARCH_MANUALS, tools.READ_SECTION, tools.QUERY_TELEMETRY], key=lambda t: t["name"])
    system = f"{HEAD}\n{LIBRARY}"
    volatile = (f"Site settings: {json.dumps(SITE, sort_keys=True)}\n"
                f"Local time: {dt.datetime.now().isoformat(timespec='minutes')}\n\n")
    return dict(model=MODEL, max_tokens=2000, tools=tool_list,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": volatile + question}])


def rendered(params: dict) -> list[tuple[str, str]]:
    """The prompt in the order the API renders it, without cache_control markers (they are not part of the key)."""
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items() if k != "cache_control"}
        if isinstance(obj, list):
            return [clean(x) for x in obj]
        return obj
    return [(part, json.dumps(clean(params.get(part)), ensure_ascii=False)) for part in ("tools", "system", "messages")]


def first_divergence(a: dict, b: dict) -> str:
    for (part, x), (_, y) in zip(rendered(a), rendered(b)):
        if x != y:
            i = next((i for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))
            return f"{part} @ char {i}: {x[max(0, i - 30):i + 25]!r}\n{' ' * (len(part) + 14)}vs {y[max(0, i - 30):i + 25]!r}"
    return "no divergence - the prefixes are byte-identical"


def send(client, builder, label: str, n: int = 3) -> list:
    rows, bodies = [], []
    for i in range(n):
        params = builder(QUESTION)
        bodies.append(params)
        r = client.messages.create(**params)
        rows.append([f"{label} #{i + 1}", r.usage.input_tokens, r.usage.cache_creation_input_tokens or 0,
                     r.usage.cache_read_input_tokens or 0, d3.money(d3.response_cost(r))])
    d3.table(rows, ["request", "uncached", "cache write", "cache read", "cost"])
    return bodies


def main() -> None:
    client = get_client()
    header("Solution 11 - hunting a silent invalidator")
    random.seed(11)

    step(1, "Symptom: identical questions, zero cache reads")
    bodies = send(client, build_buggy, "buggy")

    step(2, "Diagnosis: diff adjacent requests in render order (tools -> system -> messages)")
    print("first divergence:", first_divergence(bodies[0], bodies[1]))
    found = []
    a, b = bodies[0], bodies[1]
    if rendered(a)[0][1] != rendered(b)[0][1]:
        found.append("tool list order changes between requests (tools render first: everything after misses)")
    sys_a, sys_b = a["system"][0]["text"], b["system"][0]["text"]
    if sys_a.split("Site settings")[0] != sys_b.split("Site settings")[0]:
        found.append("a timestamp in the system prompt ('Generated at ...')")
    if "Site settings" in sys_a and sys_a.split("Site settings")[1][:200] != sys_b.split("Site settings")[1][:200]:
        found.append("json.dumps(settings) without sort_keys: same data, different bytes")
    print("invalidators found:\n  - " + "\n  - ".join(found))

    step(3, "Fix: sorted tools, frozen system prompt, volatile values after the breakpoint")
    fixed = send(client, build_fixed, "fixed")
    print("first divergence:", first_divergence(fixed[0], fixed[1]))
    print("(the prefixes now match through tools and system; the messages differ only after the cached prefix)")

    step(4, "Regression check to keep it fixed")
    p1, p2 = build_fixed(QUESTION), build_fixed(QUESTION)
    client.messages.create(**p1)
    second = client.messages.create(**p2)
    ok = (second.usage.cache_read_input_tokens or 0) > 0
    print(f"second identical request read {second.usage.cache_read_input_tokens or 0:,} tokens from cache -> "
          f"{'PASS' if ok else 'FAIL'}")
    print("Put this assertion in CI (against the real API, or the labkit mock) - caching regressions are silent.")


if __name__ == "__main__":
    main()
