"""Solution to exercise 5 - choose the always-loaded core from traffic, and price the choice.

Objective
    Read which tools the support agent's 480 production traces called, then compare candidate always-loaded sets for
    the copilot on the numbers that decide it: the tools block every request carries (count_tokens), the share of
    conversations that would need a discovery, and the input cost of both per conversation.

Concepts
    usage share as the loading criterion, the break-even share (definition read cost vs discovery cost), the safety
    exit and the knowledge search as always loaded, near-duplicate pairs split across loaded and deferred, traces as
    a proxy for the traffic you will actually serve

Run
    python advanced/day2_tools_at_scale/solutions/ex05_core_set.py

What to observe
    * The usage table: a handful of tools carry most conversations; get_build_record (core) never appears in
      support traffic, create_rma (not core) appears in about a quarter of it.
    * The break-even share: on cost alone, any tool used in more than a few percent of conversations is cheaper loaded.
    * The candidate sets side by side: cost differences are small next to what a selection error costs.
"""
# test: expect=break-even share
# test: expect=discovery

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import get_client, header, is_mock, step  # noqa: E402

import _day2 as d2  # noqa: E402

TRACES = d2.ADV_DATA / "traces" / "traces.jsonl"
RENAMED = {"get_customer_profile": "get_customer"}   # the support agent's name for the catalog's get_customer
DISCOVERY = 702                                      # tokens one search turn adds to the tail (lab 01)
READ, WRITE = 0.1, 1.25
CONVERSATIONS = 400


def traces() -> list[dict]:
    """Observable fields only: the tools each conversation called, in order, and its number of model turns."""
    out = []
    for line in TRACES.read_text(encoding="utf-8").splitlines():
        t = json.loads(line)
        out.append({"tools": [RENAMED.get(n, n) for n in t["tools"]], "turns": t["turns"]})
    return out


def discovery_units(trace: dict, loaded: set[str]) -> tuple[int, float]:
    """(discoveries, input units) if every tool outside `loaded` costs one search when it is first needed.
    Tool i is called on request i+1; the search blocks are written on the next request and read on the rest."""
    requests = max(trace["turns"], len(trace["tools"]) + 1)
    seen: set[str] = set()
    count, units = 0, 0.0
    for i, name in enumerate(trace["tools"]):
        if name in loaded or name in seen:
            continue
        seen.add(name)
        count += 1
        position = i + 1
        units += DISCOVERY * (WRITE + READ * max(0, requests - position - 1))
    return count, units


def main() -> None:
    client = get_client()
    header("Exercise 5 (solution) - the always-loaded core, from traffic")
    data = traces()
    core = d2.core_names()

    step(1, f"What {len(data)} production conversations called")
    share = Counter(n for t in data for n in set(t["tools"]))
    rows = []
    for name, k in share.most_common():
        m = d2.meta(name)
        rows.append([name, f"{k / len(data):.0%}", m["domain"], m["risk"], "yes" if name in core else "-"])
    d2.table(rows, ["tool", "conversations", "domain", "risk", "catalog core"])
    missing = [n for n in core if n not in share]
    print(f"  core tools never called in this traffic: {', '.join(missing) or 'none'}")
    print("  The traces come from the support agent (orders, returns, warranty, billing); the copilot also serves quality\n"
          "  and fleet users, so treat them as a proxy and re-measure on the copilot's own traffic after launch.")

    step(2, "The break-even share: when is a tool cheaper loaded than discovered?")
    base = d2.count_prompt(client, d2.wide_toolset(d2.SEARCH_BM25, loaded=core), [{"role": "user", "content": "x"}])
    marginal = {}
    for name in ("create_rma", "track_shipment"):
        with_it = d2.count_prompt(client, d2.wide_toolset(d2.SEARCH_BM25, loaded=core + [name]),
                                  [{"role": "user", "content": "x"}])
        marginal[name] = with_it - base
    requests = sum(max(t["turns"], len(t["tools"]) + 1) for t in data) / len(data)
    per_discovery = sum(discovery_units(t, set())[1] for t in data) / sum(discovery_units(t, set())[0] for t in data)
    for name, tokens in marginal.items():
        load_cost = requests * tokens * READ
        print(f"  {name}: +{tokens} tokens on every request -> {requests:.2f} requests x {tokens} x {READ} = "
              f"{load_cost:.1f} units per conversation loaded")
        print(f"    deferred: {share[name] / len(data):.0%} of conversations x {per_discovery:.0f} units per discovery = "
              f"{share[name] / len(data) * per_discovery:.1f} units; break-even share {load_cost / per_discovery:.1%}")
    print(f"  ({requests:.2f} requests per conversation on average; one discovery costs {per_discovery:.0f} units on average\n"
          f"   in this traffic: {DISCOVERY} tokens written at {WRITE}x, then read at {READ}x on each later request)")

    step(3, "Candidate always-loaded sets, priced on the same traffic")
    by_share = [n for n, k in share.most_common() if k / len(data) >= 0.20]
    candidates = {
        "catalog core (9)": core,
        "core + create_rma + track_shipment": core + ["create_rma", "track_shipment"],
        "calls in >= 20% of traffic + escalate": list(dict.fromkeys(by_share + ["escalate_to_human"])),
        "knowledge search + escalate only": ["search_knowledge_base", "escalate_to_human"],
    }
    rows = []
    for label, loaded in candidates.items():
        tokens = d2.count_prompt(client, d2.wide_toolset(d2.SEARCH_BM25, loaded=loaded), [{"role": "user", "content": "x"}])
        found = [discovery_units(t, set(loaded)) for t in data]
        needing = sum(1 for c, _ in found if c) / len(data)
        per_conv = sum(c for c, _ in found) / len(data)
        prefix_units = sum(max(t["turns"], len(t["tools"]) + 1) for t in data) * tokens * READ / len(data)
        tail_units = sum(u for _, u in found) / len(data)
        day = CONVERSATIONS * (prefix_units + tail_units) * d2.input_price()
        rows.append([label, len(loaded), f"{tokens:,}", f"{needing:.0%}", f"{per_conv:.2f}",
                     f"{prefix_units + tail_units:,.0f}", d2.money(day)])
    d2.table(rows, ["always loaded", "tools", "prompt tokens", "need a discovery", "discoveries / conv",
                    "units / conv", "input $ / day"])
    print("  'units' are input tokens at list price (cache reads 0.1x, writes 1.25x, prefix warm); a day is 400 conversations.")
    if is_mock():
        print("  [mock] token counts are the mock's estimates; the ranking and the break-even logic transfer.")
    print("\n  Cost alone says: load anything used in more than a few percent of conversations. The tie-breakers are\n"
          "  selection accuracy (every loaded tool is a candidate for every request - lab 01 step 5) and near-duplicate\n"
          "  pairs: loading track_shipment while get_shipment stays deferred invites the model to use the visible one.")


if __name__ == "__main__":
    main()
