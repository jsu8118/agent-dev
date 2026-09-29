"""Lab 03 - Preserved thinking: which history edits break binding, on which model, and how to keep a harness safe.

Objective
    Build a short tool-using conversation on Claude Fable 5.1, Claude Opus 5.5 and Claude Opus 5, then replay it
    after each of the edits real harnesses make - rewording an old turn, trimming an old tool result, dropping the
    oldest turn, re-rendering the system prompt, changing the tools - and after three harmless changes. See the 400,
    `prefix_mismatch_behavior: "drop_block"` and `input_transformations`; switch models mid-conversation; compare
    keep-tail and simple compaction; and finish with the compatibility checklist as code - a guard that names the
    broken rule before the request is sent.

Concepts
    thinking signatures bound to the producing model and the conversation prefix (system, tool set, earlier
    messages); enforced (Fable 5.1 here) vs recorded (Opus 5.5 here) vs no prefix check (Opus 5); beta
    thinking-binding-controls-2026-08-01; thinking.block_binding.prefix_mismatch_behavior "error" | "drop_block";
    input_transformations (thinking_dropped / thinking_mismatch_allowed; prefix_binding_mismatch /
    model_binding_mismatch); model binding across upgrades; keep-tail vs simple compaction; append-only harnesses.

Run
    python advanced/day3_long_horizon_context/labs/03_preserved_thinking_binding.py

What to observe
    * Every history edit is a 400 on Fable 5.1, a list of thinking_mismatch_allowed on Opus 5.5 (enforced only when
      the request sets prefix_mismatch_behavior), and nothing at all on Opus 5; reordering tools, appending a system
      message and changing max_tokens or effort are harmless everywhere.
    * drop_block drops the first mismatched block and every thinking block after it - for that request only.
    * Switching Opus 5.5 -> Opus 5 drops its blocks (model_binding_mismatch, a 200); Opus 5.5 -> Fable 5.1 keeps them.
    * A downgrade hides an edit for one request; the edit surfaces as a 400 on the next Fable 5.1 turn.
    * Keep-tail compaction is a 400; simple compaction (summary + the new turn) is clean.
    * The guard flags the bad harness's first rewrite in CI mode; in production mode it counts the dropped blocks.
"""
# test: expect=Invalid `signature` in `thinking` block
# test: expect=model_binding_mismatch
# test: expect=HarnessViolation

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic  # noqa: E402

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day3 as d3  # noqa: E402

FABLE, OPUS55, OPUS5 = "claude-fable-5-1", "claude-opus-5-5", MODEL
BINDING = "thinking-binding-controls-2026-08-01"
CLEAR_AT = "mid-conversation-system-clear-at-2026-08-21"
VISIT = [s for s in d3.SCRIPT if s.number in (35, 36, 37)]          # three look-ups at Westfield, one tool round each
NEXT = {"role": "user", "content": "One more: what do I check before I leave plant room B2?"}


def plain(messages: list[dict]) -> list[dict]:
    """The history as JSON-ready dicts (what the harness would store and replay)."""
    return d3.as_body(messages)["messages"]


def conversation(client, model: str) -> tuple[dict, list[dict]]:
    params = dict(model=model, max_tokens=4000, tools=d3.FIELD_TOOLS, system=d3.FIELD_SYSTEM,
                  cache_control={"type": "ephemeral"})
    day = d3.run_day(client.messages.create, params, steps=VISIT)
    return params, plain(day.messages)


def thinking_paths(messages: list[dict]) -> list[str]:
    return [f"messages.{i}.content.{j}" for i, m in enumerate(messages) if m["role"] == "assistant"
            for j, b in enumerate(m["content"]) if isinstance(b, dict) and b.get("type") == "thinking"]


# ------------------------------------------------------------------------------------------------ the edits
def reword(p, m):
    m[0] = {"role": "user", "content": m[0]["content"] + " Thanks."}
    return p, m


