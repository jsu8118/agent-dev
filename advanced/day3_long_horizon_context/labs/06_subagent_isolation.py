"""Lab 06 - Subagent isolation: read six site logs through subagents instead of stuffing them into one context.

Objective
    Before leaving the depot the technician asks for a plan of the day and then keeps asking about the sites all
    morning. Design A stuffs all six site-log exports into the coordinator's context as documents. Design B sends each
    log to an isolated reader subagent (in parallel, on a cheaper model) that returns a summary contract - a JSON
    schema checked in code, with line pointers into the source - and the coordinator works from six small summaries,
    re-reading a source only when a question needs a detail the contract does not carry. Compare tokens, cost, the
    coordinator's context and the answers.

Concepts
    context isolation; the subagent as a pure function (log in, contract out, no tools, no history); summary contracts
    (output_config.format + jsonschema + verifiable source_line pointers + completeness checks); fan-out in parallel;
    a drill-down tool for details outside the contract; model choice per role; what stuffing costs on every later turn.

Run
    python advanced/day3_long_horizon_context/labs/06_subagent_isolation.py

What to observe
    * Stuffing puts ~50K tokens in front of every later question; the subagent design keeps the coordinator near 2K.
    * Every summary validates against the schema and every source_line pointer checks out against the log.
    * The heatsink question is outside the contract: the coordinator calls ask_site_reader, which re-reads one log.
    * Both designs answer the same questions; the subagent design costs a fraction, and the gap grows with every
      further turn of the day.
"""
# test: expect=pointers verified
# test: expect=ask_site_reader

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jsonschema  # noqa: E402

from labkit import MID_MODEL, MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day3 as d3  # noqa: E402

READER_SYSTEM = """<adv_day3_site_reader>
You read one site's log export for a Kestrel field technician and return exactly the summary contract: open items, \
safety flags, parts already on site, operator notes as facts with the line number they came from, fault counts, and \
how many readings you scanned. Copy facts verbatim. Report only what the log says."""
PLANNER_SYSTEM = """<adv_day3_planner>
You plan a Kestrel field technician's day across six sites and answer questions about the sites all morning. When a \
question needs a detail your context does not have, use ask_site_reader to have the site's log re-read."""
CONTRACT = {
    "type": "object", "additionalProperties": False,
    "required": ["site", "open_items", "safety_flags", "parts_on_site", "facts", "fault_counts", "readings_scanned"],
    "properties": {
        "site": {"type": "string"},
        "open_items": {"type": "array", "items": {"type": "string"}},
        "safety_flags": {"type": "array", "items": {"type": "string"}},
        "parts_on_site": {"type": "array", "items": {"type": "string"}},
        "facts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["fact", "source_line"],
                                             "properties": {"fact": {"type": "string"},
                                                            "source_line": {"type": "integer"}}}},
        "fault_counts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                    "required": ["code", "count"],
                                                    "properties": {"code": {"type": "string"},
                                                                   "count": {"type": "integer"}}}},
        "readings_scanned": {"type": "integer"},
    },
}
ASK_TOOL = {"name": "ask_site_reader", "description": "Have one site's log export re-read by a reader for a detail the "
            "summaries do not carry. Returns the matching log line with its line number.",
            "input_schema": {"type": "object", "properties": {"site_id": {"type": "string"},
                                                              "question": {"type": "string"}},
                             "required": ["site_id", "question"]}}
PLAN = "Plan my day across the six sites: for each, what is open, what is dangerous, and what is already on site."
FOLLOW_UPS = [
    "What does Cobalt require before I go into the Zone 1 area?",
    "Which parts are already on site at Harbor Foods?",
    "What knocking noise did the operators report on GB-KP400-02?",
    "What is the fan module hour counter at Riverbend?",
    "What heatsink temperature did Riverbend's KC-1 log at its latest F05 trip?",
    "What is open at Cedar Creek, and is the pump isolated?",
]


def document(site: str) -> dict:
    return {"type": "document", "title": site, "source": {"type": "text", "media_type": "text/plain",
                                                          "data": d3.site_log(site)}}


