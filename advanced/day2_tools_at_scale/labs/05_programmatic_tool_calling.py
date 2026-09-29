"""Lab 05 - Programmatic tool calling: triage 11 recall units from code instead of 15 round trips.

Objective
    Triage the 11 units of recall RC-2026-03 (advanced/data/recall/affected_units.json): the latest seal-chamber
    temperature of each pump, the week's fault codes of each KC-2 controller, and a vibration trend for the hot ones.
    Do it three ways - direct tool calls one per turn, direct calls in parallel, and one code cell that calls the same
    client tools from Python (programmatic tool calling) - and compare requests, model turns, what enters the model's
    context, and cost. Print the pause/resume transcript of the code path, reuse the container for a second question,
    and trip the protocol's rules.

Concepts
    code_execution_20260120, allowed_callers, tool_use blocks with caller {type, tool_id}, the pause/resume protocol
    (results-only continuation + container id), code_execution_tool_result (stdout/stderr/return_code), asyncio.gather
    to batch calls into one pause, container state across turns (REPL variables), container.expires_at, what enters the
    context in each approach, when code beats round trips and when it does not, the security questions (Day 5)

Run
    python advanced/day2_tools_at_scale/labs/05_programmatic_tool_calling.py

What to observe
    * Direct, one call per turn: 16 requests and a prompt that grows with every result.
    * Direct, parallel: 3 requests, but all 15 raw results sit in the context for the rest of the conversation.
    * Code: the cell pauses twice (11 calls, then 4), your client answers with tool_result blocks only, and only the
      triage table (stdout) reaches the model. The response that only re-pauses the cell has no output tokens.
    * The summary table: the code path reads less and writes more; its saving is what every later turn carries.
    * The second question runs a new cell in the same container and uses `units` defined by the first.
    * Each rule of the protocol, broken once, is a 400.
"""
# test: expect=pause
# test: expect=same container

from __future__ import annotations

import datetime as dt
import sys
import textwrap
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step, text_of, wrap  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = (d2.MARK_PTC + "\nYou are Kestrel's recall analyst. Use the tools to check units; when you can run code, batch the "
          "look-ups in one script and print only what the answer needs. Answer only from tool results.")
TOOL_NAMES = ["get_pump_telemetry", "get_fault_codes", "get_vibration_trend", "get_stock"]


def direct_tools() -> list[dict]:
    return d2.loaded_toolset(TOOL_NAMES)


def code_tools() -> list[dict]:
    """The same four client tools, callable only from code, plus the code-execution server tool."""
    return [dict(d2.CODE_EXECUTION)] + [d2.api_tool(d2.entry(n), allowed_callers=[d2.CODE_CALLER]) for n in TOOL_NAMES]


def result_text(messages: list[dict], code: bool) -> str:
    """Everything tool-produced that the model reads: tool_result blocks (direct) or the cells' stdout (code)."""
    parts = []
    for m in messages:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if not code and isinstance(b, dict) and b.get("type") == "tool_result":
                parts.append(b["content"])
            elif code and getattr(b, "type", "") == "code_execution_tool_result":
                parts.append(b.content.stdout)
    return "\n".join(parts)