def trim_result(p, m):
    for b in m[2]["content"]:
        if b.get("type") == "tool_result":
            b["content"] = "[trimmed to save tokens]"
    return p, m


def drop_oldest(p, m):
    starts = [i for i, x in enumerate(m) if x["role"] == "user" and isinstance(x["content"], str)]
    return p, m[starts[1]:]


def rerender_system(p, m):
    return {**p, "system": p["system"] + "\nLocal time: 11:05."}, m


def edit_tool(p, m):
    tools = copy.deepcopy(p["tools"])
    tools[0]["description"] += " Updated 2026-09-15."
    return {**p, "tools": tools}, m


def add_tool(p, m):
    extra = {"name": "get_weather", "description": "Weather at a site", "input_schema": {"type": "object",
                                                                                        "properties": {}}}
    return {**p, "tools": p["tools"] + [extra]}, m


def reorder_tools(p, m):
    return {**p, "tools": list(reversed(p["tools"]))}, m


def append_system(p, m):
    return p, m                                    # the system message is appended after the new user turn


def change_params(p, m):
    return {**p, "max_tokens": 3000, "output_config": {"effort": "low"}}, m


EDITS = [("reword an earlier question", reword), ("trim an old tool result", trim_result),
         ("drop the oldest turn", drop_oldest), ("re-render the system prompt", rerender_system),
         ("change a tool description", edit_tool), ("add a tool", add_tool),
         ("reorder the tools", reorder_tools), ("append a system message", append_system),
         ("change max_tokens and effort", change_params)]
COLUMNS = [(FABLE, None, "Fable 5.1"), (FABLE, "drop_block", "Fable 5.1 drop_block"),
           (OPUS55, "header", "Opus 5.5 + header"), (OPUS55, "error", "Opus 5.5 'error'"), (OPUS5, "header", "Opus 5")]


def send(client, params: dict, messages: list[dict], mode: str | None, *, model: str | None = None):
    kw = dict(params, messages=messages)
    if model:
        kw["model"] = model
    if mode == "header":
        kw["betas"] = [BINDING]
    elif mode in ("drop_block", "error"):
        kw["betas"] = [BINDING]
        kw["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": mode}}
    return client.beta.messages.create(**kw)


def verdict(client, params, messages, mode, **kw) -> tuple[str, object]:
    try:
        r = send(client, params, messages, mode, **kw)
    except anthropic.BadRequestError as exc:
        return "400", exc
    tr = getattr(r, "input_transformations", None)
    if tr is None:
        return "200", r
    if not tr:
        return "200 []", r
    kinds: dict[str, int] = {}
    for t in tr:
        label = {"thinking_dropped": "dropped", "thinking_mismatch_allowed": "allowed"}.get(t.type, t.type)
        label += {"prefix_binding_mismatch": " (prefix)", "model_binding_mismatch": " (model)"}.get(t.reason, "")
        kinds[label] = kinds.get(label, 0) + 1
    return "200, " + ", ".join(f"{n} {k}" for k, n in kinds.items()), r


def step_edits(client, convs: dict[str, tuple[dict, list[dict]]]) -> None:
    rows, first_400 = [], None
    for name, fn in EDITS:
        row = [name]
        for model, mode, _ in COLUMNS:
            params, msgs = convs[model]
            p2, m2 = fn(dict(params), copy.deepcopy(msgs))
            tail = [NEXT] + ([{"role": "system", "content": "Keep answers under 60 words."}]
                             if fn is append_system else [])
            cell, obj = verdict(client, p2, m2 + tail, mode)
            if cell == "400" and first_400 is None:
                first_400 = (name, obj)
            row.append(cell)
        rows.append(row)
    d3.table(rows, ["edit before the new turn"] + [c[2] for c in COLUMNS])
    if first_400:
        print(f"\nThe 400, in full ({first_400[0]}, Fable 5.1, no beta header):")
        print(wrap(d3.api_error(first_400[1])))
    print("\nReading the columns: Fable 5.1 enforces the check (as every model does for accounts created on or after "
          "2026-08-31); Opus 5.5 here behaves like an older account - the mismatch is recorded, listed with the beta "
          "header, and enforced only when the request sets prefix_mismatch_behavior (any value); Opus 5 blocks carry "
          "no conversation check. Set the field explicitly in every environment so all accounts behave alike.")


