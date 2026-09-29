"""Lab 04 - Mid-conversation tool changes: change the toolset between phases without losing the cache.

Objective
    Run one support conversation through four phases (diagnose -> schedule -> communicate -> back to diagnose), each
    with its own tools, twice: (A) re-sending a different `tools` array per phase, and (B) declaring every phase tool
    up front with `defer_loading` and switching phases with `tool_addition` / `tool_removal` blocks in a
    `{"role": "system"}` message. Compare cache reads and cost turn by turn. Then add a tool that did not exist when
    the conversation started with an inline definition, and trip the rules on purpose to see the 400s.

Concepts
    beta mid-conversation-tool-changes-2026-07-01, tool_addition / tool_removal with tool_reference, tools declared with
    defer_loading as the menu the application can surface, beta inline-tools-2026-09-15 and tool_definition, the
    prefix rule (tools render first, so a changed tools array rewrites everything), placement rules for tool changes,
    executor-side gating of tools outside the phase, preserved thinking and tool edits

Run
    python advanced/day2_tools_at_scale/labs/04_mid_conversation_tool_changes.py

What to observe
    * Variant A: the first request of every phase reads at most the tools+system prefix and rewrites the conversation.
    * Variant B: cache reads keep growing through every phase change; writes stay about one turn's worth.
    * The total and the cost of A vs B in the summary table.
    * The inline definition: get_recall_status is added by value and called on the same turn.
    * Four deliberate mistakes and the exact 400 each one gets.
"""
# test: expect=cache-preserving
# test: expect=get_recall_status

from __future__ import annotations

import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = (d2.MARK_PHASES + "\nYou are Kestrel's operations copilot. The tools you have change as the conversation moves from "
          "diagnosis to scheduling to customer communication; use the ones you have now and say plainly when you lack one. "
          "Answer only from tool results.")
PHASE_TOOLS = list(dict.fromkeys(n for p in d2.PHASES for n in p["tools"]))


def ref(kind: str, name: str) -> dict:
    return {"type": kind, "tool": {"type": "tool_reference", "name": name}}


def gated(ops: d2.KestrelOps, allowed: set[str]):
    """The executor-side half of a phase: a call to a tool outside the phase is refused, whatever the model saw."""
    def execute(name: str, tool_input: dict) -> tuple[str, bool]:
        ops.allowed = allowed
        return ops.run(name, tool_input)
    return execute


def variant_a(client) -> list[tuple[str, d2.Turn]]:
    """Re-send a different tools array per phase: core + the phase's tools, all loaded."""
    ops, messages, rows = d2.KestrelOps(), [], []
    for phase in d2.PHASES:
        tools = d2.loaded_toolset(d2.core_names() + phase["tools"])
        for text in phase["turns"]:
            messages.append({"role": "user", "content": text})
            run = d2.run_agent(client, system=SYSTEM, tools=tools, messages=messages, trace=False,
                               execute=gated(ops, set(d2.core_names() + phase["tools"])))
            rows += [(phase["name"], t) for t in run.turns]
    return rows


def variant_b(client, *, inline: bool = True) -> tuple[list[tuple[str, d2.Turn]], list[dict], d2.RunResult]:
    """Declare everything once (phase tools deferred) and switch phases with tool_addition / tool_removal."""
    tools = d2.wide_toolset(search=None, names=d2.core_names() + PHASE_TOOLS)          # no search tool: deferral needs none
    for t in tools:
        t["defer_loading"] = t["name"] in PHASE_TOOLS
    ops, messages, rows, current = d2.KestrelOps(), [], [], set()
    run = None
    for phase in d2.PHASES:
        wanted = set(phase["tools"])
        changes = [ref("tool_removal", n) for n in PHASE_TOOLS if n in current - wanted] + \
                  [ref("tool_addition", n) for n in phase["tools"] if n not in current]
        for i, text in enumerate(phase["turns"]):
            messages.append({"role": "user", "content": text})
            if i == 0 and changes:
                messages.append({"role": "system", "content": changes})                # after the user turn, last in messages
            run = d2.run_agent(client, system=SYSTEM, tools=tools, messages=messages, trace=False, betas=[d2.TOOL_CHANGES_BETA],
                               execute=gated(ops, set(d2.core_names()) | wanted))
            rows += [(phase["name"], t) for t in run.turns]
        current = wanted
    if inline:                                         # a tool the recall team shipped after the conversation started
        definition = {k: v for k, v in d2.EXTRA_TOOLS["get_recall_status"].items() if k != "meta"}
        messages.append({"role": "user", "content": d2.RECALL_STATUS_TURN})
        messages.append({"role": "system", "content": [{"type": "tool_addition",
                                                        "tool": {"type": "tool_definition", "definition": definition}}]})
        run = d2.run_agent(client, system=SYSTEM, tools=tools, messages=messages, trace=False, betas=[d2.INLINE_TOOLS_BETA],
                           execute=gated(ops, set(d2.core_names()) | current | {"get_recall_status"}))
        rows += [("inline", t) for t in run.turns]
    return rows, messages, run


def print_rows(rows: list[tuple[str, d2.Turn]]) -> None:
    table_rows, previous = [], None
    for i, (phase, t) in enumerate(rows, 1):
        mark = "<- phase change" if phase != previous and previous is not None else ""
        table_rows.append([i, phase, t.prompt, t.cache_read, t.cache_write, t.input_tokens, d2.money(t.cost),
                           "; ".join(c.split("(")[0] for c in t.calls) or "(answer)", mark])
        previous = phase
    d2.table(table_rows, ["req", "phase", "prompt", "cache read", "cache write", "uncached", "cost", "calls", ""])


