"""Solution to exercise 10 - predict binding breaks before a request is sent, and check against the API.

Objective
    Implement the starter's divergence() and predict() and run its test bench: thirteen cases on Claude Fable 5.1 and
    Claude Opus 5.5 - append-only, a reworded turn, a trimmed tool result, a re-rendered system prompt, reordered tools,
    and model switches - each predicted before sending and compared with the API's input_transformations or 400.

Concepts
    the prefix a thinking block is bound to (system, tool set as a name-sorted set, earlier messages); the model check
    before the prefix check; enforced vs recorded vs none; drop_block drops the first mismatch and everything after it;
    a harness must record which model produced each turn.

Run
    python advanced/day3_long_horizon_context/solutions/ex10_prefix_diff.py

What to observe
    * Every case says 'match': the harness can know, before sending, what the API will do.
    * Reordering tools is not a divergence (the set is compared, not the order); re-rendering the system prompt is.
    * The downgrade case: the model check drops every Fable 5.1 block, so the edit is never prefix-judged.
"""
# test: expect=13/13 predictions match

from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ex10_starter", HERE.parents[0] / "exercises" / "ex10_prefix_diff.py")
starter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(starter)

from labkit import get_client  # noqa: E402
from labkit.models import can_read_thinking  # noqa: E402


def tool_set(body: dict) -> list[str]:
    """Tools as the check sees them: non-deferred definitions, compared as a name-sorted set (order is harmless)."""
    return sorted(starter.canonical(t) for t in body.get("tools") or [] if not t.get("defer_loading"))


def divergence(prev: dict, nxt: dict) -> int | None:
    if starter.canonical(prev.get("system") or "") != starter.canonical(nxt.get("system") or "") \
            or tool_set(prev) != tool_set(nxt):
        return 0                                       # everything after the system prompt / tools is invalidated
    pm, nm = prev["messages"], nxt["messages"]
    for i in range(min(len(pm), len(nm))):
        if starter.canonical(pm[i]) != starter.canonical(nm[i]):
            return i
    return 0 if len(nm) < len(pm) else None           # shorter: turns were dropped from the front


def predict(prev: dict, nxt: dict, model: str, producers: dict[int, str], behaviour: str | None) -> list[dict]:
    check = starter.binding(model)
    enforce = check == "enforced" or (check == "recorded" and behaviour is not None)
    d = divergence(prev, nxt)
    out, dropping = [], False
    for i, j in starter.thinking_paths(nxt):
        path, producer = f"messages.{i}.content.{j}", producers.get(i, model)
        if not can_read_thinking(model, producer):                       # 1. the model check
            out.append({"path": path, "outcome": "dropped", "reason": "model_binding_mismatch"})
            continue
        if dropping:                                                     # drop_block: everything after the first
            out.append({"path": path, "outcome": "dropped", "reason": "prefix_binding_mismatch"})
            continue
        checked = check in ("enforced", "recorded") and starter.binding(producer) in ("enforced", "recorded")
        if not checked or d is None or d > i:                            # 2. the prefix check
            out.append({"path": path, "outcome": "kept", "reason": ""})
        elif not enforce:
            out.append({"path": path, "outcome": "allowed", "reason": "prefix_binding_mismatch"})
        elif behaviour == "drop_block":
            dropping = True
            out.append({"path": path, "outcome": "dropped", "reason": "prefix_binding_mismatch"})
        else:
            out.append({"path": path, "outcome": "rejected", "reason": "prefix_binding_mismatch"})
    return out


def main() -> None:
    starter.divergence, starter.predict = divergence, predict           # plug the solution into the test bench
    client = get_client()
    print("Exercise 10 (solution) - predict binding breaks before sending")
    cases = starter.cases(client)
    matches = 0
    rows = []
    for name, prev, nxt, model, producers, behaviour in cases:
        mine = starter.summarise(predict(prev, nxt, model, producers, behaviour))
        api = starter.api_verdict(client, nxt, model, behaviour)
        matches += mine == api
        where = divergence(prev, nxt)
        head_changed = starter.canonical(prev.get("system") or "") != starter.canonical(nxt.get("system") or "") \
            or tool_set(prev) != tool_set(nxt)
        where_text = "-" if where is None else "system/tools" if head_changed else f"messages[{where}]"
        shown = (", ".join(sorted(set(mine.values()))) + f" x{len(mine)}") if mine else "all kept"
        rows.append([name, behaviour or "(unset)", where_text, shown,
                     "match" if mine == api else "MISMATCH"])
    starter.d3.table(rows, ["case", "prefix_mismatch_behavior", "diverges at", "predicted = API", "check"])
    print(f"{matches}/{len(cases)} predictions match the API. Run predict() in the request path: in CI fail the build "
          "on any 'rejected' or 'dropped (prefix)'; in production log them before the API does.")


if __name__ == "__main__":
    main()