def usage_of(responses: list) -> tuple[int, float]:
    return sum(d3.prompt_size(r.usage) for r in responses), sum(d3.response_cost(r) for r in responses)


# ------------------------------------------------------------------------------------------------ design A
def stuffing(client) -> dict:
    docs = [document(s) for s in d3.SITE_ORDER]
    docs[-1] = {**docs[-1], "cache_control": {"type": "ephemeral"}}       # the six logs are the shared prefix
    messages = [{"role": "user", "content": docs + [{"type": "text", "text": PLAN}]}]
    responses, answers = [], []
    r = client.messages.create(model=MODEL, max_tokens=4000, system=PLANNER_SYSTEM, messages=messages)
    responses.append(r)
    messages.append({"role": "assistant", "content": r.content})
    plan = d3.text_of(r)
    for q in FOLLOW_UPS:
        messages.append({"role": "user", "content": q})
        r = client.messages.create(model=MODEL, max_tokens=4000, system=PLANNER_SYSTEM, messages=messages,
                                   cache_control={"type": "ephemeral"})
        responses.append(r)
        messages.append({"role": "assistant", "content": r.content})
        answers.append(d3.text_of(r))
    return {"plan": plan, "answers": answers, "responses": responses, "side": [],
            "context": d3.prompt_size(responses[-1].usage)}


# ------------------------------------------------------------------------------------------------ design B
def read_site(client, site: str, question: str | None = None):
    ask = f"<question>{question}</question>" if question else "Summarise this log into the contract."
    kwargs = {} if question else {"output_config": {"format": {"type": "json_schema", "schema": CONTRACT}}}
    return client.messages.create(model=MID_MODEL, max_tokens=4000, system=READER_SYSTEM,
                                  messages=[{"role": "user", "content": [document(site), {"type": "text", "text": ask}]}],
                                  **kwargs)


def check_contract(site: str, summary: dict) -> tuple[int, int, list[str]]:
    """Schema, pointers and completeness - checked in code before anything reaches the coordinator."""
    problems: list[str] = []
    jsonschema.validate(summary, CONTRACT)
    lines = d3.site_log(site).splitlines()
    ok = 0
    for f in summary["facts"]:
        n = f["source_line"]
        if 1 <= n <= len(lines) and f["fact"] in lines[n - 1]:
            ok += 1
        else:
            problems.append(f"{site}: fact not found at line {n}")
    readings = sum(1 for ln in lines if ln[:4].isdigit() and "T" in ln[:11])
    if summary["readings_scanned"] != readings:
        problems.append(f"{site}: reader says {summary['readings_scanned']} readings, the log has {readings}")
    return ok, len(summary["facts"]), problems


def subagents(client) -> dict:
    with cf.ThreadPoolExecutor(max_workers=6) as pool:                  # six isolated readers, in parallel
        futures = {site: pool.submit(read_site, client, site) for site in d3.SITE_ORDER}
        readers = {site: f.result() for site, f in futures.items()}
    summaries, verified, total, problems = {}, 0, 0, []
    for site, r in readers.items():
        summaries[site] = json.loads(d3.text_of(r))
        ok, n, issues = check_contract(site, summaries[site])
        verified, total, problems = verified + ok, total + n, problems + issues
    blocks = [{"type": "text", "text": f'<site_summary site="{s}">{json.dumps(summaries[s], ensure_ascii=False)}'
                                        f'</site_summary>'} for s in d3.SITE_ORDER]
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    messages = [{"role": "user", "content": blocks}]                     # the plan request follows as its own turn
    responses, side, answers, drills = [], list(readers.values()), [], []
    params = dict(model=MODEL, max_tokens=4000, system=PLANNER_SYSTEM, tools=[ASK_TOOL])

    def execute(name: str, args: dict) -> tuple[str, bool]:
        if args.get("site_id") not in d3.SITE_BY_ID:                   # tool errors go back to the model
            return f"Error: unknown site {args.get('site_id')!r}; use one of {', '.join(d3.SITE_ORDER)}.", True
        r = read_site(client, args["site_id"], args["question"])      # a targeted re-read of one source
        side.append(r)
        drills.append((args["site_id"], d3.text_of(r)))
        return d3.text_of(r), False

    plan = d3.run_turn(client.messages.create, params=params, messages=messages, text=PLAN, execute=execute)
    responses += plan.responses
    for q in FOLLOW_UPS:
        t = d3.run_turn(client.messages.create, params=dict(params, cache_control={"type": "ephemeral"}),
                        messages=messages, text=q, execute=execute)
        responses += t.responses
        answers.append(t.reply)
    return {"plan": plan.reply, "answers": answers, "responses": responses, "side": side, "drills": drills,
            "summaries": summaries, "checks": (verified, total, problems),
            "context": d3.prompt_size(responses[-1].usage)}


