"""Exercise 11 (starter) - one function that decides what to do with a streamed tool call.

With eager_input_streaming the API no longer validates tool input; your client does. Write `decide()`: given the stop
reason, the raw input_json_delta fragments of one tool_use block, whether the SDK raised while streaming, the tool's
schema and the request, return what the loop must do:

    "run"             - the input is complete, valid JSON, schema-valid and passes the content checks;
    "retry_bigger"    - the turn stopped at max_tokens with a tool_use in it: drop the turn, retry with more max_tokens;
    "stop"            - the turn is a refusal: run nothing;
    "invalid_json"    - you hold the tool_use block but the input fails: answer with an is_error tool_result
                        {"INVALID_JSON": <raw>} so the model can send it again;
    "reissue"         - the SDK raised ValueError while streaming: there is no tool_use id to answer; re-issue the request.

Run
    python advanced/day2_tools_at_scale/exercises/ex11_stream_validator.py

The starter records a real streamed draft from the API, builds eight cases from it and prints TODO until `decide()`
returns the expected action for every case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header, step  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = (d2.MARK_STREAM + "\nYou are Kestrel's quality copilot. When asked for a bulletin, write it in full into the "
          "draft_bulletin tool; never paste it into the chat.")
TOOL = {**{k: v for k, v in d2.EXTRA_TOOLS["draft_bulletin"].items() if k != "meta"}, "eager_input_streaming": True}
SCHEMA = TOOL["input_schema"]


def record_fragments(client) -> list[str]:
    """Stream one draft and keep the raw partial_json fragments of its tool_use block."""
    fragments: list[str] = []
    with client.messages.stream(model=MODEL, max_tokens=8000, system=SYSTEM, tools=[TOOL],
                                messages=[{"role": "user", "content": d2.bulletin_request()}]) as stream:
        for event in stream:
            if event.type == "input_json":
                fragments.append(event.partial_json)
        stream.get_final_message()
    return fragments


def cases(fragments: list[str]) -> list[dict]:
    raw = "".join(fragments)
    cut_body = raw[: raw.index('"body"') + 300]
    cut_action = raw[: raw.rindex('", "') + 1]
    no_comma = raw.replace('", "actions"', '" "actions"', 1)
    parsed = json.loads(raw)
    wrong_lot = json.dumps({**parsed, "lots": [lot.rsplit("-", 1)[0] for lot in parsed["lots"]]})
    vague_body = json.dumps({**parsed, "body": parsed["body"].replace("PS-2608-B", "the seal lot")})
    return [
        {"name": "complete and valid", "stop": "tool_use", "raw": raw, "raised": False, "expect": "run"},
        {"name": "cut inside body at max_tokens", "stop": "max_tokens", "raw": cut_body, "raised": False, "expect": "retry_bigger"},
        {"name": "cut after an action at max_tokens", "stop": "max_tokens", "raw": cut_action, "raised": False, "expect": "retry_bigger"},
        {"name": "refusal mid-input", "stop": "refusal", "raw": cut_body, "raised": False, "expect": "stop"},
        {"name": "missing comma", "stop": "tool_use", "raw": no_comma, "raised": False, "expect": "invalid_json"},
        {"name": "lot id breaks the schema pattern", "stop": "tool_use", "raw": wrong_lot, "raised": False, "expect": "invalid_json"},
        {"name": "body never names a requested lot", "stop": "tool_use", "raw": vague_body, "raised": False,
         "expect": "invalid_json"},
        {"name": "SDK raised ValueError", "stop": None, "raw": raw[:40] + "}{", "raised": True, "expect": "reissue"},
    ]


def decide(stop_reason: str | None, raw: str, sdk_raised: bool, schema: dict, request: str) -> tuple[str, str]:
    """Return (action, reason)."""
    raise NotImplementedError


def main() -> None:
    client = get_client()
    header("Exercise 11 - a streaming validator")
    fragments = record_fragments(client)
    step(1, f"Recorded {len(fragments)} fragments ({sum(map(len, fragments)):,} characters) from one streamed draft")
    step(2, "Your decide()")
    for case in cases(fragments):
        try:
            action, reason = decide(case["stop"], case["raw"], case["raised"], SCHEMA, d2.bulletin_request())
            mark = "ok" if action == case["expect"] else f"WRONG (expected {case['expect']})"
            print(f"  {case['name']:<36} -> {action:<13} {mark}: {reason}")
        except NotImplementedError:
            print(f"  {case['name']:<36} -> TODO (expected {case['expect']})")


if __name__ == "__main__":
    main()