def totals(rows: list[tuple[str, d2.Turn]]) -> list:
    turns = [t for _, t in rows]
    return [len(turns), sum(t.prompt for t in turns), sum(t.cache_read for t in turns), sum(t.cache_write for t in turns),
            sum(t.input_tokens for t in turns), sum(t.output_tokens for t in turns), d2.money(sum(t.cost for t in turns))]


def try_request(client, label: str, **kw) -> None:
    try:
        client.beta.messages.create(model=MODEL, max_tokens=1000, system=SYSTEM, **kw)
        print(f"  [accepted] {label}")
    except anthropic.BadRequestError as exc:
        message = exc.body.get("error", {}).get("message", str(exc)) if isinstance(exc.body, dict) else str(exc)
        print(f"  [400] {label}\n        {message}")


def step_rules(client) -> None:
    tools = d2.wide_toolset(search=None, names=d2.core_names() + ["decode_fault_code"])
    tools[-1]["defer_loading"] = True
    add = {"role": "system", "content": [ref("tool_addition", "decode_fault_code")]}
    user = {"role": "user", "content": "Decode F05 for the KC-1 family."}
    try_request(client, "tool_addition without the beta header", tools=tools, messages=[user, add])
    loaded = d2.loaded_toolset(d2.core_names() + ["decode_fault_code"])
    try_request(client, "tool_addition of a tool that is already loaded (not deferred)", tools=loaded, messages=[user, add],
                betas=[d2.TOOL_CHANGES_BETA])
    removal = {"role": "system", "content": [ref("tool_removal", "decode_fault_code")]}
    try_request(client, "tool_removal followed by another user message", tools=tools,
                messages=[user, removal, {"role": "user", "content": "And F17?"}], betas=[d2.TOOL_CHANGES_BETA])
    definition = {k: v for k, v in d2.EXTRA_TOOLS["get_recall_status"].items() if k != "meta"}
    inline = {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_definition", "definition": definition}}]}
    try_request(client, "inline tool_definition under the by-reference beta only", tools=tools, messages=[user, inline],
                betas=[d2.TOOL_CHANGES_BETA])
    try_request(client, "the same request with the inline-tools beta", tools=tools, messages=[user, inline],
                betas=[d2.INLINE_TOOLS_BETA])


def main() -> None:
    client = get_client()
    header("Lab 04 - Mid-conversation tool changes")
    if is_mock():
        print("[mock] the stand-in only uses the tools it can see in each phase; the cache is simulated with the API's rules.")

    step(1, "The conversation and its phases")
    for p in d2.PHASES:
        print(f"  {p['name']:<12} tools: {', '.join(p['tools'])}")
        for t in p["turns"]:
            print(wrap(t, "      > "))
    print(f"  (inline)     tools: get_recall_status - defined by value mid-conversation\n{wrap(d2.RECALL_STATUS_TURN, '      > ')}")

    step(2, "Variant A: re-send a different tools array in each phase")
    rows_a = variant_a(client)
    print_rows(rows_a)

    step(3, "Variant B: declare once with defer_loading, switch with tool_addition / tool_removal")
    rows_b, messages, last = variant_b(client)
    print_rows(rows_b)
    changes = [m for m in messages if m["role"] == "system"]
    print(f"\n{len(changes)} system messages carried the changes; the first phase change (request 3) was this message:")
    lines = [f'      {{"type": "{b["type"]}", "tool": {{"type": "tool_reference", "name": "{b["tool"]["name"]}"}}}}'
             for b in changes[1]["content"]]
    print('  {"role": "system", "content": [\n' + ",\n".join(lines) + "\n  ]}")

    step(4, "The same conversation, two ways")
    base = rows_b[: len(rows_a)]
    d2.table([["A: re-send tools per phase", *totals(rows_a)], ["B: tool_addition / tool_removal", *totals(base)]],
             ["variant", "requests", "prompt", "cache read", "cache write", "uncached", "out", "cost"])
    a_cost = sum(t.cost for _, t in rows_a)
    b_cost = sum(t.cost for _, t in base)
    print(f"\nB costs {b_cost / a_cost:.0%} of A on the same {len(rows_a)} requests: the cache-preserving path keeps the whole\n"
          "conversation readable across phase changes, because the tools block (position 0 of the prompt) never changes; the\n"
          "additions and removals are appended after the cached prefix. A's phase changes each rewrite the prefix from the\n"
          "tools block on. On models with preserved thinking (Opus 5.5, Fable 5.1) A also invalidates every earlier thinking\n"
          "block (a changed tool set is an edit of the prefix they are bound to); B does not.")

    step(5, "A tool that did not exist at the start: an inline definition (beta inline-tools-2026-09-15)")
    print(f"  calls on the last turn: {', '.join(last.calls) or 'none'}")
    print("  answer:")
    print(wrap(last.reply, "     | "))
    print("\nThe definition travels in the message, so the tools array (and the cache) stays as it was. From that message on\n"
          "the definition is part of the history: keep the header on every later request, and change the tool again only by\n"
          "appending another tool_addition (a new definition replaces the old one) - never by editing the message.")

    step(6, "Rules, on purpose: four mistakes and the 400 each one gets")
    step_rules(client)
    if is_mock():
        print("[mock] the error texts are the mock's, phrased after the API's; match on the rule, not the exact string.")


if __name__ == "__main__":
    main()
