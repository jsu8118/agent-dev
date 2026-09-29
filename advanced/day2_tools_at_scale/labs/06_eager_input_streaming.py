"""Lab 06 - Eager input streaming: stream a long tool input, and validate it before you act on it.

Objective
    Ask the copilot to draft technical safety bulletin TSB-2026-09 into a client tool whose `body` is a long document,
    with `eager_input_streaming: true` on the tool. Watch the `input_json_delta` fragments arrive, then do the client's
    duties: check the stop reason, parse, validate against the tool's schema and check the content before running the
    tool. Replay the same fragments through the SDK's tolerant parser to see what a cut or malformed stream looks
    like, handle a truncated turn the documented way, and answer an input you reject with an INVALID_JSON error result.

Concepts
    eager_input_streaming (no beta header), content_block_start / input_json_delta / content_block_stop, the SDK's
    input_json events and snapshots, the tolerant partial-JSON parser (silently truncated objects), stop_reason checks
    before tools run (max_tokens, refusal), strict parse + JSON Schema validation + semantic checks, the INVALID_JSON
    tool_result, re-issuing when the SDK raises, what eager streaming changes and what it does not

Run
    python advanced/day2_tools_at_scale/labs/06_eager_input_streaming.py

What to observe
    * Dozens of input_json events for one tool_use block, and the snapshot growing field by field.
    * The replay table: a cut stream parses into a schema-valid object missing its last action - parse success is not
      a signal; the stop reason is.
    * The truncated turn is dropped and retried with a bigger max_tokens - never executed.
    * A rejected input goes back as {"INVALID_JSON": ...} with is_error, and the model sends the draft again.
"""
# test: expect=INVALID_JSON
# test: expect=schema-valid

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import jiter            # the partial-JSON parser the Python SDK uses for streamed tool input (installed with anthropic)
import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step, text_of, wrap  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = (d2.MARK_STREAM + "\nYou are Kestrel's quality copilot. When asked for a bulletin, write it in full into the "
          "draft_bulletin tool; never paste it into the chat.")
TOOL = {**{k: v for k, v in d2.EXTRA_TOOLS["draft_bulletin"].items() if k != "meta"}, "eager_input_streaming": True}
SCHEMA = TOOL["input_schema"]


class Streamed:
    """One streamed response: the final message plus what arrived on the wire for each tool_use block."""

    def __init__(self) -> None:
        self.message = None
        self.fragments: dict[str, list[str]] = {}      # tool_use id -> raw partial_json fragments, in order
        self.snapshots: dict[str, list[object]] = {}
        self.first_fragment_s: float | None = None


def stream_turn(client, messages: list[dict], *, max_tokens: int = 8000, tools: list[dict] | None = None,
                show: bool = False) -> Streamed:
    out, current, started = Streamed(), None, time.monotonic()
    with client.messages.stream(model=MODEL, max_tokens=max_tokens, system=SYSTEM, tools=tools or [TOOL],
                                messages=messages) as stream:
        for event in stream:
            if event.type == "content_block_start" and event.content_block.type == "tool_use":
                current = event.content_block.id
                out.fragments[current], out.snapshots[current] = [], []
                if show:
                    print(f"  content_block_start  tool_use {event.content_block.name} id={current}")
            elif event.type == "input_json" and current:
                if out.first_fragment_s is None:
                    out.first_fragment_s = time.monotonic() - started
                out.fragments[current].append(event.partial_json)
                out.snapshots[current].append(event.snapshot)
                n = len(out.fragments[current])
                if show and (n <= 3 or n % 25 == 0):
                    snap = event.snapshot if isinstance(event.snapshot, dict) else {}
                    body = len(snap.get("body", "")) if isinstance(snap.get("body"), str) else 0
                    print(f"  input_json #{n:<3} +{len(event.partial_json):>2} chars {json.dumps(event.partial_json)[:44]:<46} "
                          f"snapshot keys={list(snap)} body={body} chars")
            elif event.type == "content_block_stop" and current and show:
                print(f"  content_block_stop   after {len(out.fragments[current])} input_json events, "
                      f"{sum(map(len, out.fragments[current])):,} characters")
                current = None
        out.message = stream.get_final_message()
    if show:
        print(f"  message_delta        stop_reason={out.message.stop_reason}")
    return out


