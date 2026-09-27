"""Lab 02 - The agent loop, from scratch.

Objective
    Write the loop every tool-using agent runs - call the model, execute the tools it asks for, send the
    results back, repeat until it answers - with production guards: a max-iteration guard, handling for
    EVERY stop_reason, is_error results, and a per-turn trace that shows the history (and the bill)
    growing turn by turn.

Concepts
    Manual agent loop; stop_reason end_turn / tool_use / max_tokens / stop_sequence / pause_turn /
    refusal / model_context_window_exceeded; never executing a possibly truncated tool call; all
    tool_results for one assistant turn in ONE user message; turn budgets; input-token growth.

Run
    python day2_tools_agent_loop/labs/02_agent_loop_from_scratch.py

What to observe
    * Run 1: turn-by-turn input tokens rise although each turn adds only a little - every request re-sends
      the system prompt, the tool definitions and the whole history.
    * Run 2: the max-iteration guard stops a run that would need more turns, and hands over safely.
    * Run 3: a starved max_tokens budget truncates a turn; the loop drops that turn (it may contain a
      half-written tool call) and retries it with a bigger budget.
    * The stop-reason drill shows the decision the loop takes for every stop_reason, including the ones
      that are hard to trigger on demand (refusal, pause_turn).
"""

# test: expect=Per-turn trace
# test: expect=max-iteration guard
# test: expect=Stop-reason drill

from __future__ import annotations

from dataclasses import dataclass, field

from anthropic.types import Message

from _order_desk import SYSTEM_PROMPT, TOOLS, OrderDesk
from labkit import MODEL, cost_usd, get_client, header, step, text_of, wrap

MAX_TOKENS_CAP = 16_000          # above ~21k the SDK insists on streaming for non-streaming calls
SAFE_HANDOVER = ("I couldn't complete this automatically, so I've passed it to a colleague who will follow up.")


@dataclass
class TurnRecord:
    turn: int
    stop_reason: str
    tools: list[str]
    input_tokens: int            # everything the model read: uncached + cache writes + cache reads
    output_tokens: int           # thinking + text + tool_use, all billed as output
    cost_usd: float
    note: str = ""


@dataclass
class AgentRun:
    answer: str
    status: str                  # done | truncated | max_turns | refused | error
    turns: list[TurnRecord] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)


def next_action(response: Message) -> str:
    """Map a response to what the loop must do next. Pure function: easy to unit-test."""
    reason = response.stop_reason
    has_tool_use = any(b.type == "tool_use" for b in response.content)
    has_text = any(b.type == "text" and b.text.strip() for b in response.content)
    if reason == "refusal":
        return "refused"                    # check BEFORE reading content: it may be partial
    if reason in ("max_tokens", "model_context_window_exceeded"):
        # Output was cut off. A tool_use block may hold a truncated (yet parseable) input: never run it.
        return "retry_bigger" if (has_tool_use or not has_text) else "finish_truncated"
    if reason == "tool_use":
        return "run_tools" if has_tool_use else "finish"
    if reason == "pause_turn":
        return "resume"                     # server-side tool loop paused: send the turn back unchanged
    if reason in ("end_turn", "stop_sequence"):
        return "finish"
    return "finish"                         # unknown future value: stop safely rather than loop


def run_agent(client, question: str, desk: OrderDesk, *, max_turns: int = 8, max_tokens: int = 8000,
              tools: list[dict] = TOOLS, system: str = SYSTEM_PROMPT) -> AgentRun:
    messages: list[dict] = [{"role": "user", "content": question}]
    run = AgentRun(answer="", status="error", messages=messages)
    budget = max_tokens
    for turn in range(1, max_turns + 1):
        response = client.messages.create(model=MODEL, max_tokens=budget, system=system, tools=tools,
                                          messages=messages)
        usage = response.usage
        record = TurnRecord(turn, response.stop_reason or "?",
                            [f"{b.name}({', '.join(f'{v}' for v in b.input.values())})"
                             for b in response.content if b.type == "tool_use"],
                            usage.input_tokens + (usage.cache_creation_input_tokens or 0)
                            + (usage.cache_read_input_tokens or 0),
                            usage.output_tokens, cost_usd(usage, response.model))
        run.turns.append(record)
        action = next_action(response)

        if action == "refused":
            run.answer, run.status = SAFE_HANDOVER, "refused"
            record.note = f"refusal ({getattr(response.stop_details, 'category', None)}): handed to a human"
            return run
        if action == "retry_bigger":
            if budget >= MAX_TOKENS_CAP:
                run.answer, run.status = SAFE_HANDOVER, "truncated"
                record.note = "still truncated at the cap: giving up safely"
                return run
            budget = min(budget * 2, MAX_TOKENS_CAP)
            record.note = f"truncated turn DROPPED (not executed); retrying with max_tokens={budget}"
            continue                                     # the truncated turn is never appended

        messages.append({"role": "assistant", "content": response.content})   # verbatim, thinking included
        if action == "resume":
            record.note = "pause_turn: re-sending the paused turn as-is"
            continue
        if action in ("finish", "finish_truncated"):
            run.answer = text_of(response)
            run.status = "done" if action == "finish" else "truncated"
            if action == "finish_truncated":
                record.note = "answer cut off at max_tokens: returned flagged as truncated"
            return run

        # action == "run_tools": execute EVERY tool_use block, answer all of them in ONE user message.
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            content, is_error = desk.run(block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content,
                            "is_error": is_error})
            if is_error:
                record.note += f"{block.name} -> is_error; "
        messages.append({"role": "user", "content": results})

    run.answer, run.status = SAFE_HANDOVER, "max_turns"     # the max-iteration guard
    return run


