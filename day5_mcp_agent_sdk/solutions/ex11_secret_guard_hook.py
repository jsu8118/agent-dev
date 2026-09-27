"""Solution to exercise 11 - a PreToolUse hook that keeps files containing "secret" away from the model.

Objective
    Deny any Read of a file whose content contains "secret" (case-insensitive), and any Grep whose
    search scope includes such a file, then prove with a canary string that the protected content
    never entered the conversation.

Concepts
    Content-based (not name-based) policy; canonical paths; closing the side door (Grep reads file
    content too); deny reasons that steer the agent; unit-testing hooks without the CLI; canary tokens
    and session transcripts as evidence.

Run
    python day5_mcp_agent_sdk/solutions/ex11_secret_guard_hook.py

What to observe
    * The unit tests exercise every branch of the policy in milliseconds.
    * In the agent run, two reads succeed, one read and one repository-wide Grep are denied, and the
      agent reports which file it could not open.
    * The canary check scans the session transcript on disk - it works the same in live mode.
"""

# test: timeout=240
# test: expect=canary absent

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, SystemMessage, query  # noqa: E402

from _agent_common import agent_runtime, show                                   # noqa: E402
from labkit import MODEL, header, runs_dir, step                                # noqa: E402

CANARY = "CANARY-7F3A9-VENDOR"
MAX_SCAN_BYTES = 2_000_000          # don't let a huge file turn the hook into a denial-of-service


def make_workspace() -> Path:
    root = runs_dir("day5_secret_guard", "workspace")
    shutil.rmtree(root, ignore_errors=True)
    (root / "vendor").mkdir(parents=True)
    (root / "handover-notes.md").write_text(
        "# Shift handover 2026-09-14\n- HF-KP250-03: hum reported, bearing wear suspected, parts requested.\n"
        "- GB-KP400-02: impeller pitting found on borescope; schedule inspection.\n", encoding="utf-8")
    (root / "shift-log.txt").write_text("06:00 start of shift\n09:24 PagerDuty: Kestrel Connect 5xx alert\n",
                                        encoding="utf-8")
    (root / "vendor" / "portal-access.env").write_text(
        f"# Vendor portal credentials - SECRET, do not share\nVENDOR_USER=kestrel-ops\nVENDOR_PASSWORD={CANARY}\n",
        encoding="utf-8")
    return root


# --------------------------------------------------------------------------- the policy
def contains_secret(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"secret" in fh.read(MAX_SCAN_BYTES).lower()
    except (OSError, IsADirectoryError):
        return False


def secret_files_under(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if contains_secret(path) else []
    return sorted(p for p in path.rglob("*") if p.is_file() and contains_secret(p)) if path.is_dir() else []


def secret_policy(tool_name: str, tool_input: dict[str, Any], cwd: str) -> str | None:
    base = Path(cwd)
    if tool_name == "Read":
        target = (base / str(tool_input.get("file_path", ""))).resolve()
        if contains_secret(target):
            return f"{target.name} contains secrets; it may not be read. Continue without it and say it was withheld."
    elif tool_name == "Grep":
        scope = (base / str(tool_input.get("path") or ".")).resolve()
        hits = secret_files_under(scope)
        if hits:   # Grep would return lines of these files: close the side door too
            names = ", ".join(str(h.relative_to(base)) if h.is_relative_to(base) else str(h) for h in hits)
            return (f"This search covers files that contain secrets ({names}). "
                    "Search specific non-secret files instead.")
    return None


def make_hook(decisions: list[dict]):
    async def guard(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        reason = secret_policy(input_data["tool_name"], input_data.get("tool_input") or {}, input_data.get("cwd", "."))
        decisions.append({"tool": input_data["tool_name"], "input": input_data.get("tool_input"), "reason": reason})
        if reason:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        return {}
    return guard


def unit_tests(root: Path) -> None:
    cases = [("Read", {"file_path": "handover-notes.md"}, False),
             ("Read", {"file_path": str(root / "vendor" / "portal-access.env")}, True),
             ("Read", {"file_path": "vendor/../vendor/portal-access.env"}, True),
             ("Grep", {"pattern": "PASSWORD", "path": "."}, True),
             ("Grep", {"pattern": "hum", "path": "handover-notes.md"}, False),
             ("Glob", {"pattern": "**/*"}, False)]
    for tool_name, tool_input, expect_deny in cases:
        reason = secret_policy(tool_name, tool_input, str(root))
        ok = bool(reason) == expect_deny
        print(f"  [{'ok' if ok else 'FAIL'}] {tool_name}({json.dumps(tool_input)[:60]}) -> {'deny' if reason else 'pass'}")
        assert ok, reason


SYSTEM_PROMPT = """KESTREL-SECRET-GUARD
You help the on-call engineer catch up on shift notes in the current directory. Use Glob to list files and
Read or Grep to read them. Summarise what matters for the next shift. If a file is withheld, say so."""


async def run() -> None:
    header(f"Exercise 11 - a PreToolUse hook that withholds files containing secrets ({MODEL})")
    root = make_workspace()
    step(1, "Unit-test the policy")
    unit_tests(root)

    step(2, "Run an agent over the workspace with the hook installed")
    decisions: list[dict] = []
    transcript: str | None = None
    with agent_runtime("ex11") as env:
        options = ClaudeAgentOptions(
            model=MODEL, system_prompt=SYSTEM_PROMPT, tools=["Read", "Grep", "Glob"],
            allowed_tools=["Read", "Grep", "Glob"], permission_mode="dontAsk", cwd=str(root), setting_sources=[],
            hooks={"PreToolUse": [HookMatcher(matcher="Read|Grep", hooks=[make_hook(decisions)])]},
            max_turns=12, max_budget_usd=1.0, env=env)
        async for message in query(prompt="Catch me up on everything in this folder.", options=options):
            show(message)
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id")
                matches = list(Path(env["CLAUDE_CONFIG_DIR"]).glob(f"projects/*/{session_id}.jsonl"))
                transcript = str(matches[0]) if matches else None
            if isinstance(message, ResultMessage) and transcript is None:
                matches = list(Path(env["CLAUDE_CONFIG_DIR"]).glob(f"projects/*/{message.session_id}.jsonl"))
                transcript = str(matches[0]) if matches else None

    step(3, "Evidence")
    for d in decisions:
        print(f"  {'DENY' if d['reason'] else 'pass'} {d['tool']}({json.dumps(d['input'])[:70]})")
    if transcript is None:
        matches = sorted(runs_dir("day5_agent_sdk", "ex11", "claude-config").glob("projects/*/*.jsonl"),
                         key=lambda p: p.stat().st_mtime)
        transcript = str(matches[-1]) if matches else None
    text = Path(transcript).read_text(encoding="utf-8") if transcript else ""
    print(f"  session transcript: {transcript}")
    print(f"  canary {CANARY!r} in transcript: {CANARY in text} -> {'canary absent' if CANARY not in text else 'LEAKED'}")


if __name__ == "__main__":
    asyncio.run(run())
