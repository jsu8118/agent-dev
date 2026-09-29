"""Lab 02 - Compaction strategies compared: the same 40-turn day, six ways to keep it inside the window.

Objective
    Replay the technician's day with no context management and with five strategies - client-side truncation, your
    own summarisation at site boundaries, server-side compaction, server-side tool-result clearing, and structured
    state extraction (a scratchpad the harness keeps) - then ask eight probe questions about facts from earlier in the
    day. The stand-in model answers only from what survived in its context. Print a quality/cost table, the probe
    matrix, and whether each strategy rewrote history (which restarts the cache and, on binding models, invalidates
    earlier thinking).

Concepts
    context growth over a long session; sliding-window truncation by whole turns; simple compaction (summary + the next
    turn, nothing older replayed); a summarisation fork that reuses the cached prefix; compact_20260112 (beta
    compact-2026-01-12, trigger >= 50,000, instructions, usage.iterations); clear_tool_uses_20250919 (beta
    context-management-2025-06-27, keep / clear_at_least / exclude_tools); structured state extraction with
    output_config.format; probe questions as the quality signal; what each strategy costs.

Run
    python advanced/day3_long_horizon_context/labs/02_compaction_strategies.py [--strategies none,truncate,...]
    Live, all six runs cost about $10 on Claude Opus 5; pick fewer strategies to spend less.

What to observe
    * The baseline answers all eight probes and ends the day at ~64K tokens; every strategy trades some of that.
    * Truncation forgets whatever sat in the oldest turns (the GBWD facts), not what matters least.
    * Server-side compaction fires once (turn 27 in mock mode); the mock's summary keeps only the user requests and
      the tool calls, so most probes fail - read the [mock] note, then the case study.
    * Own summary, clearing and the scratchpad keep 7/8: the heatsink temperature lived only in a raw log export.
    * The scratchpad is the cheapest of the good ones, and none of the three rewrites history mid-conversation.
"""
# test: expect=Probe matrix
# test: expect=scratchpad

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day3 as d3  # noqa: E402

SYSTEM = [{"type": "text", "text": d3.FIELD_SYSTEM, "cache_control": {"type": "ephemeral"}}]
BASE = dict(model=MODEL, max_tokens=16000, tools=d3.FIELD_TOOLS, system=SYSTEM, cache_control={"type": "ephemeral"})
STRATEGIES = ["none", "truncate", "summary", "compact", "clear", "scratchpad"]
TITLES = {"none": "No context management (baseline)", "truncate": "Client-side truncation (sliding window, whole turns)",
          "summary": "Own summarisation at each site boundary (simple compaction)",
          "compact": "Server-side compaction (compact_20260112)",
          "clear": "Server-side tool-result clearing (clear_tool_uses_20250919)",
          "scratchpad": "Structured state extraction (the agent's scratchpad)"}
WINDOW = 30_000                 # truncation keeps the request under this many tokens
COMPACT_TRIGGER = 50_000        # the documented minimum for compact_20260112
CLEAR_TRIGGER = 30_000
KEEP_NOTE = ("Keep verbatim: every finding (site, unit, finding, action, parts), every 'remember for next time' note "
             "with its site, and every fact quoted from a manual with its citation. Drop raw readings and log exports.")
SUMMARY_REQUEST = f"<summarise_request>Summarise today's session so it can continue without the transcript. {KEEP_NOTE}" \
                  "</summarise_request>"
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


def turn_starts(messages: list[dict]) -> list[int]:
    """Indices where a technician turn starts (a user message with text, not tool results)."""
    return [i for i, m in enumerate(messages) if m["role"] == "user" and (
        isinstance(m["content"], str) or any(isinstance(b, dict) and b.get("type") == "text" for b in m["content"]))]


def render_scratchpad(pad: dict[str, dict]) -> str:
    """The harness's state, rendered for the model: one line per item, a site header per site."""
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