def step_drop_block(client, convs) -> None:
    params, msgs = convs[FABLE]
    p2, m2 = trim_result(dict(params), copy.deepcopy(msgs))
    r = send(client, p2, m2 + [NEXT], "drop_block")
    print(f"Thinking blocks in the replayed history: {', '.join(thinking_paths(m2))}")
    print("After trimming the tool result in messages.2, the response's input_transformations:")
    for t in r.input_transformations:
        print(f"  {{type: {t.type}, path: {t.path}, reason: {t.reason}}}")
    print("The first block after the edit and every block after it were dropped (unbilled) - the edit invalidates the "
          "rest of the chain. The drop applies to this request only: keep sending drop_block for the rest of the "
          "session, or stop editing history.")


def step_switch(client, convs) -> None:
    rows = []
    for src, dst in [(OPUS55, OPUS5), (OPUS55, FABLE), (FABLE, OPUS55), (OPUS5, OPUS55), (OPUS5, FABLE)]:
        params, msgs = convs[src]
        cell, _ = verdict(client, params, msgs + [NEXT], "header", model=dst)
        rows.append([f"{src} -> {dst}", cell])
    # a round trip: Opus 5.5, one turn on Opus 5, back to Opus 5.5
    params, msgs = convs[OPUS55]
    down = send(client, params, msgs + [NEXT], "header", model=OPUS5)
    back = msgs + [NEXT, {"role": "assistant", "content": plain([{"role": "assistant", "content": down.content}])[0][
        "content"]}, {"role": "user", "content": "Thanks - and the permit?"}]
    cell, _ = verdict(client, params, back, "header", model=OPUS55)
    rows.append([f"{OPUS55} -> {OPUS5} -> {OPUS55}", cell + " (on the return)"])
    d3.table(rows, ["conversation moved", "result (beta header on)"])
    print("A block the new model cannot read is dropped, not rejected: a 200 with model_binding_mismatch entries and "
          "no charge for the dropped tokens. Opus 5.5 blocks are readable only by Fable 5.1 (and Mythos 5.1); Fable "
          "5.1 blocks by no other model here; Opus 5 blocks by everyone. Keep sending the same messages to every model "
          "- never strip blocks on a switch - so a round trip loses nothing.")

    params, msgs = convs[FABLE]
    p2, m2 = reword(dict(params), copy.deepcopy(msgs))
    cell, _ = verdict(client, p2, m2 + [NEXT], "error", model=OPUS5)
    back, obj = verdict(client, p2, m2 + [NEXT], "error")
    print(f"\nA downgrade hides an edit: reworded history sent to {OPUS5} with 'error' -> {cell} (only the model "
          f"check ran); the same history back on {FABLE} -> {back}.")


