"""Lab 04 - Turn-scoped system messages: per-turn reminders without growing, breaking or spoofing the history.

Objective
    Deliver the same per-turn status reminder to the field agent at Cobalt Chemical four ways - a turn-scoped system
    message (clear_at), a persistent system message, a <system-reminder> injected into the user turn, and the old
    inject-then-delete pattern - and measure with count_tokens what the model reads on each turn, and what each does to
    the cache. Then hit every placement 400, fall into the tool-loop pitfall (a reminder cleared by the tool result),
    fix it, and see what deleting a reminder does to preserved thinking.

Concepts
    mid-conversation {"role": "system"} messages (operator authority, no cache reset); clear_at "next_user_message"
    (beta mid-conversation-system-clear-at-2026-08-21): renders for one turn, then stays in the transcript at 0 tokens;
    placement (after the user message it applies to; last, or followed by an assistant turn); a tool_result message is
    the next user message; re-send after each tool round, never delete earlier copies; user-turn injection (user
    authority, stays forever); token accounting with count_tokens.

Run
    python advanced/day3_long_horizon_context/labs/04_turn_scoped_system_messages.py

What to observe
    * Seven placement mistakes, seven 400s - each message tells you the rule.
    * Turn-scoped: the model reads one copy (~70 tokens) on every turn; persistent copies and user-turn injections
      pile up (seven copies by the last turn). Inject-then-delete reads one copy but rewrites history every turn.
    * The bullet-point reminder is gone by the time the tool results arrive - unless it is re-sent after them.
    * On Fable 5.1, deleting yesterday's reminder is a 400; leaving the cleared copy in place is not.
"""
# test: expect=a system message must be the last message
# test: expect=re-sent after the tool results
# test: expect=Invalid `signature` in `thinking` block

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic  # noqa: E402

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day3 as d3  # noqa: E402

CLEAR_AT = "mid-conversation-system-clear-at-2026-08-21"
SYSTEM = [{"type": "text", "text": d3.FIELD_SYSTEM, "cache_control": {"type": "ephemeral"}}]
VISIT = [s for s in d3.SCRIPT if 27 <= s.number <= 33]                  # the Cobalt Chemical visit
STATUS = ("Status for this turn: site Cobalt Chemical, Zone 1 (ATEX) around CC-KP250X-02/-03 - safety faults are "
          "escalated, never repaired; gas-test certificate valid until 11:40; open work order WO-24466.")
GLOVES = "The technician is at the pump with gloves on: reply in at most three bullet points."


def params(model: str = MODEL, **extra) -> dict:
    return dict(model=model, max_tokens=8000, tools=d3.FIELD_TOOLS, system=SYSTEM, cache_control={"type": "ephemeral"},
                **extra)


# ------------------------------------------------------------------------------------------------ step 1: placement
def placements(client) -> None:
    user = {"role": "user", "content": "Pull the brief for Cobalt."}
    reply = {"role": "assistant", "content": "Brief pulled: four units, one ATEX pair."}
    nxt = {"role": "user", "content": "Anything open?"}
    note = {"role": "system", "content": STATUS, "clear_at": "next_user_message"}
    cases = [
        ("reminder directly before the next user turn", [user, reply, nxt, note, {"role": "user", "content": "And?"}],
         MODEL, [CLEAR_AT]),
        ("system message as the first message", [note, user], MODEL, [CLEAR_AT]),
        ("clear_at without the beta header", [user, reply, nxt, note], MODEL, []),
        ("clear_at with cache_control", [user, reply, nxt, {**note, "content": [
            {"type": "text", "text": STATUS, "cache_control": {"type": "ephemeral"}}]}], MODEL, [CLEAR_AT]),
        ("clear_at: 'next_turn'", [user, reply, nxt, {**note, "clear_at": "next_turn"}], MODEL, [CLEAR_AT]),
        ("turn-scoped message with output_config", [user, reply, nxt, {**note, "output_config": {"effort": "low"}}],
         MODEL, [CLEAR_AT, "mid-conversation-output-config-2026-07-01"]),
        ("any system message on claude-sonnet-5", [user, reply, nxt, {"role": "system", "content": STATUS}],
         "claude-sonnet-5", []),
    ]
    rows, notes = [], []
    for label, messages, model, betas in cases:
        try:
            client.beta.messages.create(model=model, max_tokens=300, messages=messages, **({"betas": betas} if betas
                                                                                            else {}))
            rows.append([label, "accepted"])
        except anthropic.BadRequestError as exc:
            notes.append(d3.api_error(exc))
            rows.append([label, f"400 ({len(notes)})"])
    ok = [user, reply, nxt, note]
    client.beta.messages.create(model=MODEL, max_tokens=300, messages=ok, betas=[CLEAR_AT])
    rows.append(["after the user message it applies to, last", "accepted"])
    d3.table(rows, ["placement", "result"])
    for i, note_text in enumerate(notes, 1):
        print(wrap(f"({i}) {note_text}"))