class Strategy:
    """One way of keeping the day inside the window: request parameters plus the harness hooks it needs."""

    def __init__(self, client, name: str) -> None:
        self.client, self.name = client, name
        self.params = dict(BASE)
        self.create = client.messages.create
        self.side: list[tuple[str, object]] = []
        self.pending: str | None = None
        self.pad: dict[str, dict] = {}
        self.dropped_turns = 0
        self.last_body: dict | None = None
        self.breaks: list[str] = []
        self.requests = 0
        if name == "clear":
            self.create = client.beta.messages.create
            self.params.update(betas=["context-management-2025-06-27"], context_management={"edits": [{
                "type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": CLEAR_TRIGGER},
                "keep": {"type": "tool_uses", "value": 3},                     # the current turn's calls
                "clear_at_least": {"type": "input_tokens", "value": 10_000},   # each cache break must buy space
                "exclude_tools": ["log_finding"]}]})                           # small, and the report needs them
        elif name == "compact":
            self.create = client.beta.messages.create
            self.params.update(betas=["compact-2026-01-12"], context_management={"edits": [{
                "type": "compact_20260112", "trigger": {"type": "input_tokens", "value": COMPACT_TRIGGER},
                "instructions": "Summarise the field-service session so far. " + KEEP_NOTE}]})

    # ---------------------------------------------------------------- hooks
    def truncate(self, messages: list[dict]) -> None:
        """Drop the oldest whole turns until the request fits the window (never the current turn)."""
        while True:
            n = self.client.messages.count_tokens(model=MODEL, system=SYSTEM, tools=d3.FIELD_TOOLS,
                                                  messages=messages).input_tokens
            starts = turn_starts(messages)
            if n <= WINDOW or len(starts) < 3:
                return
            del messages[:starts[1]]
            self.dropped_turns += 1

    def watch(self, messages: list[dict]) -> None:
        """Record whether this request extends the previous one append-only (what the cache and binding need)."""
        body = d3.as_body(messages, **self.params)
        if self.last_body is not None:
            changes = d3.prefix_changes(self.last_body, body)
            first = body["messages"][0]["content"] if body["messages"] else ""
            if changes and isinstance(first, list) and first and str(first[0].get("text", "")).startswith("<"):
                self.breaks.append("reset")          # a new conversation that starts from the summary/scratchpad
            elif changes:
                self.breaks.append(changes[0])
        self.last_body = body
        self.requests += 1

    def before_request(self, messages: list[dict]) -> None:
        if self.name == "truncate":
            self.truncate(messages)
        self.watch(messages)

    def before_turn(self, s: d3.Step, messages: list[dict]) -> None:
        if self.pending:
            messages.append({"role": "user", "content": [{"type": "text", "text": self.pending}]})
            self.pending = None

    def after_turn(self, s: d3.Step, stats: d3.TurnStats, messages: list[dict]) -> None:
        if s.kind != "depart" or self.name not in ("summary", "scratchpad"):
            return
        if self.name == "summary":           # a fork of the same prefix: it reads the transcript from the cache
            r = self.client.messages.create(messages=messages + [{"role": "user", "content": SUMMARY_REQUEST}], **BASE)
            self.side.append(("summary", r))
            self.pending = f"<conversation_summary>\n{d3.text_of(r)}\n</conversation_summary>"
        else:
            request = f'<scratchpad_request site="{s.site}">Extract this site\'s state.</scratchpad_request>'
            r = self.client.messages.create(messages=messages + [{"role": "user", "content": request}],
                                            output_config={"format": {"type": "json_schema",
                                                                      "schema": SCRATCHPAD_SCHEMA}}, **BASE)
            self.side.append(("scratchpad", r))
            self.pad[s.site] = json.loads(d3.text_of(r))
            self.pending = render_scratchpad(self.pad)
        messages.clear()                        # simple compaction: the next request starts a new conversation

    # ---------------------------------------------------------------- run
    def run(self, steps: list[d3.Step]) -> tuple[d3.DayRun, list]:
        first = steps[0]
        steps = [d3.Step(first.number, first.site, first.kind, f"(strategy: {self.name}) {first.text}")] + steps[1:]
        day = d3.run_day(self.create, self.params, steps=steps, before_turn=self.before_turn,
                         before_request=self.before_request, after_turn=self.after_turn)
        day.side_calls.extend(self.side)
        prepare = (lambda fork: (self.truncate(fork), fork)[1]) if self.name == "truncate" else None
        probes = d3.ask_probes(self.create, self.params, day.messages, prepare=prepare)
        return day, probes


def quality(day: d3.DayRun) -> tuple[int, int]:
    """(sites the end-of-day report still covers with findings, remember-notes recalled) - out of 6 and 5."""
    report = next((t.reply for t in day.turns if t.number == 39), "")
    recall = next((t.reply for t in day.turns if t.number == 40), "")
    sites = sum(1 for s in d3.SITE_ORDER if re.search(rf"^- {s}: (?!no findings)", report, re.M))
    notes = len(re.findall(r"phone call|Sundays 06:00|hearing-protection|13:30|4 hours", recall))
    return sites, notes