def check_input(block, raw: str, request: str) -> list[str]:
    """The client's validation duties for one tool_use block, in order. Returns the problems (empty = safe to run)."""
    try:
        parsed = json.loads(raw)                       # strict: the accumulated fragments must be one JSON document
    except json.JSONDecodeError as exc:
        return [f"not valid JSON ({exc.msg} at char {exc.pos})"]
    problems = [f"schema: {e.message}" for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(parsed)]
    if parsed != block.input:
        problems.append("the SDK's parsed input differs from the raw fragments")
    for lot in (lot for lot in ("PS-2608-B", "VD-2607-C") if lot in request):
        if isinstance(parsed.get("body"), str) and lot not in parsed["body"]:
            problems.append(f"content: the body never mentions lot {lot}")
    return problems


def tolerant(raw: str) -> object:
    try:
        return jiter.from_json(raw.encode(), partial_mode=True)
    except ValueError as exc:
        return f"ValueError: {str(exc)[:40]}"


def verdict(value: object) -> str:
    if not isinstance(value, dict):
        return "-"
    errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(value))
    return "schema-valid" if not errors else f"invalid: {errors[0].message[:48]}"


def step_replay(fragments: list[str]) -> None:
    raw = "".join(fragments)
    body_start = raw.index('"body"')
    actions_start = raw.index('"actions"')
    last_action = raw.rindex('", "') + 1
    cases = [("the full stream", raw),
             ("cut inside `body`", raw[: body_start + 400]),
             ("cut after the last complete action", raw[:last_action]),
             ("cut inside `actions`, mid-string", raw[: last_action + 12]),
             ("a missing comma between two fields", raw[:actions_start].rstrip().rstrip(",") + " " + raw[actions_start:])]
    rows = []
    for label, text in cases:
        value = tolerant(text)
        keys = f"{len(value)} keys, {len(value.get('actions', []))} actions" if isinstance(value, dict) else str(value)
        try:
            json.loads(text)
            strict = "ok"
        except json.JSONDecodeError as exc:
            strict = f"error: {exc.msg}"
        rows.append([label, len(text), keys, verdict(value), strict])
    d2.table(rows, ["what arrived", "chars", "tolerant parse (what the SDK hands you)", "schema", "strict json.loads"])
    print("\nA stream cut after a complete array element parses into a schema-valid object that silently lost the rest,\n"
          "and a missing comma makes the tolerant parser drop every field after it without raising. Parse success is not\n"
          "the signal: check stop_reason first (max_tokens, refusal), then validate strictly, then run the tool.")