# ------------------------------------------------------------------------------------------------ step 2: accounting
def is_reminder(m: dict) -> bool:
    return m.get("role") == "system" and "Status for this turn" in str(m.get("content"))


def strip_reminders(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if is_reminder(m):
            continue
        if isinstance(m.get("content"), list):
            m = {**m, "content": [b for b in m["content"] if not (isinstance(b, dict) and
                                                                   str(b.get("text", "")).startswith("<system-reminder>"))]}
        out.append(m)
    return out


class Method:
    """One way to deliver STATUS on every turn; counts what the model reads before each request."""

    def __init__(self, client, name: str, *, count: bool = True) -> None:
        self.client, self.name, self.count = client, name, count
        self.per_turn: dict[int, int] = {}
        self.turn = 0

    def after_user(self, s: d3.Step) -> list[dict]:
        self.turn = s.number
        if self.name == "turn-scoped":
            return [{"role": "system", "content": STATUS, "clear_at": "next_user_message"}]
        if self.name == "persistent":
            return [{"role": "system", "content": STATUS}]
        return []

    def before_request(self, messages: list[dict]) -> None:
        if self.name in ("user-turn", "inject-then-delete"):
            if self.name == "inject-then-delete":            # the old pattern: remove every earlier copy first
                for m in messages:
                    if isinstance(m.get("content"), list):
                        m["content"] = [b for b in m["content"] if not (
                            isinstance(b, dict) and str(b.get("text", "")).startswith("<system-reminder>"))]
            last = max(i for i, m in enumerate(messages) if m["role"] == "user" and (
                isinstance(m["content"], str) or any(b.get("type") == "text" for b in m["content"]
                                                      if isinstance(b, dict))))
            m = messages[last]
            if isinstance(m["content"], str):
                m["content"] = [{"type": "text", "text": m["content"]}]
            if not any(str(b.get("text", "")).startswith("<system-reminder>") for b in m["content"]):
                m["content"].append({"type": "text", "text": f"<system-reminder>{STATUS}</system-reminder>"})
        if self.count and self.turn not in self.per_turn:
            count = self.client.beta.messages.count_tokens
            kw = dict(model=MODEL, system=SYSTEM, tools=d3.FIELD_TOOLS, betas=[CLEAR_AT])
            with_r = count(messages=messages, **kw).input_tokens
            without = count(messages=strip_reminders(messages), **kw).input_tokens
            self.per_turn[self.turn] = with_r - without


def accounting(client) -> dict[str, d3.DayRun]:
    methods = ["turn-scoped", "persistent", "user-turn", "inject-then-delete"]
    runs, counters = {}, {}
    for name in methods:
        method = Method(client, name)
        first = VISIT[0]
        steps = [d3.Step(first.number, first.site, first.kind, f"({name}) {first.text}")] + VISIT[1:]
        runs[name] = d3.run_day(client.beta.messages.create, params(betas=[CLEAR_AT]), steps=steps,
                                after_user=method.after_user, before_request=method.before_request)
        counters[name] = method
    rows = []
    for s in VISIT:
        rows.append([s.number] + [counters[n].per_turn.get(s.number, 0) for n in methods])
    rows.append(["total"] + [sum(counters[n].per_turn.values()) for n in methods])
    print("Reminder tokens the model reads on each turn (count_tokens with the reminders minus without):")
    d3.table(rows, ["turn"] + methods)
    rows = []
    for n in methods:
        day = runs[n]
        read = sum(t.cache_read for t in day.turns)
        billed = sum(t.cache_read + t.cache_write + t.uncached for t in day.turns)
        rows.append([n, sum(t.cache_write for t in day.turns), f"{read / billed:.0%}", d3.money(day.cost)])
    print("\nWhat it did to the cache over the visit:")
    d3.table(rows, ["method", "cache writes", "cache-read share", "visit cost"])
    print("Turn-scoped copies keep their place in the prefix and cost nothing once cleared; persistent messages and "
          "injected blocks are re-read on every later turn; inject-then-delete keeps the prompt small but rewrites the "
          "previous user turn on every request, so each request writes again what the last one cached.")
    return runs


# ------------------------------------------------------------------------------------------------ step 3: tool loop
def tool_loop(client) -> None:
    visit = [s for s in d3.SCRIPT if 27 <= s.number <= 30]
    gloves = {"role": "system", "content": GLOVES, "clear_at": "next_user_message"}
    rows = []
    for label, resend in [("only after the technician's message", False), ("re-sent after the tool results", True)]:
        first = visit[0]
        steps = [d3.Step(first.number, first.site, first.kind, f"({label}) {first.text}")] + visit[1:]
        after_user = (lambda s: [dict(gloves)] if s.number == 30 else [])
        state = {"on": False}

        def before_turn(s, messages, state=state):
            state["on"] = s.number == 30

        def after_results(messages, state=state, resend=resend):
            if resend and state["on"]:
                messages.append(dict(gloves))
        day = d3.run_day(client.beta.messages.create, params(betas=[CLEAR_AT]), steps=steps, after_user=after_user,
                         before_turn=before_turn, after_results=after_results)
        t = day.turn(30)
        rows.append([label, t.requests, "yes" if t.reply.startswith("- ") else "no", t.reply.splitlines()[0][:70]])
    d3.table(rows, ["gloves reminder placed", "requests", "bullets?", "first line of the answer"])
    print("Turn 30 needs a tool round (the F07 rule is in the manual). The tool_result message is the next user "
          "message, so it clears a reminder placed only after the technician's question: the answer that follows the "
          "tool results never sees it. Append a fresh copy after each tool_result message you want it in view for, "
          "and leave every earlier copy where it is.")


# ------------------------------------------------------------------------------------------------ step 4: scope
def scope(client) -> None:
    visit = [s for s in d3.SCRIPT if s.number in (30, 33, 34, 35)]
    rows = []
    for label, clear_at in [("persistent (clear_at 'never')", "never"), ("turn-scoped", "next_user_message")]:
        steps = [d3.Step(v.number, v.site, v.kind, v.text) for v in visit]
        steps[0] = d3.Step(30, "cobalt", "lookup", f"({label}) " + visit[0].text)
        after_user = (lambda s, c=clear_at: [{"role": "system", "content": GLOVES, "clear_at": c}]
                      if s.number == 30 else [])
        day = d3.run_day(client.beta.messages.create, params(betas=[CLEAR_AT]), steps=steps, after_user=after_user)
        t = day.turn(35)
        rows.append([label, "yes" if t.reply.startswith("- ") else "no", t.reply.splitlines()[0][:78]])
    d3.table(rows, ["gloves reminder sent once at turn 30", "turn 35 in bullets?", "turn 35 (Westfield, desk work)"])
    print("A persistent system message is right for a rule that stays true (end it with another appended message, "
          "never by deleting it); a turn-scoped one is right for a fact about this turn.")


# ------------------------------------------------------------------------------------------------ step 5: binding
def binding(client) -> None:
    visit = [s for s in d3.SCRIPT if 35 <= s.number <= 36]
    model = "claude-fable-5-1"
    rows, notes = [], []
    for label in ("turn-scoped copies left in place", "inject-then-delete"):
        name = "turn-scoped" if label.startswith("turn") else "inject-then-delete"
        method = Method(client, name, count=False)
        try:
            d3.run_day(client.beta.messages.create, params(model, betas=[CLEAR_AT]), steps=visit,
                       after_user=method.after_user, before_request=method.before_request)
            rows.append([label, "both turns answered"])
        except anthropic.BadRequestError as exc:
            notes.append(d3.api_error(exc))
            rows.append([label, f"400 ({len(notes)}) on the second turn"])
    d3.table(rows, ["reminders on Claude Fable 5.1", "result"])
    for i, note_text in enumerate(notes, 1):
        print(wrap(f"({i}) {note_text}"))
    print("Deleting last turn's reminder edits a message that comes before this turn's thinking blocks: an enforced "
          "model rejects the replay. A cleared turn-scoped message renders nothing but keeps its place in the prefix.")


def main() -> None:
    client = get_client()
    header("Lab 04 - Turn-scoped system messages")
    if is_mock():
        print("(mock mode: the stand-in obeys the reminders it can see - system messages that are in force and "
              "<system-reminder> blocks in the current turn; token counts are estimates)")

    step(1, "Placement rules - seven ways to get a 400")
    placements(client)

    step(2, "Four ways to deliver a per-turn status line, measured over the Cobalt visit")
    accounting(client)

    step(3, "The tool-loop pitfall: a reminder cleared by the tool result")
    tool_loop(client)

    step(4, "Scope: a fact about this turn vs a rule that stays true")
    scope(client)

    step(5, "Deleting a reminder on a model that binds thinking to the prefix")
    binding(client)


if __name__ == "__main__":
    main()