def step_compaction(client, convs) -> None:
    params, msgs = convs[FABLE]
    starts = [i for i, x in enumerate(msgs) if x["role"] == "user" and isinstance(x["content"], str)]
    summary = {"role": "user", "content": "<conversation_summary>Westfield plant room B2: KC-1 key parameters and "
                                          "defaults read [UM-KC1 §3]; safety faults F07/F17 and the reset rule read "
                                          "[UM-KC1 §5].</conversation_summary>"}
    tail = msgs[starts[-1]:]                                # the last turn, verbatim, thinking included
    keep_tail = [summary, {"role": "assistant", "content": "Understood."}] + copy.deepcopy(tail) + [NEXT]
    stripped = [summary, {"role": "assistant", "content": "Understood."}] + [
        {**m, "content": [b for b in m["content"] if b.get("type") != "thinking"]} if m["role"] == "assistant" else m
        for m in copy.deepcopy(tail)] + [NEXT]
    simple = [{"role": "user", "content": summary["content"] + "\n\n" + NEXT["content"]}]
    rows = [["keep-tail: summary + last turn verbatim", verdict(client, params, keep_tail, None)[0]],
            ["keep-tail, retained turn's thinking stripped", verdict(client, params, stripped, None)[0]],
            ["keep-tail with drop_block", verdict(client, params, keep_tail, "drop_block")[0]],
            ["simple compaction: summary + the new turn", verdict(client, params, simple, "header")[0]]]
    d3.table(rows, ["client-side compaction shape (Fable 5.1)", "result"])
    print("The retained turn's thinking was minted with the full history in front of it, so replaying it after a "
          "summary fails the check. Simple compaction replays nothing older than the summary. Server-side compaction "
          "and context editing never count as edits: the API checks the conversation as you sent it, and after a "
          "compaction block the checked prefix starts at the block.")


# ------------------------------------------------------------------------------------------------ the checklist as code
RULES = {
    "system_rerendered": ("Freeze the top-level system prompt for the session",
                          "append a {'role': 'system'} message when something changes"),
    "tool_set_changed": ("Declare every tool at session start",
                         "defer_loading + tool_addition / tool_removal (beta mid-conversation-tool-changes-2026-07-01)"),
    "tool_schema_changed": ("Freeze each tool's text for the conversation", "store and replay the definitions as sent"),
    "blocks_removed": ("Never drop, reword or re-order earlier turns",
                       "server-side clearing/compaction, or simple compaction at a boundary"),
    "blocks_modified": ("Never edit an earlier turn (tool results included)",
                        "bound tool outputs before the first send; clear server-side"),
    "blocks_inserted": ("Never insert into an earlier turn", "append instead"),
}


class HarnessViolation(RuntimeError):
    pass


class BindingGuard:
    """Wraps `create` for ONE conversation: before each request, compare it with the previous one and name the rule it
    breaks; send every request with the binding beta and an explicit prefix_mismatch_behavior; count what the API
    dropped. mode="ci" raises on the first violation ("error" semantics); mode="prod" degrades (drop_block)."""

    def __init__(self, client, mode: str) -> None:
        self.client, self.mode = client, mode
        self.prev: dict | None = None
        self.violations: list[tuple[int, str]] = []
        self.dropped = 0
        self.requests = 0

    def create(self, **kw):
        self.requests += 1
        body = d3.as_body(kw["messages"], system=kw.get("system"), tools=kw.get("tools"))
        if self.prev is not None:
            for change in d3.prefix_changes(self.prev, body):
                kind = change.split(" ")[-1]
                self.violations.append((self.requests, change))
                if self.mode == "ci":
                    rule, fix = RULES.get(kind, ("Append-only history", "append instead"))
                    raise HarnessViolation(f"request {self.requests}: {change} - rule: {rule}; fix: {fix}")
        self.prev = body
        kw = dict(kw, betas=sorted(set(kw.get("betas", [])) | {BINDING}),
                  thinking={"type": "adaptive", "block_binding": {
                      "prefix_mismatch_behavior": "error" if self.mode == "ci" else "drop_block"}})
        r = self.client.beta.messages.create(**kw)
        self.dropped += sum(1 for t in r.input_transformations if t.type == "thinking_dropped")
        return r