def main() -> None:
    client = get_client()
    header("Lab 06 - Eager input streaming")
    if is_mock():
        print("[mock] the mock streams every tool input in 32-character input_json_delta fragments, instantly, whether or not\n"
              "       eager_input_streaming is set; what the flag changes live (no per-parameter buffering or validation, the\n"
              "       first fragment arrives at once) is measured in step 7 in live mode only.")
    request = d2.bulletin_request()

    step(1, "The tool and the request")
    print(f"  tool {TOOL['name']}: eager_input_streaming={TOOL['eager_input_streaming']}, required {SCHEMA['required']}")
    print(f"  body: {SCHEMA['properties']['body']['minLength']}-{SCHEMA['properties']['body']['maxLength']} characters; "
          f"lots: pattern {SCHEMA['properties']['lots']['items']['pattern']}; actions: at least "
          f"{SCHEMA['properties']['actions']['minItems']}")
    print(wrap(request, "  | "))

    step(2, "Stream it and watch the fragments arrive")
    messages = [{"role": "user", "content": request}]
    first = stream_turn(client, messages, show=True)
    block = next(b for b in first.message.content if b.type == "tool_use")
    raw = "".join(first.fragments[block.id])

    step(3, "The client's duties before running the tool")
    print(f"  1. stop_reason = {first.message.stop_reason!r}: a tool_use turn, not max_tokens or refusal -> may proceed")
    problems = check_input(block, raw, request)
    print(f"  2. strict parse of the {len(raw):,} raw characters: {'ok' if not problems or 'JSON' not in problems[0] else problems[0]}")
    print(f"  3. JSON Schema (Draft 2020-12) + content checks: {'; '.join(problems) if problems else 'schema-valid, every lot named'}")
    ops = d2.KestrelOps()
    content, is_error = ops.run(block.name, block.input)
    print(f"  4. run the tool -> {content}")
    messages += [{"role": "assistant", "content": first.message.content},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id, "content": content}]}]
    done = stream_turn(client, messages)
    print("  answer: " + text_of(done.message))

    step(4, "Replay: what the tolerant parser makes of a cut or broken stream")
    step_replay(first.fragments[block.id])

    step(5, "A truncated turn for real: max_tokens too small")
    messages = [{"role": "user", "content": request}]
    budget, attempt = 600, 1
    while True:
        s = stream_turn(client, messages, max_tokens=budget)
        uses = [b for b in s.message.content if b.type == "tool_use"]
        print(f"  attempt {attempt}: max_tokens={budget:,} stop_reason={s.message.stop_reason} tool_use blocks={len(uses)}"
              + (f" input keys={list(uses[0].input)}" if uses else ""))
        if s.message.stop_reason == "max_tokens" and uses and budget < 16_000:
            print("     -> truncated tool input: NOT executed, NOT appended; retry the turn with a larger budget")
            budget, attempt = min(budget * 4, 16_000), attempt + 1
            continue
        break
    print(f"  attempt {attempt} is a complete tool_use turn: validate it as in step 3, then run it.")
    if is_mock():
        print("  [mock] the mock empties a truncated tool input ({}); live, the SDK hands you whatever its tolerant parser made\n"
              "  of the fragments that arrived - often a schema-valid object, as step 4 showed. The stop reason decides.")

    step(6, "Rejecting an input: the INVALID_JSON error result (fault injection)")
    messages = [{"role": "user", "content": request}]
    s = stream_turn(client, messages)
    block = next(b for b in s.message.content if b.type == "tool_use")
    raw = "".join(s.fragments[block.id])
    broken = raw.replace('", "actions"', '" "actions"', 1)      # [fault injection] one comma lost on the wire
    print("  [fault injection] the lab drops one comma from the received fragments, as a malformed emission would.")
    problems = check_input(block, broken, request)
    print(f"  validation: {problems[0]}")
    error = {"type": "tool_result", "tool_use_id": block.id, "is_error": True, "content": json.dumps({"INVALID_JSON": broken})}
    print(f"  tool_result sent back: is_error=True, content={error['content'][:70]}...")
    messages += [{"role": "assistant", "content": s.message.content}, {"role": "user", "content": [error]}]
    retry = stream_turn(client, messages)
    again = [b for b in retry.message.content if b.type == "tool_use"]
    if again:
        raw2 = "".join(retry.fragments[again[0].id])
        print(f"  the model sent the draft again: {len(raw2):,} characters, "
              f"validation: {'; '.join(check_input(again[0], raw2, request)) or 'schema-valid, every lot named'}")
        content, _ = ops.run(again[0].name, again[0].input)
        messages += [{"role": "assistant", "content": retry.message.content},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": again[0].id, "content": content}]}]
        print("  answer: " + text_of(stream_turn(client, messages).message))
    print("  Had the SDK raised ValueError while streaming (JSON it could not parse at all), there would be no tool_use id\n"
          "  to answer: re-issue the request instead, with a retry cap, and let typed API errors propagate.")

    step(7, "What the flag changes: time to the first fragment (live only)")
    if is_mock():
        print("  [mock] skipped - the mock has no generation latency. Live, the lab streams the same request with and without\n"
              "  eager_input_streaming and prints the seconds to the first input_json event of each.")
    else:
        buffered = {k: v for k, v in TOOL.items() if k != "eager_input_streaming"}
        for label, tool in (("eager_input_streaming: true", TOOL), ("default (buffered per parameter)", buffered)):
            s = stream_turn(client, [{"role": "user", "content": request}], tools=[tool])
            print(f"  {label:<34} first fragment after {s.first_fragment_s or 0:.1f} s")


if __name__ == "__main__":
    main()
