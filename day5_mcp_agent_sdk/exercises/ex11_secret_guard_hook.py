"""Exercise 11 starter - a PreToolUse hook that blocks reading files containing "secret".

Run:  python day5_mcp_agent_sdk/exercises/ex11_secret_guard_hook.py
It runs as-is (no CLI, no API) and prints TODO notes; the solution is solutions/ex11_secret_guard_hook.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from labkit import runs_dir


def make_workspace() -> Path:
    root = runs_dir("day5_secret_guard_starter")
    (root / "notes.md").write_text("Pump HF-KP250-03: hum reported.\n", encoding="utf-8")
    (root / "vendor.env").write_text("# SECRET vendor password\nPASSWORD=hunter2\n", encoding="utf-8")
    return root


def secret_policy(tool_name: str, tool_input: dict[str, Any], cwd: str) -> str | None:
    """Return a denial reason, or None to let the call proceed."""
    # TODO 1: Read -> resolve file_path against cwd; deny if the file's content contains "secret" (any case)
    # TODO 2: Grep -> deny if the search scope (path, default cwd) includes any such file
    # TODO 3: think about limits: huge files, binary files, symlinks, Bash, other MCP servers
    return None


async def guard(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
    reason = secret_policy(input_data["tool_name"], input_data.get("tool_input") or {}, input_data.get("cwd", "."))
    if reason:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    return {}


async def main() -> None:
    root = make_workspace()
    for tool_name, tool_input, expected in (("Read", {"file_path": "notes.md"}, "pass"),
                                            ("Read", {"file_path": "vendor.env"}, "deny"),
                                            ("Grep", {"pattern": "PASSWORD", "path": "."}, "deny")):
        out = await guard({"tool_name": tool_name, "tool_input": tool_input, "cwd": str(root)}, None, {})
        got = "deny" if out else "pass"
        print(f"{tool_name}({tool_input}) -> {got} (expected {expected}){'' if got == expected else '   <- TODO'}")
    print("Then register it: ClaudeAgentOptions(hooks={'PreToolUse': [HookMatcher(matcher='Read|Grep', hooks=[guard])]})")


if __name__ == "__main__":
    asyncio.run(main())
