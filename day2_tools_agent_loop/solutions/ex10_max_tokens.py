"""Solution to exercise 10 - robust max_tokens handling in a manual loop.

Rules implemented (reasoning in solutions/README.md):
1. A turn that stopped on max_tokens (or model_context_window_exceeded) with a tool_use block - or with no
   usable text - is DROPPED: never executed, never appended.  The same request is retried with the next,
   larger budget from a bounded ladder.
2. Budgets above what the SDK accepts without streaming (~21,333 tokens: it estimates >10 minutes and
   raises ValueError client-side) are sent with streaming, and the final message is assembled with
   get_final_message() - the rest of the loop cannot tell the difference.
3. A truncated TEXT answer is retried too (a half-finished customer email must never be sent); when the
   ladder is exhausted the loop hands over instead of shipping the fragment.
4. The ladder is "sticky": the next turn starts at the budget that last succeeded, so a conversation does not
   pay for the same truncated attempts on every turn.
5. Every attempt is recorded, so truncation shows up in your traces and cost reports.

Run
    python day2_tools_agent_loop/solutions/ex10_max_tokens.py
"""

# test: expect=streamed=True
# test: expect=handed over

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _order_desk import SYSTEM_PROMPT, TOOLS, OrderDesk  # noqa: E402
from labkit import MODEL, get_client, header, step, text_of, wrap  # noqa: E402

NON_STREAMING_MAX = 21_333          # the SDK's 10-minute estimate: 3600 s * max_tokens / 128,000 > 600 s
DEFAULT_LADDER = (8_000, 16_000, 32_000)
TRUNCATED = ("max_tokens", "model_context_window_exceeded")


class Truncated(Exception):
    """Every budget on the ladder produced a truncated turn."""


@dataclass
class Attempt:
    max_tokens: int
    streamed: bool
    stop_reason: str
    output_tokens: int


@dataclass
class TurnResult:
    response: object
    attempts: list[Attempt] = field(default_factory=list)


def usable(response) -> bool:
    """A response we may act on: not truncated, or truncated text we have decided to accept (we don't)."""
    return response.stop_reason not in TRUNCATED


def create_with_ladder(client, ladder: tuple[int, ...] = DEFAULT_LADDER, **params) -> TurnResult:
    result = TurnResult(response=None)
    for budget in ladder:
        streamed = budget > NON_STREAMING_MAX
        if streamed:
            with client.messages.stream(max_tokens=budget, **params) as stream:
                response = stream.get_final_message()
        else:
            response = client.messages.create(max_tokens=budget, **params)
        result.attempts.append(Attempt(budget, streamed, response.stop_reason, response.usage.output_tokens))
        if response.stop_reason == "refusal" or usable(response):
            result.response = response
            return result
        # Truncated: whatever it contains (a half-written tool call, half an email) is discarded.
    raise Truncated(result.attempts)


def run_agent(client, question: str, *, ladder: tuple[int, ...] = DEFAULT_LADDER, max_turns: int = 8) -> dict:
    desk = OrderDesk()
    messages: list[dict] = [{"role": "user", "content": question}]
    attempts: list[Attempt] = []
    rung = 0                                                   # sticky: start where the last turn succeeded
    for _ in range(max_turns):
        try:
            turn = create_with_ladder(client, ladder[rung:], model=MODEL, system=SYSTEM_PROMPT, tools=TOOLS,
                                      messages=messages)
        except Truncated as exc:
            attempts += exc.args[0]
            return {"status": "handed over", "answer": "(truncated at every budget - routed to a human)",
                    "attempts": attempts}
        attempts += turn.attempts
        rung = ladder.index(turn.attempts[-1].max_tokens)
        response = turn.response
        if response.stop_reason == "refusal":
            return {"status": "refused", "answer": "(declined - routed to a human)", "attempts": attempts}
        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            return {"status": "done", "answer": text_of(response), "attempts": attempts}
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id, "content": content, "is_error": is_error}
            for b in calls for content, is_error in [desk.run(b.name, b.input)]]})
    return {"status": "max_turns", "answer": "(turn limit - routed to a human)", "attempts": attempts}


def show(outcome: dict) -> None:
    for a in outcome["attempts"]:
        print(f"  attempt: max_tokens={a.max_tokens:>6,} streamed={a.streamed} stop_reason={a.stop_reason:<11} "
              f"output_tokens={a.output_tokens:,}")
    print(f"  status: {outcome['status']}")
    print(wrap("answer: " + outcome["answer"]))


def main() -> None:
    client = get_client()
    header("Exercise 10 - max_tokens handling with a budget ladder")

    step(1, "A normal ladder: the first budget is enough")
    show(run_agent(client, "Where is SO-10303?"))

    step(2, "A starved ladder: small budgets truncate, the last rung is streamed (and sticks)")
    # 100 and 180 are deliberately too small for thinking + a tool call; 24,000 exceeds the non-streaming limit.
    show(run_agent(client, "Where is SO-10303?", ladder=(100, 180, 24_000)))

    step(3, "An exhausted ladder: hand over rather than act on a fragment")
    show(run_agent(client, "Where is SO-10303?", ladder=(60, 90)))

    step(4, "Why not just call messages.create(max_tokens=24000)?")
    try:
        client.messages.create(model=MODEL, max_tokens=24_000, system=SYSTEM_PROMPT, tools=TOOLS,
                               messages=[{"role": "user", "content": "Where is SO-10303?"}])
        print("  accepted (the client has a custom timeout, which disables the SDK's guard)")
    except ValueError as exc:
        print(f"  the SDK refused it client-side, before sending anything: {str(exc)[:110]}...")


if __name__ == "__main__":
    main()