def evidence(name: str, strat: Strategy, day: d3.DayRun) -> None:
    if name == "truncate":
        kept = next((t for t in day.turns if t.number == 40), None)
        first_kept = next((m for m in day.messages if m["role"] == "user"), {})
        text = first_kept.get("content") if isinstance(first_kept.get("content"), str) else ""
        print(f"Window {WINDOW:,} tokens: {strat.dropped_turns} oldest turns dropped over the day. At turn 40 the oldest "
              f"message left is: {text[:90]!r}...")
        if kept:
            print(f"Recall at turn 40:\n{wrap(kept.reply)}")
    elif name == "summary":
        r = strat.side[-1][1]
        lines = d3.text_of(r).splitlines()
        print(f"{len(strat.side)} summaries (one per departure). The last one, {len(lines)} lines - the first eight:")
        print(wrap("\n".join(lines[:8])))
    elif name == "compact":
        for t in day.turns:
            for r in t.responses:
                block = next((b for b in r.content if b.type == "compaction"), None)
                if block is not None:
                    its = [(it.type, it.input_tokens, it.output_tokens) for it in r.usage.iterations or []]
                    print(f"Compaction fired at turn {t.number}: usage.iterations {its}")
                    print("The compaction block (all the model keeps from before it):\n" + wrap(block.content[:600]))
        if is_mock():
            print("[mock] The stand-in's summary keeps only the recent user requests and the list of tool calls - "
                  "whatever `instructions` say. A real Claude summary follows your instructions and keeps far more; "
                  "what it drops, you only learn by probing.")
    elif name == "clear":
        cleared_turns = [t.number for t in day.turns if any(getattr(r, "context_management", None) and
                                                             r.context_management.applied_edits for r in t.responses)]
        last = day.turns[-1].responses[-1]
        edit = (last.context_management.applied_edits or [None])[0] if getattr(last, "context_management", None) else None
        history = strat.client.messages.count_tokens(model=MODEL, system=SYSTEM, tools=d3.FIELD_TOOLS,
                                                     messages=day.messages).input_tokens
        print(f"Clearing applied from turn {cleared_turns[0] if cleared_turns else '-'} on, on every request (the "
              f"API edits each request, your history keeps everything). At turn {day.turns[-1].number} your history "
              f"was {history:,} tokens; the model read {d3.prompt_size(last.usage):,}"
              + (f" - {edit.cleared_tool_uses} old tool results ({edit.cleared_input_tokens:,} tokens) replaced by a "
                 "placeholder." if edit else ".") + " Its own replies and every log_finding (excluded) stay.")
    elif name == "scratchpad":
        site = next(iter(strat.pad))
        lines = render_scratchpad({site: strat.pad[site]}).splitlines()[1:-1]
        print(f"{len(strat.pad)} site entries extracted with a JSON schema (output_config.format), validated and "
              f"stored by the harness, and rendered into each new conversation. What the model sees for {site}:")
        print(wrap("\n".join(line[:160] for line in lines)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    parser.add_argument("--turns", type=int, default=40)
    args = parser.parse_args()
    chosen = [s.strip() for s in args.strategies.split(",") if s.strip()]
    steps = d3.SCRIPT[: args.turns]
    client = get_client()
    header("Lab 02 - Compaction strategies compared")
    if is_mock():
        print("[mock] Token counts are estimates; server-side clearing and compaction are simulated with the "
              "documented semantics; the stand-in answers probes only from what is left in its context.")

    results = {}
    for i, name in enumerate(chosen, 1):
        step(i, TITLES[name])
        strat = Strategy(client, name)
        day, probes = strat.run(steps)
        results[name] = (strat, day, probes)
        sites, notes = quality(day)
        print(f"Probes {sum(ok for _, _, ok in probes)}/8, report covers {sites}/6 sites, {notes}/5 notes recalled; "
              f"peak context {day.peak_context:,} tokens; cost {d3.money(day.cost)}.")
        evidence(name, strat, day)

    step(len(chosen) + 1, "Probe matrix - which facts survived where")
    rows = [[p.key, p.lives_in] + ["yes" if dict((q.key, ok) for q, _, ok in results[n][2])[p.key] else "-"
                                   for n in chosen] for p in d3.PROBES]
    d3.table(rows, ["probe", "where the fact was said"] + chosen)
    wrong = [(n, p, a) for n in chosen for p, a, ok in results[n][2] if not ok and not a.startswith("I don't have")]
    if wrong:
        n, p, a = wrong[0]
        print(f"\nNot every miss is an honest 'I don't know'. {n} / {p.key}: {a[:170]}")

    step(len(chosen) + 2, "Quality and cost")
    rows = []
    for n in chosen:
        strat, day, probes = results[n]
        sites, notes = quality(day)
        resets = strat.breaks.count("reset")
        edits = [b.split(" ")[-1] for b in strat.breaks if b != "reset"]
        rewrites = (f"{len(edits)} ({', '.join(sorted(set(edits)))})" if edits else "0") + \
            (f" + {resets} resets" if resets else "")
        rows.append([n, f"{sum(ok for _, _, ok in probes)}/8", f"{sites}/6", f"{notes}/5", day.peak_context,
                     day.turns[-1].prompt, rewrites,
                     d3.money(day.side_cost), d3.money(day.cost)])
    d3.table(rows, ["strategy", "probes", "report", "notes", "peak ctx", "final ctx", "history rewrites",
                    "side calls", "total cost"])
    print("'history rewrites' counts requests that did not extend the previous one append-only: each restarts the "
          "cache from that point and, on a model that binds thinking to the prefix, invalidates the thinking that "
          "follows it (lab 03). A reset starts a new conversation from the summary or scratchpad - the cache restarts, "
          "but nothing old is replayed, so no thinking block is invalidated.")


if __name__ == "__main__":
    main()
