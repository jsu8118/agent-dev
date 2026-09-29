"""Exercise 8 (starter) - add a close-out phase that removes tools, and prove three properties.

Extend lab 04's cache-preserving conversation with a fifth phase, "close-out", whose tools are close_service_ticket and
create_task and which REMOVES the communication tools (notify_account_manager, post_teams_message). Then check:

  1. cache reads never fall at a phase change (each request reads at least what the previous one read);
  2. a call to a removed tool is refused by the executor gate (code "not_available"), whatever the model saw;
  3. in the close-out phase, a request that needs a removed tool is not served by it (the model says it lacks it).

Run
    python advanced/day2_tools_at_scale/exercises/ex08_tool_removal_phase.py

The starter runs the four phases of lab 04 and prints TODO markers where your code goes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import get_client, header, step  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = (d2.MARK_PHASES + "\nYou are Kestrel's operations copilot. The tools you have change as the conversation moves from "
          "diagnosis to scheduling to customer communication; use the ones you have now and say plainly when you lack one. "
          "Answer only from tool results.")
CLOSE_OUT = {"name": "close-out", "tools": ["close_service_ticket", "create_task"],
             "turns": ["Close ticket SVC-4030 with resolution 'board replaced under RC-2026-03', and create a task for the "
                       "quality team due 2026-09-30 to review the F17 logs.",
                       "Also post a short update to the field-service Teams channel."]}


def ref(kind: str, name: str) -> dict:
    return {"type": kind, "tool": {"type": "tool_reference", "name": name}}


def run_phases(client, phases: list[dict]) -> tuple[list[tuple[str, d2.Turn]], d2.KestrelOps, d2.RunResult | None]:
    """Lab 04's variant B: every phase tool declared once with defer_loading, phases switched by tool changes."""
    names = list(dict.fromkeys(n for p in phases for n in p["tools"]))
    tools = d2.wide_toolset(search=None, names=d2.core_names() + names)
    for t in tools:
        t["defer_loading"] = t["name"] in names
    ops, messages, rows, current, run = d2.KestrelOps(), [], [], set(), None
    for phase in phases:
        wanted = set(phase["tools"])
        changes = [ref("tool_removal", n) for n in names if n in current - wanted] + \
                  [ref("tool_addition", n) for n in phase["tools"] if n not in current]
        for i, text in enumerate(phase["turns"]):
            messages.append({"role": "user", "content": text})
            if i == 0 and changes:
                messages.append({"role": "system", "content": changes})
            ops.allowed = set(d2.core_names()) | wanted
            run = d2.run_agent(client, system=SYSTEM, tools=tools, messages=messages, trace=False,
                               betas=[d2.TOOL_CHANGES_BETA], execute=ops.run)
            rows += [(phase["name"], t) for t in run.turns]
        current = wanted
    return rows, ops, run


def main() -> None:
    client = get_client()
    header("Exercise 8 - a close-out phase with tool_removal")
    step(1, "The four phases of lab 04")
    rows, ops, _ = run_phases(client, d2.PHASES)
    for phase, t in rows:
        print(f"  {phase:<12} read {t.cache_read:>6,} write {t.cache_write:>5,}  {'; '.join(t.calls) or '(answer)'}")

    step(2, "Your fifth phase")
    print("TODO: run the conversation with d2.PHASES + [CLOSE_OUT] (run_phases already handles removals).")
    print("TODO: check property 1 - cache reads never fall between consecutive requests.")
    print("TODO: check property 2 - ops.run('post_teams_message', ...) is refused with code 'not_available' in close-out.")
    print("TODO: check property 3 - the last turn asks for a Teams post; post_teams_message must not be called.")


if __name__ == "__main__":
    main()
