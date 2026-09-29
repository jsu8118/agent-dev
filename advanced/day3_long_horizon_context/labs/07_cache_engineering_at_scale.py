"""Lab 07 - Cache engineering at scale: fan-out, pre-warming, the 20-position lookback, multi-tenant prefixes.

Objective
    Kestrel's field agent serves a fleet: eight technicians start at 07:00, six customers share one deployment, and
    some turns run long tool loops. Measure four cache effects the first course only described: (1) eight concurrent
    requests over a cold prefix all write it - unless one max_tokens=0 request pre-warms it; (2) a breakpoint looks back
    at most 20 positions for an earlier entry, where a run of tool_use or tool_result blocks is one position; (3) the
    order of shared and per-tenant content decides whether tenants share an entry; (4) below a model's minimum nothing
    is cached. End with a savings table and a fleet projection.

Concepts
    an entry is readable only once its writer has started responding (the mock's cache.ready_delay stands in for
    that); pre-warming with max_tokens=0 (content [], stop_reason max_tokens, output 0, a normal write charge); the
    20-position lookback and how positions are counted; explicit breakpoints at stability boundaries vs automatic
    caching; multi-tenant prefix layout (shared first, tenant next, conversation last); TTL ordering; minimum
    cacheable prefix per model (get_spec(model).cache_min_tokens).

Run
    python advanced/day3_long_horizon_context/labs/07_cache_engineering_at_scale.py

What to observe
    * Cold fan-out: 8 writes, 0 reads. Pre-warmed: 1 write (the warm-up, zero output tokens), 8 reads.
    * The lookback sweep: after a one-block reply, 19 appended blocks still reach the previous entry and 20 do not;
      an intermediate breakpoint rescues 25.
    * With one breakpoint on the newest technician message, eight look-ups one at a time push the previous entry out
      of reach and the next turn re-writes the whole export; the same eight in parallel do not; automatic caching
      (an entry at the end of every request) makes the order irrelevant.
    * Tenant-first layout: every customer writes the whole prefix. Shared-first: the card is written once.
    * A ~800-token prompt caches on Claude Opus 5 and silently does not on Sonnet 5 or Haiku 4.5.
"""
# test: expect=pre-warm
# test: expect=lookback
# test: expect=Savings

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import FAST_MODEL, MID_MODEL, MODEL, get_client, get_spec, header, is_mock, mock_api, step  # noqa: E402

import _day3 as d3  # noqa: E402

MARKER = "<adv_day3_cache_lab>"
CARD_SECTIONS = [("IOM-KP250", "5"), ("IOM-KP250", "6"), ("IOM-KP250", "7.4"), ("IOM-KP250", "7.5"), ("IOM-KP250", "9"),
                 ("IOM-KP400", "4"), ("IOM-KP400", "5"), ("IOM-KP400", "6"), ("UM-KC1", "3"), ("UM-KC1", "4"),
                 ("UM-KC1", "5"), ("SEAL-FA-02", "2"), ("CM-GUIDE-01", "3")]
CARD = "\n\n".join(d3.read_manual_section(doc, sec) for doc, sec in CARD_SECTIONS)
MORNING_QUESTIONS = ["What torque do the KP-250 baseplate foundation bolts need?",
                     "KP-400 vibration limits on a flexible foundation?",
                     "How much grease per KP-250 bearing, and how often?",
                     "What does F05 mean on a KC-1, and the remedy?",
                     "Seal faces with radial heat cracks - what caused it?",
                     "KC-1 default PID setpoint?",
                     "When do I replace KP-250 bearings?",
                     "KP-400 regrease interval?"]
READY_DELAY = 0.5                   # mock only: seconds before a written entry can be read by other requests


def system(day: str, *, ttl: str | None = None) -> list[dict]:
    """The fleet-wide prefix: instructions + the reference card, one explicit breakpoint at its end."""
    control = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
    return [{"type": "text", "text": f"{MARKER}\nKestrel field reference assistant ({day}). Answer from the reference "
                                     f"card below; cite the section.\n\n{CARD}", "cache_control": control}]


def ask(client, sys_blocks: list[dict], question: str, **kw):
    return client.messages.create(model=MODEL, max_tokens=kw.pop("max_tokens", 2000), system=sys_blocks,
                                  tools=d3.FIELD_TOOLS, messages=[{"role": "user", "content": question}], **kw)


