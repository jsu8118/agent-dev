"""Exercise 11 (starter) - A summary contract that keeps what the probes need.

In lab 02 the own-summary and scratchpad strategies answered 7 of 8 probes: the heatsink temperature at Riverbend's
latest F05 trip lived only in a controller-log export, and neither the summary instructions nor the scratchpad schema
asked for it. Change the contract - the instructions and the schema, not the harness - so that both strategies answer
all eight probes plus one of your own, then measure what the richer contract costs.

Run
    python advanced/day3_long_horizon_context/exercises/ex11_summary_contract.py

TODO
    1. Extend SUMMARY_INSTRUCTIONS so the summary keeps the facts the heatsink probe needs.
    2. Add a field to SCRATCHPAD_SCHEMA for the same facts, and render it in render_scratchpad().
    3. Add a probe to MY_PROBES: a fact from the day you expect a summary to drop (the fan module's hour counter
       at Riverbend is one), then make the contract keep it too.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client  # noqa: E402

import _day3 as d3  # noqa: E402

SYSTEM = [{"type": "text", "text": d3.FIELD_SYSTEM, "cache_control": {"type": "ephemeral"}}]
BASE = dict(model=MODEL, max_tokens=16000, tools=d3.FIELD_TOOLS, system=SYSTEM, cache_control={"type": "ephemeral"})

# ---- the contract (lab 02's) ---------------------------------------------------------------------------------------
SUMMARY_INSTRUCTIONS = ("Summarise today's session so it can continue without the transcript. Keep verbatim: every "
                        "finding (site, unit, finding, action, parts), every 'remember for next time' note with its "
                        "site, and every fact quoted from a manual with its citation. Drop raw readings and log "
                        "exports.")
SCRATCHPAD_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["site_id", "findings", "facts", "open_items", "remember", "parts_used"],
    "properties": {
        "site_id": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["unit", "finding", "action", "parts"],
            "properties": {"unit": {"type": "string"}, "finding": {"type": "string"}, "action": {"type": "string"},
                           "parts": {"type": "array", "items": {"type": "string"}}}}},
        "facts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["fact", "source"],
            "properties": {"fact": {"type": "string"}, "source": {"type": "string"}}}},
        "open_items": {"type": "array", "items": {"type": "string"}},
        "remember": {"type": "array", "items": {"type": "string"}},
        "parts_used": {"type": "array", "items": {"type": "string"}},
    },
}
MY_PROBES: list[d3.Probe] = []          # TODO 3


def render_scratchpad(pad: dict[str, dict]) -> str:
    """How the harness shows its stored state to the model (lab 02's rendering)."""
    lines = ["<scratchpad>"]
    for site, e in pad.items():
        lines.append(f"site {site}:")
        lines += [f"finding: {site} / {f['unit']} - {f['finding']}; action: {f['action']}; parts: "
                  f"{', '.join(f['parts']) or 'none'}" for f in e["findings"]]
        lines += [f"fact ({site}): {f['fact']}" for f in e["facts"]]
        lines += [f"remember ({site}): {n}" for n in e["remember"]]
        lines += [f"open ({site}): {o}" for o in e["open_items"]]
    lines.append("</scratchpad>")
    return "\n".join(lines)


# ---- the harness (lab 02's summary and scratchpad strategies, unchanged) -------------------------------------------
def run(client, strategy: str, *, instructions: str, schema: dict, render, probes: list[d3.Probe]) -> dict:
    state = {"pending": None, "pad": {}, "side": [], "summary_tokens": 0}

    def before_turn(s, messages):
        if state["pending"]:
            messages.append({"role": "user", "content": [{"type": "text", "text": state["pending"]}]})
            state["pending"] = None

    def after_turn(s, stats, messages):
        if s.kind != "depart":
            return
        if strategy == "summary":
            request = f"<summarise_request>{instructions}</summarise_request>"
            r = client.messages.create(messages=messages + [{"role": "user", "content": request}], **BASE)
            state["pending"] = f"<conversation_summary>\n{d3.text_of(r)}\n</conversation_summary>"
        else:
            request = f'<scratchpad_request site="{s.site}">Extract this site\'s state.</scratchpad_request>'
            r = client.messages.create(messages=messages + [{"role": "user", "content": request}],
                                       output_config={"format": {"type": "json_schema", "schema": schema}}, **BASE)
            state["pad"][s.site] = json.loads(d3.text_of(r))
            state["pending"] = render(state["pad"])
        state["side"].append((strategy, r))
        state["summary_tokens"] = d3.approx_tokens(state["pending"])
        messages.clear()

    first = d3.SCRIPT[0]
    steps = [d3.Step(first.number, first.site, first.kind, f"({strategy}, {len(instructions)}) {first.text}")] + \
        d3.SCRIPT[1:]
    day = d3.run_day(client.messages.create, BASE, steps=steps, before_turn=before_turn, after_turn=after_turn)
    day.side_calls.extend(state["side"])
    results = d3.ask_probes(client.messages.create, BASE, day.messages, probes=d3.PROBES + probes)
    return {"day": day, "probes": results, "last_summary_tokens": state["summary_tokens"]}


def report(label: str, res: dict) -> list:
    passed = sum(ok for _, _, ok in res["probes"])
    missed = [p.key for p, _, ok in res["probes"] if not ok]
    return [label, f"{passed}/{len(res['probes'])}", ", ".join(missed) or "-", res["last_summary_tokens"],
            d3.money(res["day"].cost)]


def main() -> None:
    client = get_client()
    print("Exercise 11 - a summary contract that keeps what the probes need")
    rows = []
    for strategy in ("summary", "scratchpad"):
        res = run(client, strategy, instructions=SUMMARY_INSTRUCTIONS, schema=copy.deepcopy(SCRATCHPAD_SCHEMA),
                  render=render_scratchpad, probes=MY_PROBES)
        rows.append(report(f"{strategy} (lab 02 contract)", res))
    d3.table(rows, ["strategy", "probes", "missed", "last summary (tokens)", "day cost"])
    print("TODO 1: extend SUMMARY_INSTRUCTIONS; TODO 2: add a field to SCRATCHPAD_SCHEMA and render it; "
          "TODO 3: add a probe to MY_PROBES - then aim for every probe on both strategies.")


if __name__ == "__main__":
    main()
