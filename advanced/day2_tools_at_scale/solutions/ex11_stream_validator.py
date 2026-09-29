"""Solution to exercise 11 - one function that decides what to do with a streamed tool call.

Objective
    Implement `decide()` for eager-streamed tool input: check what the SDK and the stop reason say first, then parse
    the raw fragments strictly, validate against the tool's schema, run the content checks, and return the action the
    loop must take - run, retry with a bigger max_tokens, stop, answer with an INVALID_JSON error result, or re-issue.

Concepts
    order of checks (SDK error -> refusal -> max_tokens -> strict parse -> schema -> content), why a tolerant parse is
    not a validity signal, the INVALID_JSON tool_result, re-issuing when there is no tool_use id to answer

Run
    python advanced/day2_tools_at_scale/solutions/ex11_stream_validator.py

What to observe
    * Eight cases built from a real streamed draft, each mapped to the documented action.
    * The case the schema cannot catch (a body that never names a requested lot) needs the content check.
"""
# test: expect=8/8

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))

from labkit import get_client, header, step  # noqa: E402

import ex11_stream_validator as ex  # noqa: E402

d2 = ex.d2
LOT = re.compile(r"\b[A-Z]{2}-\d{4}-[A-Z]\b")


def decide(stop_reason: str | None, raw: str, sdk_raised: bool, schema: dict, request: str) -> tuple[str, str]:
    if sdk_raised:
        return "reissue", "the SDK raised while streaming: no tool_use id to answer, send the request again (capped)"
    if stop_reason == "refusal":
        return "stop", "a refusal can cut a tool_use mid-input: run nothing"
    if stop_reason == "max_tokens":
        return "retry_bigger", "truncated turn: drop it, never execute it, retry with a larger max_tokens"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return "invalid_json", f"not one JSON document ({exc.msg}); send {{'INVALID_JSON': raw}} with is_error"
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(value))
    if errors:
        return "invalid_json", f"schema: {errors[0].message[:60]}"
    missing = [lot for lot in dict.fromkeys(LOT.findall(request)) if lot not in value.get("body", "")]
    if missing:
        return "invalid_json", f"content: the body never names {', '.join(missing)}"
    return "run", "complete, strict JSON, schema-valid, every requested lot named"


def main() -> None:
    client = get_client()
    header("Exercise 11 (solution) - a streaming validator")
    fragments = ex.record_fragments(client)
    step(1, f"Recorded {len(fragments)} fragments ({sum(map(len, fragments)):,} characters) from one streamed draft")
    step(2, "decide() on eight cases")
    passed = 0
    for case in ex.cases(fragments):
        action, reason = decide(case["stop"], case["raw"], case["raised"], ex.SCHEMA, d2.bulletin_request())
        ok = action == case["expect"]
        passed += ok
        print(f"  {case['name']:<36} -> {action:<13} {'ok' if ok else 'WRONG'}: {reason}")
    print(f"\n  {passed}/{len(ex.cases(fragments))} cases decided as documented.")
    print("  The order matters: the stop reason outranks a successful parse (a cut stream often parses), and the schema\n"
          "  cannot know that a body about PS-2608-B must name it - content checks are yours too.")


if __name__ == "__main__":
    main()