def totals(responses: list) -> tuple[int, int, float]:
    return (sum(r.usage.cache_creation_input_tokens or 0 for r in responses),
            sum(r.usage.cache_read_input_tokens or 0 for r in responses),
            sum(d3.response_cost(r) for r in responses))


# ------------------------------------------------------------------------------------------------ step 1: fan-out
def fan_out(client, sys_blocks: list[dict]) -> list:
    with cf.ThreadPoolExecutor(max_workers=len(MORNING_QUESTIONS)) as pool:
        return list(pool.map(lambda q: ask(client, sys_blocks, q), MORNING_QUESTIONS))


def step_fanout(client) -> dict:
    if is_mock():
        mock_api().cache.ready_delay = READY_DELAY
        print(f"[mock] cache.ready_delay = {READY_DELAY} s: an entry becomes readable {READY_DELAY} s after its writer "
              "started - the stand-in for 'once the first response begins streaming'.")
    cold = fan_out(client, system("Tuesday 2026-09-15"))                  # each morning starts cold: TTL expired
    warm_sys = system("Wednesday 2026-09-16")
    warm = ask(client, warm_sys, "warmup", max_tokens=0)
    print(f"Pre-warm request: max_tokens=0 -> content {warm.content}, stop_reason {warm.stop_reason}, output_tokens "
          f"{warm.usage.output_tokens}, cache_creation_input_tokens {warm.usage.cache_creation_input_tokens:,}.")
    if is_mock():
        time.sleep(READY_DELAY)             # live: the max_tokens=0 call returns after prefill - the entry is ready
    warmed = fan_out(client, warm_sys)
    w1, r1, c1 = totals(cold)
    w2, r2, c2 = totals([warm] + warmed)
    d3.table([["cold fan-out", len(cold), sum(1 for r in cold if r.usage.cache_creation_input_tokens), w1, r1,
               d3.money(c1)],
              ["pre-warm + fan-out", len(warmed) + 1, sum(1 for r in [warm] + warmed if r.usage.cache_creation_input_tokens),
               w2, r2, d3.money(c2)]],
             ["07:00, eight technicians", "requests", "requests that wrote", "cache writes", "cache reads", "cost"])
    print("Concurrent requests cannot read what the others are still writing: each pays the 1.25x write. One "
          "max_tokens=0 request (or one real request, then the other seven once its first token arrives) turns seven "
          "writes into reads.")
    if is_mock():
        mock_api().cache.ready_delay = 0.0
    return {"saving": c1 - c2}


