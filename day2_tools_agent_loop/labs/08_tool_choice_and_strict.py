"""Lab 08 - Steering tool use: tool_choice, parallel control, strict schemas, and model differences.

Objective
    See what each tool_choice mode does (auto, auto without parallel calls, none, a forced tool), learn
    the classic forced-choice bug (forcing on EVERY turn means the loop can never finish), watch Claude
    Opus 5.5 reject forced tool choice with a 400, and apply the portable replacement: auto + a clear
    instruction + strict tools + a check that the call happened.  Finish with structured outputs, the
    right tool when a "forced tool call" only existed to extract JSON.

Concepts
    tool_choice auto / any / tool / none; disable_parallel_tool_use; strict tool use and
    additionalProperties:false; per-model request surfaces (labkit.get_spec); structured outputs.

Run
    python day2_tools_agent_loop/labs/08_tool_choice_and_strict.py

What to observe
    * disable_parallel_tool_use turns one parallel turn into several sequential turns: more requests, more
      re-sent history, same answer.
    * A permanently forced tool_choice never lets the model answer; force the FIRST turn only.
    * On claude-opus-5-5 the forced request fails before any tokens are generated (no cost); the
      auto + instruction + strict version works on every model.
"""

# test: expect=disable_parallel_tool_use
# test: expect=never finished
# test: expect=parsed_output

from __future__ import annotations

import json
from typing import Callable, Literal

import anthropic
from pydantic import BaseModel

from _order_desk import SYSTEM_PROMPT, TOOLS, OrderDesk
from labkit import MODEL, get_client, get_spec, header, step, text_of, wrap

NO_FORCING_MODEL = "claude-opus-5-5"          # rejects tool_choice any/tool (so do claude-fable-5-1, claude-mythos-5-1)
FORCE_ORDER_TOOL = {"type": "tool", "name": "get_order_status"}


def calls_of(response) -> list[str]:
    return [f"{b.name}({b.input.get('order_id') or b.input.get('query') or ''})"
            for b in response.content if b.type == "tool_use"]


def run_loop(client, question: str, choice_for_turn: Callable[[int], dict | None], *, model: str = MODEL,
             max_turns: int = 4) -> tuple[bool, int, int]:
    """A minimal loop that lets each turn use a different tool_choice. Returns (finished, turns, input tokens).

    (No max_tokens/refusal handling - lab 02 has the full stop-reason policy.)
    """
    desk, messages, total_in = OrderDesk(), [{"role": "user", "content": question}], 0
    for turn in range(1, max_turns + 1):
        choice = choice_for_turn(turn)
        extra = {"tool_choice": choice} if choice else {}
        response = client.messages.create(model=model, max_tokens=4000, system=SYSTEM_PROMPT, tools=TOOLS,
                                          messages=messages, **extra)
        total_in += response.usage.input_tokens
        print(f"  turn {turn}: tool_choice={json.dumps(choice) if choice else 'auto (default)'} "
              f"stop_reason={response.stop_reason} calls={calls_of(response) or '-'}")
        messages.append({"role": "assistant", "content": response.content})
        uses = [b for b in response.content if b.type == "tool_use"]
        if not uses:
            print("  answer: " + text_of(response)[:160])
            return True, turn, total_in
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id, "content": desk.run(b.name, b.input)[0]} for b in uses]})
    return False, max_turns, total_in


def call_order_tool_portably(client, model: str, question: str, retries: int = 1):
    """What replaces tool_choice={"type": "tool"} on models that reject forcing."""
    strict_tools = [{**t, "strict": True} for t in TOOLS]           # schema-valid arguments, as forcing gave you
    content = question + "\n\nUse the get_order_status tool to answer."
    for attempt in range(retries + 1):
        response = client.messages.create(model=model, max_tokens=4000, system=SYSTEM_PROMPT, tools=strict_tools,
                                          tool_choice={"type": "auto"},
                                          messages=[{"role": "user", "content": content}])
        if any(b.type == "tool_use" and b.name == "get_order_status" for b in response.content):
            return response, attempt
        content = question + "\n\nYou must call get_order_status before answering."   # auto guarantees nothing
    return response, retries


class TicketRefs(BaseModel):
    order_ids: list[str]
    invoice_ids: list[str]
    intent: Literal["order_status", "billing", "return", "other"]


