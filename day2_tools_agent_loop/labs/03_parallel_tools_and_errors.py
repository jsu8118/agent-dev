"""Lab 03 - Parallel tool calls, is_error recovery, and the message-ordering rules.

Objective
    (1) Handle several tool_use blocks in ONE assistant turn - execute them (concurrently when they are
    read-only) and return every result in ONE user message.  (2) Return failures as is_error results that
    tell the model how to recover, and watch it recover.  (3) Break the protocol's ordering rules on
    purpose, catch anthropic.BadRequestError, and learn to recognise each 400 on sight.

Concepts
    Parallel tool use; disable_parallel_tool_use; parallel-safe (read) vs serial (write) tools; is_error;
    errors written for the model; tool_use / tool_result pairing rules; thinking blocks in tool loops.

Run
    python day2_tools_agent_loop/labs/03_parallel_tools_and_errors.py

What to observe
    * One response can carry two tool_use blocks. Running them concurrently cuts the wall-clock time of the
      turn (the order desk simulates a 0.5 s ERP round trip per call).
    * After an is_error result the model changes course: it switches to the tool the error names, or asks for
      a corrected ID instead of inventing one.
    * Each ordering violation fails fast with a 400 whose message names the rule you broke.
"""

# test: expect=tool_use blocks in ONE assistant turn
# test: expect=BadRequestError

from __future__ import annotations

import importlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import anthropic

from _order_desk import SYSTEM_PROMPT, TOOLS, OrderDesk
from labkit import MODEL, get_client, header, show_message, step, text_of, wrap

lab02 = importlib.import_module("02_agent_loop_from_scratch")      # reuse the loop you wrote in lab 02


def print_transcript(messages: list[dict]) -> None:
    """Show each tool call next to its result - the view you want when debugging an agent."""
    results = {}
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b["tool_use_id"]] = b
    turn = 0
    for m in messages:
        if m["role"] != "assistant":
            continue
        turn += 1
        for b in m["content"]:
            if b.type == "tool_use":
                res = results.get(b.id, {})
                flag = "is_error=True " if res.get("is_error") else ""
                content = str(res.get("content", "(no result)"))
                print(f"  turn {turn}: {b.name}({json.dumps(b.input)})\n          -> {flag}{content[:150]}"
                      + ("..." if len(content) > 150 else ""))
            elif b.type == "text" and b.text.strip():
                print(f"  turn {turn}: text: {b.text.strip()[:150]}")


def demo_parallel(client) -> list[dict]:
    desk = OrderDesk(latency_s=0.5)
    question = "Two customers are chasing us: what's the status of SO-10300 and of SO-10303?"
    messages: list[dict] = [{"role": "user", "content": question}]
    print(wrap("Staff question: " + question))
    response = client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT, tools=TOOLS,
                                      messages=messages)
    show_message(response)
    calls = [b for b in response.content if b.type == "tool_use"]
    print(f"-> {len(calls)} tool_use blocks in ONE assistant turn")

    started = time.perf_counter()
    sequential = [desk.run(b.name, b.input) for b in calls]
    t_seq = time.perf_counter() - started
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, len(calls))) as pool:
        # Read-only tools are parallel-safe. Never parallelise writes without thinking about ordering/idempotency.
        concurrent = list(pool.map(lambda b: desk.run(b.name, b.input), calls))
    t_par = time.perf_counter() - started
    print(f"executed sequentially in {t_seq:.2f}s, concurrently in {t_par:.2f}s (same results: "
          f"{sequential == concurrent})")

    results = [{"type": "tool_result", "tool_use_id": b.id, "content": content, "is_error": is_error}
               for b, (content, is_error) in zip(calls, concurrent)]
    messages += [{"role": "assistant", "content": response.content},
                 {"role": "user", "content": results}]          # ALL results, ONE message, results first
    final = client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT, tools=TOOLS,
                                   messages=messages)
    print("Final answer:\n" + wrap(text_of(final)))
    return messages


def demo_recovery(client, title: str, question: str) -> None:
    print(f"\n{title}\n" + wrap("Staff question: " + question))
    run = lab02.run_agent(client, question, OrderDesk())
    print_transcript(run.messages)
    print(f"  status={run.status}, turns={len(run.turns)}")
    print("Final answer:\n" + wrap(run.answer))


def try_request(client, label: str, messages: list[dict]) -> None:
    try:
        client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages)
        print(f"[accepted] {label}")
    except anthropic.BadRequestError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        message = (body.get("error") or {}).get("message", str(exc))
        print(f"[BadRequestError {exc.status_code}] {label}\n    {message}")


def demo_violations(client, base: list[dict]) -> None:
    """`base` = [user question, assistant turn with 2 tool_use blocks, user with both results]."""
    question, assistant, answered = base[0], base[1], base[2]
    results = answered["content"]
    blocks = assistant["content"]

    try_request(client, "correct: all results, one user message, results first", base)
    try_request(client, "text BEFORE the tool results in the user message",
                [question, assistant, {"role": "user", "content": [{"type": "text", "text": "Here you go:"},
                                                                   *results]}])
    try_request(client, "only ONE of the two results (answering tool calls one at a time)",
                [question, assistant, {"role": "user", "content": results[:1]}])
    try_request(client, "a tool_result whose tool_use_id matches no tool_use",
                [question, assistant, {"role": "user", "content": [
                    *results, {**results[0], "tool_use_id": "toolu_does_not_exist"}]}])
    if blocks and blocks[0].type == "thinking":
        rebuilt = [{"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                   for b in blocks if b.type == "tool_use"]
        try_request(client, "assistant turn rebuilt by hand WITHOUT its thinking block",
                    [question, {"role": "assistant", "content": rebuilt}, answered])
    else:
        print("[skipped] this response had no thinking block (adaptive thinking may skip easy turns), so there is "
              "nothing to strip")
    try_request(client, "text AFTER the tool results (allowed: results first, then any text)",
                [question, assistant, {"role": "user", "content": [
                    *results, {"type": "text", "text": "Reminder: answer in two sentences."}]}])


def main() -> None:
    client = get_client()
    header(f"Lab 03 - parallel tools, is_error recovery, ordering rules ({MODEL})")

    step(1, "Parallel tool calls: several tool_use blocks in one turn")
    base = demo_parallel(client)

    step(2, "is_error recovery")
    demo_recovery(client, "(a) the wrong kind of ID - the error names the right tool",
                  "The customer says their order AR-90263 hasn't arrived. Where is it?")
    demo_recovery(client, "(b) an ID that doesn't exist - the model must not invent another one",
                  "What's the status of SO-10999?")

    step(3, "Breaking the ordering rules on purpose")
    demo_violations(client, base)


if __name__ == "__main__":
    main()