# ------------------------------------------------------------------------------------------------ step 2: lookback
def step_lookback(client) -> dict:
    sys_blocks = system("lookback")
    log = {"type": "document", "title": "harbor", "source": {"type": "text", "media_type": "text/plain",
                                                             "data": d3.site_log("harbor")}}
    first = [{"role": "user", "content": [log, {"type": "text", "text": "Harbor's historian export for today."}]}]
    r1 = client.messages.create(model=MODEL, max_tokens=2000, system=sys_blocks, messages=first,
                                cache_control={"type": "ephemeral"})
    base = first + [{"role": "assistant", "content": r1.content}]
    assistant_blocks = len(r1.content)
    edge = 20 - assistant_blocks                   # the most blocks one request can append and still reach the entry
    rows = []
    for k in (10, edge, edge + 1, 25):
        items = [{"type": "text", "text": f"Checklist run {k}, item {i + 1}: torque, grease, alignment OK."}
                 for i in range(k)]
        r = client.messages.create(model=MODEL, max_tokens=2000, system=sys_blocks,
                                   messages=base + [{"role": "user", "content": items}],
                                   cache_control={"type": "ephemeral"})
        rows.append([k, k + assistant_blocks, r.usage.cache_read_input_tokens, r.usage.cache_creation_input_tokens,
                     "hit" if r.usage.cache_read_input_tokens > 5_000 else "MISS - back to the system entry"])
    items = [{"type": "text", "text": f"Checklist run 25b, item {i + 1}: torque, grease, alignment OK."}
             for i in range(25)]
    items[11]["cache_control"] = {"type": "ephemeral"}          # an intermediate breakpoint 14 positions in
    r = client.messages.create(model=MODEL, max_tokens=2000, system=sys_blocks,
                               messages=base + [{"role": "user", "content": items}], cache_control={"type": "ephemeral"})
    rows.append(["25 + a breakpoint at item 12", 25 + assistant_blocks, r.usage.cache_read_input_tokens,
                 r.usage.cache_creation_input_tokens, "hit" if r.usage.cache_read_input_tokens > 5_000 else "MISS"])
    d3.table(rows, ["blocks appended", "positions since the last entry", "cache read", "cache write", "result"])
    print(f"The previous request's entry sits at its last block. The next request's breakpoint walks back at most 20 "
          f"positions: the assistant reply ({assistant_blocks} block{'s' if assistant_blocks > 1 else ''}) plus {edge} "
          f"new blocks reach it; {edge + 1} do not, and the read falls back to the system prompt's entry - "
          "re-writing the whole historian export.")

    # the same rule inside an agent loop: a harness that keeps ONE breakpoint on the newest technician message
    seq = ("Check these sections: IOM-KP250 §3, IOM-KP250 §5, IOM-KP250 §6, IOM-KP250 §9, IOM-KP400 §4, IOM-KP400 §5, "
           "UM-KC1 §3, UM-KC1 §4 - one at a time.")
    rows, costs = [], {}
    for label, text, auto in [("breakpoint on the newest technician message; one at a time", seq, False),
                              ("breakpoint on the newest technician message; in parallel",
                               seq.replace("one at a time", "in parallel"), False),
                              ("automatic caching; one at a time", seq, True)]:
        params = dict(model=MODEL, max_tokens=4000, system=sys_blocks, tools=d3.FIELD_TOOLS,
                      **({"cache_control": {"type": "ephemeral"}} if auto else {}))
        first_text = {"type": "text", "text": f"({label}) {text}"}
        if not auto:
            first_text["cache_control"] = {"type": "ephemeral"}
        msgs = [{"role": "user", "content": [log, first_text]}]
        execute, rounds = d3.make_executor(), 0
        for _ in range(10):                                        # turn 1: the look-up loop
            r = client.messages.create(messages=msgs, **params)
            msgs.append({"role": "assistant", "content": r.content})
            uses = [b for b in r.content if b.type == "tool_use"]
            if r.stop_reason != "tool_use":
                break
            rounds += 1
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id,
                                                      "content": execute(b.name, dict(b.input))[0]} for b in uses]})
        nxt = {"type": "text", "text": "Which of those sections gives the baseplate bolt torque?"}
        if not auto:
            first_text.pop("cache_control")        # the harness moves its message breakpoint to the newest message
            nxt["cache_control"] = {"type": "ephemeral"}
        r2 = client.messages.create(messages=msgs + [{"role": "user", "content": [nxt]}], **params)
        costs[label] = d3.response_cost(r2)
        rows.append([label, rounds, r2.usage.cache_read_input_tokens, r2.usage.cache_creation_input_tokens,
                     d3.money(costs[label])])
    d3.table(rows, ["harness", "tool rounds in turn 1", "turn 2 cache read", "turn 2 cache write", "turn 2 cost"])
    print("Each sequential round adds four positions (thinking, text, tool_use; tool_result), so eight rounds put the "
          "previous breakpoint over 30 positions back and turn 2 re-writes the export. Parallel calls are one tool_use "
          "run and one tool_result run - one round. Automatic caching writes an entry at the end of every request, so "
          "no gap ever opens.")
    labels = list(costs)
    miss_cost = costs[labels[0]] - costs[labels[2]]
    return {"saving": miss_cost}


# ------------------------------------------------------------------------------------------------ step 3: tenants
CONTRACT = ("Service contract terms for this customer: P1 response 1 hour, P2 4 hours, P3 next business day; parts "
            "billed at list minus the contract discount; work outside the stop windows needs the site supervisor's "
            "written approval; every visit ends with a findings report in the customer portal. ")


def tenant_block(site: str) -> str:
    return (f"<tenant site=\"{site}\">\n{json.dumps(d3.site_brief(site), ensure_ascii=False)}\n{CONTRACT * 3}"
            f"\n</tenant>")