def main() -> None:
    client = get_client()
    header(f"Lab 08 - tool_choice, strict tools and model differences ({MODEL})")
    two_orders = "What's the status of SO-10300 and SO-10303?"

    step(1, 'tool_choice {"type": "auto"} (the default): the model decides, parallel calls allowed')
    finished, turns, tokens_parallel = run_loop(client, two_orders, lambda t: None)
    print(f"  -> finished={finished} in {turns} turns, {tokens_parallel:,} input tokens")

    step(2, "auto + disable_parallel_tool_use: at most one tool call per turn")
    sequential = {"type": "auto", "disable_parallel_tool_use": True}
    finished, turns, tokens_seq = run_loop(client, two_orders, lambda t: sequential)
    print(f"  -> finished={finished} in {turns} turns, {tokens_seq:,} input tokens "
          f"(x{tokens_seq / max(tokens_parallel, 1):.2f} vs parallel). Use it when tools must run in order or your "
          "executor can't handle concurrency - not by default.")

    step(3, 'tool_choice {"type": "none"}: tools stay in the prompt but cannot be called')
    response = client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT, tools=TOOLS,
                                      tool_choice={"type": "none"},
                                      messages=[{"role": "user", "content": "Where is SO-10303?"}])
    print(f"  stop_reason={response.stop_reason}, tool calls={calls_of(response) or 'none'}")
    print(wrap("answer: " + text_of(response)))
    print("  Useful for a final 'summarise, don't act' turn without changing `tools` (which would break the cache).")

    step(4, "Forcing a tool: only on the first turn")
    print("Forced on EVERY turn (a common bug):")
    finished, turns, _ = run_loop(client, "Where is SO-10303?", lambda t: FORCE_ORDER_TOOL)
    print(f"  -> never finished: {not finished} (stopped by the {turns}-turn guard). Every response MUST contain a "
          "tool call, so the model can never answer.")
    print("Forced on turn 1 only, auto afterwards:")
    finished, turns, _ = run_loop(client, "Where is SO-10303?", lambda t: FORCE_ORDER_TOOL if t == 1 else None)
    print(f"  -> finished={finished} in {turns} turns")

    step(5, f"The same forced request on {NO_FORCING_MODEL}")
    spec = get_spec(NO_FORCING_MODEL)
    print(f"  catalog: {spec.display_name} forced_tool_choice={spec.forced_tool_choice}; "
          f"{MODEL}: forced_tool_choice={get_spec(MODEL).forced_tool_choice}")
    portable_model = NO_FORCING_MODEL
    try:
        client.messages.create(model=NO_FORCING_MODEL, max_tokens=4000, system=SYSTEM_PROMPT, tools=TOOLS,
                               tool_choice=FORCE_ORDER_TOOL,
                               messages=[{"role": "user", "content": "Where is SO-10303?"}])
        print("  accepted (unexpected for this model - check the current model documentation)")
    except anthropic.BadRequestError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        print(f"  BadRequestError {exc.status_code}: {(body.get('error') or {}).get('message', exc)}")
    except (anthropic.NotFoundError, anthropic.PermissionDeniedError) as exc:
        print(f"  {type(exc).__name__}: {NO_FORCING_MODEL} is not available to your organisation; the portable "
              f"pattern below runs on {MODEL} instead.")
        portable_model = MODEL

    step(6, "The portable replacement: auto + instruction + strict + verify the call happened")
    response, retries = call_order_tool_portably(client, portable_model, "Where is SO-10303?")
    print(f"  model={portable_model} calls={calls_of(response)} retries needed={retries}")
    bad = {**TOOLS[0], "strict": True, "input_schema": {k: v for k, v in TOOLS[0]["input_schema"].items()
                                                         if k != "additionalProperties"}}
    try:
        client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT, tools=[bad],
                               messages=[{"role": "user", "content": "Where is SO-10303?"}])
        print("  a strict tool without additionalProperties:false was accepted")
    except anthropic.BadRequestError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        print(f"  strict needs a closed schema -> BadRequestError: {(body.get('error') or {}).get('message', exc)}")

    step(7, "When the forced call only existed to get JSON: structured outputs")
    parsed = client.messages.parse(
        model=MODEL, max_tokens=2000, output_format=TicketRefs,
        system="<day2_extract>\nExtract the Kestrel order IDs (SO-12345), invoice IDs (AR-12345) and the intent.",
        messages=[{"role": "user", "content": "Hi, invoice AR-90263 for SO-10279 shows as unpaid but we paid it "
                                              "last week - can you check?"}])
    print(f"  parsed_output = {parsed.parsed_output!r}")
    print("  No tool, no loop, no forcing: output_config.format guarantees schema-valid JSON on every model that "
          "supports structured outputs.")


if __name__ == "__main__":
    main()