def main() -> None:
    client = get_client()
    header("Lab 06 - Subagent isolation: six site logs, stuffed or read by subagents")
    if is_mock():
        print("(mock mode: the readers and the coordinator are rule-based stand-ins that report only what the log or "
              "their context contains; parallel requests run, but the mock has no real latency to save)")

    step(1, "What the logs weigh")
    sizes = {s: client.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": [document(s)]}])
             .input_tokens for s in d3.SITE_ORDER}
    d3.table([[s, n] for s, n in sizes.items()] + [["all six", sum(sizes.values())]], ["site log", "tokens"])

    step(2, "Design A - stuff all six logs into the coordinator's context")
    a = stuffing(client)
    print(wrap(a["plan"]))

    step(3, "Design B - six reader subagents in parallel, then plan from their contracts")
    b = subagents(client)
    verified, total, problems = b["checks"]
    sizes_b = {s: len(json.dumps(v)) for s, v in b["summaries"].items()}
    print(f"Six summaries, {sum(d3.approx_tokens(json.dumps(v)) for v in b['summaries'].values()):,} tokens in all "
          f"(largest: {max(sizes_b, key=sizes_b.get)}). Contract checks: schema valid for 6/6, {verified}/{total} "
          f"pointers verified against the source lines, {len(problems)} problems.")
    for p in problems:
        print("  problem:", p)
    print("The riverbend contract, as the coordinator receives it:")
    print(wrap(json.dumps(b["summaries"]["riverbend"], ensure_ascii=False)))
    print("\nThe plan:")
    print(wrap(b["plan"]))

    step(4, "The same questions, all morning")
    for q, ans_a, ans_b in zip(FOLLOW_UPS, a["answers"], b["answers"]):
        print(f"Q: {q}\n  A (stuffed):   {ans_a[:150]}\n  B (subagents): {ans_b[:150]}")
    for site, text in b["drills"]:
        print(f"Drill-down via ask_site_reader({site}): {text[:120]}")

    step(5, "Tokens and cost")
    in_a, cost_a = usage_of(a["responses"])
    in_b, cost_b = usage_of(b["responses"])
    side_in, side_cost = usage_of(b["side"])
    d3.table([["A: stuffed logs", len(a["responses"]), 0, in_a, a["context"], d3.money(cost_a)],
              ["B: subagents + coordinator", len(b["responses"]), len(b["side"]), in_b + side_in, b["context"],
               d3.money(cost_b + side_cost)]],
             ["design", "coordinator calls", "reader calls", "prompt tokens", "coordinator context at the end",
              "cost"])
    per_turn_a = a["context"] * d3.price(MODEL) * 0.1
    per_turn_b = b["context"] * d3.price(MODEL) * 0.1
    print(f"Each further turn of the day re-reads the coordinator's context: {d3.money(per_turn_a)} (A) vs "
          f"{d3.money(per_turn_b)} (B) of cached input per request - over the day's ~76 requests, "
          f"{d3.money(76 * per_turn_a)} vs {d3.money(76 * per_turn_b)} - before counting what 50K tokens of raw readings "
          "do to attention (context rot).")
    print(f"The readers ran on {MID_MODEL} (${d3.price(MID_MODEL) * 1e6:.0f}/MTok input) while the coordinator stayed "
          f"on {MODEL}; a reader has no tools and no history, so a hostile line in one log can reach the coordinator "
          "only as a string inside a schema field - a preview of Day 5.")


if __name__ == "__main__":
    main()