def step_tenants(client) -> dict:
    shared = system("tenants")[0]["text"]
    rows, costs = [], {}
    for layout in ("tenant first", "shared first"):
        responses = []
        for rnd in range(3):                                      # three questions per customer, round-robin
            for site in d3.SITE_ORDER:
                tenant = tenant_block(site)
                if layout == "tenant first":
                    blocks = [{"type": "text", "text": tenant}, {"type": "text", "text": shared,
                                                                 "cache_control": {"type": "ephemeral"}}]
                else:
                    blocks = [{"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
                              {"type": "text", "text": tenant, "cache_control": {"type": "ephemeral"}}]
                q = MORNING_QUESTIONS[(rnd * 3 + d3.SITE_ORDER.index(site)) % len(MORNING_QUESTIONS)]
                responses.append(client.messages.create(model=MODEL, max_tokens=2000, system=blocks,
                                                        tools=d3.FIELD_TOOLS,
                                                        messages=[{"role": "user", "content": f"({site}) {q}"}]))
        w, r, c = totals(responses)
        costs[layout] = c
        rows.append([layout, len(responses), w, r, d3.money(c)])
    d3.table(rows, ["system layout", "requests (6 customers x 3)", "cache writes", "cache reads", "cost"])
    print("Tools render first, then system blocks in order: with the tenant block first, every customer's prefix "
          "differs from its first system byte and nothing is shared. Shared card first (breakpoint), tenant next "
          "(breakpoint), conversation last: the card is written once for all six. If the card uses the 1-hour TTL, it "
          "must come before the 5-minute tenant entry - longer TTLs first.")
    return {"saving": costs["tenant first"] - costs["shared first"]}


# ------------------------------------------------------------------------------------------------ step 4: minimums
def step_minimums(client) -> None:
    prompt = [{"type": "text", "text": f"{MARKER}\n" + tenant_block("harbor") + "\n" + CONTRACT * 4,
               "cache_control": {"type": "ephemeral"}}]
    size = client.messages.count_tokens(model=MODEL, system=prompt, messages=[{"role": "user", "content": "x"}])
    rows = []
    for model in (MODEL, MID_MODEL, FAST_MODEL):
        runs = [client.messages.create(model=model, max_tokens=500, system=prompt,
                                       messages=[{"role": "user", "content": "(harbor) What is open today?"}])
                for _ in range(2)]
        rows.append([model, get_spec(model).cache_min_tokens, runs[0].usage.cache_creation_input_tokens,
                     runs[1].usage.cache_read_input_tokens])
    d3.table(rows, ["model", "minimum cacheable", "1st request wrote", "2nd request read"])
    print(f"The same ~{size.input_tokens:,}-token prompt: cached on one model, silently not on the others - no error, "
          "just zeros. Check the minimum whenever a route moves to another model.")


def main() -> None:
    client = get_client()
    header("Lab 07 - Cache engineering at scale")
    print(f"Shared prefix: tools ({len(d3.FIELD_TOOLS)}) + instructions + a reference card of {len(CARD_SECTIONS)} "
          "manual sections.")

    step(1, "Fan-out: eight technicians at 07:00, with and without a pre-warm")
    fan = step_fanout(client)

    step(2, "The 20-position lookback")
    look = step_lookback(client)

    step(3, "Multi-tenant prefix layout")
    ten = step_tenants(client)

    step(4, "Minimum cacheable prefix per model")
    step_minimums(client)

    step(5, "Savings table")
    days, techs = 250, 40
    rows = [["pre-warm before the 07:00 fan-out", d3.money(fan["saving"]), "per 8-technician morning",
             d3.money(fan["saving"] * techs / 8 * days)],
            ["automatic caching through long tool loops", d3.money(look["saving"]), "per long look-up turn",
             d3.money(look["saving"] * techs * 2 * days)],
            ["shared-first prefix layout", d3.money(ten["saving"]), "per 6 cold customer prefixes",
             d3.money(ten["saving"] * techs * days)]]
    d3.table(rows, ["technique", "measured saving", "unit", f"fleet-year ({techs} technicians, {days} days)"])
    print("Multipliers: one morning fan-out per 8 technicians; two long look-up turns per technician-day; six cold "
          "customer prefixes per technician-day (the 5-minute entries lapse between site visits). The per-unit numbers "
          "are measured above; replace the multipliers with your own traffic.")


if __name__ == "__main__":
    main()