def bad_harness(create):
    """A harness written for older models: a clock line re-rendered into the system prompt on every request, and a
    reminder injected into the newest user turn and deleted on the next request."""
    state = {"n": 0}

    def wrapped(**kw):
        state["n"] += 1
        messages = kw["messages"]
        for m in messages:                                     # delete the previous request's reminder
            if isinstance(m["content"], list):
                m["content"] = [b for b in m["content"] if not (isinstance(b, dict) and
                                                                str(b.get("text", "")).startswith("<system-reminder>"))]
        last = next(m for m in reversed(messages) if m["role"] == "user")
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append({"type": "text", "text": "<system-reminder>Plant room B2 is a hospital site: no "
                                                         "circulation outage without redundancy.</system-reminder>"})
        clock = f"Local time: {13 + state['n'] // 6:02d}:{(state['n'] * 7) % 60:02d}."
        return create(**dict(kw, system=kw["system"] + "\n" + clock))
    return wrapped


def good_after_user(s: d3.Step) -> list[dict]:
    """The append-only form of the same two things: a turn-scoped system message after the technician's message."""
    return [{"role": "system", "clear_at": "next_user_message",
             "content": f"Local time: 13:{(s.number * 7) % 60:02d}. Plant room B2 is a hospital site: no circulation "
                        "outage without redundancy."}]


def step_guard(client) -> None:
    visit = [s for s in d3.SCRIPT if 34 <= s.number <= 38]
    params = dict(model=FABLE, max_tokens=4000, tools=d3.FIELD_TOOLS, system=d3.FIELD_SYSTEM,
                  cache_control={"type": "ephemeral"})
    print("The checklist the guard enforces (rule -> append-only fix):")
    for kind, (rule, fix) in RULES.items():
        print(f"  {kind:<20} {rule} -> {fix}")
    rows = []
    for label, mode, make in [("bad harness", "ci", "bad"), ("bad harness", "prod", "bad"),
                              ("append-only harness", "ci", "good"), ("append-only harness", "prod", "good")]:
        guard = BindingGuard(client, mode)
        create = bad_harness(guard.create) if make == "bad" else guard.create
        hooks = {"after_user": good_after_user} if make == "good" else {}
        p = dict(params, betas=[CLEAR_AT]) if make == "good" else params
        try:
            day = d3.run_day(create, p, steps=visit, **hooks)
            outcome = f"{len(day.turns)} turns completed"
        except HarnessViolation as exc:
            outcome = f"stopped: {exc}".split(" - rule")[0]
            print(f"\n{type(exc).__name__}: {exc}")
        rows.append([label, mode, guard.requests, len(guard.violations), guard.dropped, outcome])
    print()
    d3.table(rows, ["harness", "mode", "requests", "violations", "thinking dropped", "outcome"])
    print("CI runs with 'error' so an edit fails the build; production runs with 'drop_block' and alerts on "
          "input_transformations - either way the setting is explicit, so new and old accounts behave the same.")


def main() -> None:
    client = get_client()
    header("Lab 03 - Preserved thinking: binding, drops and a compatible harness")
    if is_mock():
        print("[mock] Signatures are HMACs over the producing model and the prefix digest; Fable 5.1 enforces the "
              "check like a new account, Opus 5.5 records it like an older one, Opus 5 has none.")

    step(1, "Three turns on each model")
    convs = {m: conversation(client, m) for m in (FABLE, OPUS55, OPUS5)}
    for m, (_, msgs) in convs.items():
        paths = thinking_paths(msgs)
        print(f"  {m:<17} {len(msgs)} messages, {len(paths)} thinking blocks at {', '.join(paths)}")
    print("Each signature records who produced the block and the prefix it was produced in: the top-level system "
          "prompt, the tool set, and every message before it. The harness must replay the blocks unchanged.")

    step(2, "Replay after each edit (the next turn is appended after the edit)")
    step_edits(client, convs)

    step(3, "drop_block and input_transformations")
    step_drop_block(client, convs)

    step(4, "Switching models mid-conversation")
    step_switch(client, convs)

    step(5, "Where compaction resets the prefix")
    step_compaction(client, convs)

    step(6, "The compatibility checklist as code")
    step_guard(client)


if __name__ == "__main__":
    main()
