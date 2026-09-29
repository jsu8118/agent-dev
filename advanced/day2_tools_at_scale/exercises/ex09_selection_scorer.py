"""Exercise 9 (starter) - a selection-eval scorer, and a held-out set.

Lab 07 printed accuracy and a confusion list. A scorer you can trust needs more:

  * accuracy with a 95% interval (Wilson);
  * per-tool recall (of the tasks that needed tool X, how many got X) and precision (of the calls to X, how many were right);
  * a confusion matrix of expected -> chosen, off-diagonal cells only;
  * a paired test of A vs B on the same tasks: an exact McNemar test on the discordant pairs;
  * a HELD-OUT set: ten tasks you write yourself, never looked at while writing description set B.

Run
    python advanced/day2_tools_at_scale/exercises/ex09_selection_scorer.py

The starter collects the predictions (first tool call per task, for description sets A and B) and prints TODO markers
where the scoring goes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header, step  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = d2.MARK_SELECT + "\nYou are Kestrel's operations copilot. Call the one tool that fits the request."

# TODO: write ten held-out tasks (request -> expected tool among d2.SELECTION_TOOLS) in your users' words.
HELD_OUT: list[tuple[str, str]] = []


def toolset(which: str) -> list[dict]:
    if which == "A":
        return d2.loaded_toolset(d2.SELECTION_TOOLS)
    return [d2.api_tool(d2.entry(n), description=d2.DESCRIPTIONS_B[n]) for n in d2.SELECTION_TOOLS]


def predictions(client, tasks: list[tuple[str, str]], which: str) -> list[str | None]:
    """The first tool the model calls for each task (None when it calls none)."""
    out = []
    for text, _ in tasks:
        r = client.messages.create(model=MODEL, max_tokens=2000, system=d2.cached_system(SYSTEM), tools=toolset(which),
                                   messages=[{"role": "user", "content": text}])
        out.append(next((b.name for b in r.content if b.type == "tool_use"), None))
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    raise NotImplementedError("TODO: the Wilson score interval")


def per_tool(expected: list[str], got: list[str | None]) -> dict[str, tuple[float, float]]:
    raise NotImplementedError("TODO: {tool: (precision, recall)}")


def mcnemar_exact(a_right: list[bool], b_right: list[bool]) -> tuple[int, int, float]:
    raise NotImplementedError("TODO: (b fixes, b breaks, two-sided exact p-value)")


def main() -> None:
    client = get_client()
    header("Exercise 9 - a selection-eval scorer")
    expected = [e for _, e in d2.SELECTION_TASKS]
    got = {w: predictions(client, d2.SELECTION_TASKS, w) for w in ("A", "B")}
    step(1, "Predictions collected")
    for w in ("A", "B"):
        print(f"  set {w}: {sum(g == e for g, e in zip(got[w], expected))}/{len(expected)} first calls correct")
    step(2, "Your scorer")
    for name in ("wilson", "per_tool", "mcnemar_exact"):
        try:
            {"wilson": lambda: wilson(1, 2), "per_tool": lambda: per_tool(expected, got["A"]),
             "mcnemar_exact": lambda: mcnemar_exact([True], [True])}[name]()
            print(f"  {name}: implemented")
        except NotImplementedError as exc:
            print(f"  {exc}")
    print(f"TODO: held-out set - {len(HELD_OUT)} of 10 tasks written; score A and B on it with the same scorer.")


if __name__ == "__main__":
    main()
