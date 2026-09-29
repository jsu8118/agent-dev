"""Exercise 10 (starter) - A prefix-diff tool that predicts binding breaks before a request is sent.

Your harness knows everything the API checks: the request it sent last, the request it is about to send, and which
model produced each assistant turn (store it when you append the turn - the signature is opaque). Implement
`predict()` so that, before sending, it says for every thinking block in the next request whether the API will keep
it, drop it (and why), list it as allowed, or reject the request. The script then sends each case with the
thinking-binding-controls beta and checks your predictions against `input_transformations` (or the 400).

Run
    python advanced/day3_long_horizon_context/exercises/ex10_prefix_diff.py

TODO
    1. divergence(prev, nxt): the first message index from which `nxt` no longer extends `prev` append-only
       (0 when the system prompt or the tool set changed; None when nothing before the new turn changed).
    2. predict(prev, nxt, model, producers, behaviour): one dict per thinking block in `nxt`:
       {"path": "messages.I.content.J", "outcome": "kept" | "dropped" | "allowed" | "rejected",
        "reason": "prefix_binding_mismatch" | "model_binding_mismatch" | ""}.
       Rules: the model check runs first (labkit.models.can_read_thinking); the prefix check applies only to blocks
       from a model that binds to the prefix, read by a model that checks; "enforced" rejects unless drop_block,
       "recorded" enforces only when prefix_mismatch_behavior is set (unset + beta header = "allowed"); with
       drop_block, the first mismatched block and every thinking block after it are dropped.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import anthropic  # noqa: E402

from labkit import get_client  # noqa: E402
from labkit.models import can_read_thinking, get_spec  # noqa: E402,F401  (can_read_thinking: for predict)

import _day3 as d3  # noqa: E402

BETA = "thinking-binding-controls-2026-08-01"
FABLE, OPUS55, OPUS5 = "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5"
NEXT = {"role": "user", "content": "One more: what do I check before I leave plant room B2?"}


def binding(model: str) -> str:
    """What the model catalog says about the prefix check: "enforced", "recorded" or "none"."""
    return get_spec(model).thinking_prefix_binding


def canonical(value) -> str:
    """Bytes as the check sees them: cache_control stripped at every level, key order irrelevant."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k != "cache_control"}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return json.dumps(strip(value), sort_keys=True, ensure_ascii=False)


def thinking_paths(body: dict) -> list[tuple[int, int]]:
    return [(i, j) for i, m in enumerate(body["messages"]) if m["role"] == "assistant" and isinstance(m["content"], list)
            for j, b in enumerate(m["content"]) if b.get("type") == "thinking"]


def divergence(prev: dict, nxt: dict) -> int | None:
    """TODO 1 - where does `nxt` stop extending `prev` append-only?"""
    raise NotImplementedError("divergence")


def predict(prev: dict, nxt: dict, model: str, producers: dict[int, str], behaviour: str | None) -> list[dict]:
    """TODO 2 - the API's verdict for every thinking block in `nxt`, before sending it."""
    raise NotImplementedError("predict")


# ------------------------------------------------------------------------------------------------ the test bench
def conversation(client, model: str) -> tuple[dict, dict, dict[int, str]]:
    """Three look-ups at Westfield on `model`: (params, last request body, producing model per assistant message)."""
    params = dict(model=model, max_tokens=4000, tools=d3.FIELD_TOOLS, system=d3.FIELD_SYSTEM)
    bodies: list[dict] = []

    def create(**kw):
        bodies.append(d3.as_body(kw["messages"], system=kw["system"], tools=kw["tools"]))
        return client.messages.create(**kw)
    day = d3.run_day(create, params, steps=[s for s in d3.SCRIPT if s.number in (35, 36, 37)])
    history = d3.as_body(day.messages)["messages"]
    producers = {i: model for i, m in enumerate(history) if m["role"] == "assistant"}
    last = {**bodies[-1]}
    return params, {"system": params["system"], "tools": params["tools"], "messages": history, "_last": last}, producers