def print_trace(run: AgentRun) -> None:
    print(f"{'turn':>4}  {'stop_reason':<12} {'input tok':>9} {'cum. input':>10} {'output':>7} {'cost $':>8}  "
          "tool calls / notes")
    cumulative = 0
    for t in run.turns:
        cumulative += t.input_tokens
        calls = ", ".join(t.tools) or "-"
        print(f"{t.turn:>4}  {t.stop_reason:<12} {t.input_tokens:>9,} {cumulative:>10,} {t.output_tokens:>7,} "
              f"{t.cost_usd:>8.4f}  {calls}" + (f"   [{t.note.strip()}]" if t.note else ""))
    total = sum(t.cost_usd for t in run.turns)
    print(f"status={run.status}  turns={len(run.turns)}  total input={cumulative:,} tokens  total cost=${total:.4f}")


def stop_reason_drill() -> None:
    """Feed synthetic responses through next_action(): the table every loop must implement."""
    text = {"type": "text", "text": "SO-10303 shipped on 2026-09-11."}
    call = {"type": "tool_use", "id": "toolu_drill_1", "name": "get_order_status", "input": {"order_id": "SO-1030"}}
    server = {"type": "server_tool_use", "id": "srvtoolu_drill_1", "name": "web_search", "input": {"query": "x"}}
    cases = [
        ("end_turn", [text], "answer is complete"),
        ("tool_use", [text, call], "execute tools, send ALL results in one user message"),
        ("max_tokens", [call], "input may be truncated ('SO-1030'...): never execute; retry bigger"),
        ("max_tokens", [text], "answer cut off: return it flagged, or retry bigger"),
        ("stop_sequence", [text], "your stop sequence matched: treat as finished"),
        ("pause_turn", [server], "server tool paused: append the turn and re-send, no new user text"),
        ("refusal", [], "declined: do not use content; hand over / fall back"),
        ("model_context_window_exceeded", [text], "context full: like max_tokens; compact or trim history"),
    ]
    print(f"{'stop_reason':<31} {'content':<22} {'loop action':<17} why")
    for reason, blocks, why in cases:
        msg = Message.model_validate({"id": "msg_drill", "type": "message", "role": "assistant", "model": MODEL,
                                      "content": blocks, "stop_reason": reason, "stop_sequence": None,
                                      "usage": {"input_tokens": 0, "output_tokens": 0}})
        kinds = "+".join(b["type"] for b in blocks) or "(none)"
        print(f"{reason:<31} {kinds:<22} {next_action(msg):<17} {why}")


def main() -> None:
    client = get_client()
    desk = OrderDesk()
    header(f"Lab 02 - the agent loop from scratch ({MODEL})")
    print("Tools offered to the model:", ", ".join(t["name"] for t in TOOLS))

    question = ("Siobhan Byrne at Aurora Pharma says order SO-10279 was promised for 8 September, tracking has shown "
                "'exception' for days and nobody from Kestrel has called them. What happened, is she entitled to "
                "compensation under our shipping policy, and has the invoice for this order been paid?")

    step(1, "Run 1 - a normal run with a per-turn trace")
    print(wrap("Staff question: " + question))
    run = run_agent(client, question, desk)
    print("\nPer-turn trace (input tokens = everything re-sent on that turn):")
    print_trace(run)
    print("\nFinal answer:\n" + wrap(run.answer))

    step(2, "Run 2 - the max-iteration guard (same question, max_turns=2)")
    guarded = run_agent(client, question, OrderDesk(), max_turns=2)
    print_trace(guarded)
    print(f"-> max-iteration guard result: status={guarded.status!r}; reply: {guarded.answer}")
    print("   A guard turns 'loops forever and burns money' into 'hands over after N turns'. In production also cap "
          "tokens, dollars and wall-clock time per run.")

    step(3, "Run 3 - starving max_tokens (thinking + text + tool_use must fit in it)")
    starved = run_agent(client, "Where is order SO-10303 right now?", OrderDesk(), max_tokens=100)
    print_trace(starved)
    print(f"Answer returned (status={starved.status!r}): {starved.answer!r}")
    print("-> Truncated turns were dropped, not executed, and retried with a doubled budget. max_tokens is a "
          "backstop, not a length control: use >= 2000 even for short answers and 8000-16000 for agent turns.")

    step(4, "Stop-reason drill - what the loop does for every stop_reason")
    stop_reason_drill()

    step(5, "The history the loop built (run 1)")
    print(f"Run 1 ended with {len(run.messages)} messages in the history:")
    for m in run.messages:
        content = m["content"]
        kinds = ["text"] if isinstance(content, str) else [
            (b["type"] if isinstance(b, dict) else b.type) for b in content]
        print(f"  {m['role']:<9} {kinds}")


if __name__ == "__main__":
    main()