def run_direct(client, *, parallel: bool) -> tuple[d2.RunResult, d2.KestrelOps]:
    ops = d2.KestrelOps()
    extra = None if parallel else {"tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}
    run = d2.run_agent(client, system=SYSTEM, tools=direct_tools(), messages=[{"role": "user", "content": d2.triage_question()}],
                       execute=ops.run, max_turns=24, trace=False, extra=extra)
    return run, ops


def show(response, n: int) -> None:
    u = response.usage
    print(f"  <- response {n}: stop_reason={response.stop_reason}  in={d2.prompt_size(u):,} out={u.output_tokens:,}")
    for b in response.content:
        if b.type == "server_tool_use":
            code = b.input.get("code", "")
            print(f"       server_tool_use {b.name} id={b.id}  ({len(code.splitlines())} lines of Python)")
            for line in code.splitlines()[:6]:
                indent = line[:len(line) - len(line.lstrip())]
                print(f"         | {indent}{d2.clip(line, 100 - len(indent))}")
        elif b.type == "tool_use":
            caller = getattr(b, "caller", None)
            print(f"       tool_use {d2.call_label(b, 20)}  caller={{type: {caller.type}, tool_id: {caller.tool_id}}}"
                  if caller else f"       tool_use {d2.call_label(b, 20)}")
        elif b.type == "code_execution_tool_result":
            out = b.content.stdout.strip()
            print(f"       code_execution_tool_result return_code={b.content.return_code} stdout={d2.clip(out, 90)}")
        elif b.type == "text":
            print(f"       text {d2.clip(b.text, 90)}")
    if response.container:
        left = (response.container.expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
        print(f"       container id={response.container.id} (expires_at in about {round(left / 5) * 5:.0f} minutes)")


def run_code_path(client, ops: d2.KestrelOps, messages: list[dict], container: str | None) -> tuple[list, str]:
    """The manual pause/resume loop, printed request by request."""
    responses = []
    n = 1
    kw = {"container": container} if container else {}
    print(f"  -> request {n}: {len(messages)} message(s), last = user text" + (f", container={container}" if container else ""))
    r = client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM, tools=code_tools(), messages=messages, **kw)
    responses.append(r)
    show(r, n)
    while r.stop_reason == "tool_use":
        container = r.container.id
        results = []
        for b in r.content:
            if b.type == "tool_use":
                content, is_error = ops.run(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content, **({"is_error": True} if is_error else {})})
        messages += [{"role": "assistant", "content": r.content}, {"role": "user", "content": results}]
        n += 1
        print(f"  -> request {n}: + assistant turn + user message with {len(results)} tool_result blocks (and nothing else), "
              f"container={container}")
        r = client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM, tools=code_tools(), messages=messages,
                                   container=container)
        responses.append(r)
        show(r, n)
    messages.append({"role": "assistant", "content": r.content})
    return responses, r.container.id if r.container else container


def summary_row(label: str, requests: int, generations: int, calls: int, context_tokens: int, responses: list) -> list:
    inp = sum(d2.prompt_size(r.usage) for r in responses)
    out = sum(r.usage.output_tokens for r in responses)
    cost = sum(d2.response_cost(r) for r in responses)
    return [label, requests, generations, calls, context_tokens, inp, out, d2.money(cost)]


def try_request(client, label: str, **kw) -> None:
    try:
        client.messages.create(model=MODEL, max_tokens=1000, system=SYSTEM, **kw)
        print(f"  [accepted] {label}")
    except anthropic.BadRequestError as exc:
        message = exc.body.get("error", {}).get("message", str(exc)) if isinstance(exc.body, dict) else str(exc)
        print(f"  [400] {label}\n        {message}")


def step_rules(client, paused_messages: list[dict], container: str) -> None:
    try_request(client, "continuation without the container id", tools=code_tools(), messages=paused_messages)
    mixed = paused_messages[:-1] + [{"role": "user", "content": paused_messages[-1]["content"] + [{"type": "text", "text": "thanks"}]}]
    try_request(client, "continuation with a text block after the tool results", tools=code_tools(), messages=mixed,
                container=container)
    try_request(client, "continuation with an unknown (or expired) container id", tools=code_tools(), messages=paused_messages,
                container="container_expired_0001")
    try_request(client, "allowed_callers without a code-execution tool", tools=code_tools()[1:],
                messages=[{"role": "user", "content": "Latest telemetry for KP250-2608-0004?"}])


