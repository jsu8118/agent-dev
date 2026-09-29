"""Solution to exercise 8 - a close-out phase that removes tools, with its three properties checked.

Objective
    Run lab 04's cache-preserving conversation with a fifth phase (close-out) that adds close_service_ticket and
    create_task and removes the communication tools, then check that cache reads never fall, that the executor
    refuses a removed tool, and that the model does not use a removed tool when asked for it.

Concepts
    tool_removal + tool_addition in one system message, the executor gate as the second half of a removal, cache
    reads as a regression signal, asking for a removed capability

Run
    python advanced/day2_tools_at_scale/solutions/ex08_tool_removal_phase.py

What to observe
    * Three PASS lines.
    * The close-out answer says the Teams post is not possible with the tools available now.
"""
# test: expect=property 1
# test: expect=PASS

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))

from labkit import get_client, header, step, wrap  # noqa: E402

import ex08_tool_removal_phase as ex  # noqa: E402

d2 = ex.d2


def main() -> None:
    client = get_client()
    header("Exercise 8 (solution) - a close-out phase with tool_removal")
    rows, ops, last = ex.run_phases(client, d2.PHASES + [ex.CLOSE_OUT])

    step(1, "Request by request")
    for i, (phase, t) in enumerate(rows, 1):
        print(f"  {i:>2} {phase:<12} read {t.cache_read:>6,} write {t.cache_write:>5,}  {'; '.join(c.split('(')[0] for c in t.calls) or '(answer)'}")

    step(2, "The three properties")
    reads = [t.cache_read for _, t in rows]
    falls = [(i + 1, reads[i], reads[i + 1]) for i in range(1, len(reads) - 1) if reads[i + 1] < reads[i]]
    print(f"  property 1 - cache reads never fall after the first request: {'PASS' if not falls else f'FAIL {falls}'}")
    content, is_error = ops.run("post_teams_message", {"channel": "field-service", "text": "stale call"})
    ok = is_error and '"not_available"' in content
    print(f"  property 2 - the executor refuses a removed tool: {'PASS' if ok else 'FAIL'} ({d2.clip(content, 80)})")
    called_last = last.called if last else []
    print(f"  property 3 - the Teams post was not made with a removed tool: "
          f"{'PASS' if 'post_teams_message' not in called_last else 'FAIL'}")
    print("  answer to the last turn:")
    print(wrap(last.reply if last else "", "     | "))
    print("\nA removal is two changes: the tool_removal block (what the model sees) and the executor's allowed set (what can\n"
          "run). The first keeps the model from trying; the second holds when it tries anyway - a stale plan, an injection.")


if __name__ == "__main__":
    main()