def cases(client) -> list[tuple]:
    out = []
    for model in (FABLE, OPUS55):
        params, conv, producers = conversation(client, model)
        prev = conv["_last"]
        base = {"system": conv["system"], "tools": conv["tools"], "messages": conv["messages"]}

        def variant(edit=None, **changes):
            body = copy.deepcopy(base)
            body.update(changes)
            if edit:
                edit(body)
            body["messages"] = body["messages"] + [NEXT]
            return body

        def reword(body):
            body["messages"][0] = {"role": "user", "content": body["messages"][0]["content"] + " Thanks."}

        def trim(body):
            for b in body["messages"][2]["content"]:
                if b.get("type") == "tool_result":
                    b["content"] = "[trimmed]"
        short = model.replace("claude-", "")
        out += [(f"{short}: append-only", prev, variant(), model, producers, "error"),
                (f"{short}: reword turn 1", prev, variant(reword), model, producers, "drop_block"),
                (f"{short}: trim a tool result", prev, variant(trim), model, producers, "error"),
                (f"{short}: re-render system", prev, variant(system=base["system"] + "\nLocal time: 11:05."), model,
                 producers, None),
                (f"{short}: reorder tools", prev, variant(tools=list(reversed(base["tools"]))), model, producers,
                 "error")]
        if model == OPUS55:
            out += [(f"{short} -> opus-5", prev, variant(), OPUS5, producers, "drop_block"),
                    (f"{short} -> fable-5-1", prev, variant(), FABLE, producers, "error")]
        else:
            out += [(f"{short} -> opus-5 with an edit", prev, variant(reword), OPUS5, producers, "error")]
    return out


def api_verdict(client, body: dict, model: str, behaviour: str | None) -> dict[str, str]:
    kw = dict(model=model, max_tokens=2000, system=body["system"], tools=body["tools"], messages=body["messages"],
              betas=[BETA])
    if behaviour:
        kw["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": behaviour}}
    try:
        r = client.beta.messages.create(**kw)
    except anthropic.BadRequestError as exc:
        return {d3.api_error(exc).split(":")[0]: "rejected"}
    return {t.path: {"thinking_dropped": "dropped", "thinking_mismatch_allowed": "allowed"}.get(t.type, t.type)
            for t in r.input_transformations}


def summarise(predicted: list[dict]) -> dict[str, str]:
    """Predictions in the API's shape: only the blocks it would report (kept blocks are not listed); a rejection is
    reported at the first failing block."""
    rejected = [p for p in predicted if p["outcome"] == "rejected"]
    if rejected:
        return {rejected[0]["path"]: "rejected"}
    return {p["path"]: p["outcome"] for p in predicted if p["outcome"] in ("dropped", "allowed")}


def main() -> None:
    client = get_client()
    print("Exercise 10 - predict binding breaks before sending")
    rows, todo = [], False
    for name, prev, nxt, model, producers, behaviour in cases(client):
        api = api_verdict(client, nxt, model, behaviour)
        try:
            mine = summarise(predict(prev, nxt, model, producers, behaviour))
            verdict = "match" if mine == api else "MISMATCH"
            shown = ", ".join(sorted(set(mine.values()))) + f" x{len(mine)}" if mine else "all kept"
        except NotImplementedError as exc:
            todo, verdict, shown = True, "TODO", f"implement {exc}()"
        api_shown = ", ".join(sorted(set(api.values()))) + f" x{len(api)}" if api else "all kept"
        rows.append([name, behaviour or "(unset)", shown, api_shown, verdict])
    d3.table(rows, ["case", "prefix_mismatch_behavior", "predicted", "API said", "check"])
    if todo:
        print("TODO: implement divergence() and predict(), then re-run until every case says 'match'.")


if __name__ == "__main__":
    main()