def main() -> None:
    client = get_client()
    header("Lab 05 - Programmatic tool calling")
    if is_mock():
        print("[mock] the stand-in writes the Python cell (asyncio.gather over the client tools); the mock runs it in a local\n"
              "       sandbox and pauses it exactly like the API when it awaits a client tool. Live, Claude writes its own code.")

    step(1, "The task: 11 units, a triage rule, four client tools")
    print(wrap(d2.triage_question(), "  | "))

    step(2, "Direct tool calls, one per turn (disable_parallel_tool_use)")
    seq, _ = run_direct(client, parallel=False)
    for t in seq.turns[:3] + seq.turns[-2:]:
        print(f"  turn {t.n:>2}: prompt {t.prompt:>6,}  {'; '.join(t.calls) or '(answer)'}")
    print(f"  ... {seq.model_calls} requests in all; the prompt grew from {seq.turns[0].prompt:,} to {seq.turns[-1].prompt:,} tokens.")

    step(3, "Direct tool calls in parallel")
    par, _ = run_direct(client, parallel=True)
    for t in par.turns:
        names = [c.split("(")[0] for c in t.calls]
        print(f"  turn {t.n}: prompt {t.prompt:>6,}  " + (f"{len(names)} calls: {', '.join(sorted(set(names)))}" if names else "(answer)"))
    print("\n  answer:")
    print(wrap(par.reply, "     | "))

    step(4, "The same work from code: the pause/resume transcript")
    ops = d2.KestrelOps()
    messages = [{"role": "user", "content": d2.triage_question()}]
    responses, container = run_code_path(client, ops, messages, None)
    paused = messages[:3]                                           # user, assistant (paused), user (results): for step 7
    final = responses[-1]
    print("\n  answer:")
    print(wrap(text_of(final), "     | "))

    step(5, "Three ways, one table")
    seq_ctx = d2.token_cost(client, result_text(seq.messages, code=False))
    par_ctx = d2.token_cost(client, result_text(par.messages, code=False))
    code_ctx = d2.token_cost(client, result_text(messages, code=True))
    calls = len(ops.calls)
    written = sum(1 for r in responses if any(b.type in ("text", "server_tool_use") for b in r.content))
    rows = [summary_row("direct, one call per turn", seq.model_calls, seq.model_calls, len(seq.calls), seq_ctx, seq.responses),
            summary_row("direct, parallel calls", par.model_calls, par.model_calls, len(par.calls), par_ctx, par.responses),
            summary_row("code cell (programmatic)", len(responses), written, calls, code_ctx, responses)]
    d2.table(rows, ["approach", "requests", "model turns", "tool calls", "tool output in context", "billed input", "out", "cost"])
    print("\n'model turns' counts responses the model wrote (code or text); the other code-path requests only resumed the\n"
          "paused cell with your results. 'tool output in context' is what the model reads: every tool_result for direct\n"
          "calls, only the cell's stdout for the code path - and it stays in the conversation for every later turn.")
    par_row, code_row = rows[1], rows[2]
    par_cost = sum(d2.response_cost(r) for r in par.responses)
    code_cost = sum(d2.response_cost(r) for r in responses)
    verdict = "parallel direct calls are cheaper" if par_cost < code_cost else "the code path is already cheaper"
    print(textwrap.fill(
        f"The code path reads less ({code_row[5]:,} billed input tokens against {par_row[5]:,}): the code-called tool_use "
        f"and tool_result blocks go to the running cell, not to the model, and cost no tokens. It writes more "
        f"({code_row[6]:,} output tokens against {par_row[6]:,}), most of them the cell itself. For this one question "
        f"{verdict}; code pulls ahead as the fan-out grows and as the conversation goes on, since every later turn "
        f"carries {code_ctx:,} tokens of tool output instead of {par_ctx:,} (exercise 3 finds the break-even).", width=114))
    if is_mock():
        print("[mock] token counts are the mock's estimates, and the size of the cell is the stand-in's: live, the cell Claude\n"
              "       writes sets the fixed cost of the code path - measure it before you quote a saving.")

    step(6, "A second question in the same container: state survives between cells")
    messages.append({"role": "user", "content": d2.KITS_QUESTION})
    print(wrap(d2.KITS_QUESTION, "  | "))
    responses2, container2 = run_code_path(client, ops, messages, container)
    print(f"\n  same container: {container2 == container} ({container2}); the cell used `units` from the first cell.")
    print("  answer:")
    print(wrap(text_of(responses2[-1]), "     | "))

    step(7, "The protocol's rules, broken on purpose")
    step_rules(client, paused, container)

    step(8, "When code beats round trips - and when it does not")
    print("  Code wins when the calls are many, independent or mechanically chained, and their raw results matter only in\n"
          "  aggregate (fan-out, joins, filters, sums). Round trips win when the model must read and judge each result before\n"
          "  deciding the next step, when a call has side effects that need a human's approval, when the tool must stay\n"
          "  strict (programmatic tool calling is not compatible with strict: true), or when one or two calls do the job.\n"
          "  Security (Day 5): a cell is model-written code in a sandbox that calls YOUR tools with arguments it computed -\n"
          "  gate every call in the executor exactly as for direct calls; tool results flow into code the model wrote, so\n"
          "  untrusted text in them can steer that code.")


if __name__ == "__main__":
    main()
