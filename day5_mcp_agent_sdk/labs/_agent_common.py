"""Shared plumbing for the Claude Agent SDK labs (05-07) and solutions.  Not a lab ("_" prefix).

The Agent SDK runs the Claude Code CLI as a child process.  Two practical consequences shape this module:

1. **The child inherits your environment.**  The Python SDK merges `os.environ` into the child's
   environment (and `ClaudeAgentOptions.env` on top).  Variables such as `CLAUDE_CODE_*`, `CLAUDE_CONFIG_DIR`
   or `CLAUDE_EFFORT` - set by a surrounding Claude Code session, a CI runner or your shell profile - would
   silently change the agent's behaviour.  `agent_runtime()` therefore removes them from this process's
   environment before launching the CLI, points the CLI at a private config directory (no user settings,
   hooks, MCP servers or CLAUDE.md files leak in), and turns off telemetry and other non-essential traffic.
2. **The CLI only takes a base URL.**  In mock mode we start labkit's mock API as a local HTTP server
   and point `ANTHROPIC_BASE_URL` at it; in live mode the CLI talks to the real API with your key.
   That is the ONLY difference between the modes.
"""

from __future__ import annotations

import json
import os
import textwrap
from contextlib import contextmanager
from typing import Any, Iterator

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from labkit import is_mock, runs_dir
from labkit.mock.server import running_mock_server

# Prefixes of variables that configure Claude Code itself; they must not leak into our agents.
# CLAUDECODE, CLAUDE_CODE_*, CLAUDE_CONFIG_DIR, CLAUDE_EFFORT, CLAUDE_AGENT_SDK_*, ... and cloud-runner variables.
_SCRUB_PREFIXES = ("CLAUDECODE", "CLAUDE_", "CCR_")
_SCRUB_EXACT = ("AI_AGENT",)


def _scrub_parent_environment(mock: bool) -> list[str]:
    removed = []
    for key in list(os.environ):
        drop = key.startswith(_SCRUB_PREFIXES) or key in _SCRUB_EXACT
        # In mock mode no real credential or endpoint may reach the child; in live mode keep them.
        if mock and key.startswith("ANTHROPIC_"):
            drop = True
        if drop:
            removed.append(key)
            os.environ.pop(key, None)
    return removed


@contextmanager
def agent_runtime(lab: str, **extra_env: str) -> Iterator[dict[str, str]]:
    """Yield the `env` dict for ClaudeAgentOptions: hermetic, and wired to the mock or the real API."""
    mock = is_mock()                     # decide BEFORE touching the environment
    removed = _scrub_parent_environment(mock)
    env = {
        "CLAUDE_CONFIG_DIR": str(runs_dir("day5_agent_sdk", lab, "claude-config")),  # sessions/transcripts live here
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",   # no telemetry, error reporting or update checks
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        # Load our few tool definitions up front instead of deferring them behind tool search: with a
        # handful of tools this is faster, and it behaves the same against any base URL.
        "ENABLE_TOOL_SEARCH": "false",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "kestrel-agent-course/day5",   # identifies us in the User-Agent
        **extra_env,
    }
    scrubbed = f" (removed {len(removed)} inherited CLAUDE*/runtime variables)" if removed else ""
    if mock:
        with running_mock_server() as base_url:
            env.update(ANTHROPIC_BASE_URL=base_url, ANTHROPIC_API_KEY="mock-key")
            print(f"[day5] MOCK MODE - the Claude Code CLI runs for real (tools, hooks, permissions, subagents) "
                  f"but its API calls go to labkit's mock at {base_url}{scrubbed}.")
            yield env
    else:
        # Nothing to add: the SDK merges this process's environment into the CLI's, so the credentials
        # labkit found (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN, from your shell or .env) reach the CLI
        # without this module ever reading them.
        print(f"[day5] LIVE MODE - the Claude Code CLI calls the Claude API with your credentials{scrubbed}.")
        yield env


# --------------------------------------------------------------------------- printing the message stream
def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _result_text(block: ToolResultBlock) -> str:
    if isinstance(block.content, list):
        return " ".join(part.get("text", f"<{part.get('type')}>") for part in block.content if isinstance(part, dict))
    return str(block.content or "")


def show(message: Any, *, text_limit: int = 3000, result_limit: int = 150) -> None:
    """Print one Agent SDK message in a compact, log-friendly form."""
    if isinstance(message, SystemMessage):
        if message.subtype == "init":
            d = message.data
            servers = [f"{s.get('name')}:{s.get('status')}" for s in d.get("mcp_servers", [])]
            print(f"[init] session={d.get('session_id', '?')[:8]} model={d.get('model')} "
                  f"permission_mode={d.get('permissionMode')} tools={d.get('tools')} mcp={servers}")
        elif message.subtype == "task_started":
            d = message.data
            mode = "background" if d.get("is_backgrounded") else "foreground"
            print(f"[task] started {d.get('subagent_type')} ({mode}): {d.get('description', '')}")
        elif message.subtype == "task_notification":
            d, usage = message.data, message.data.get("usage") or {}
            print(f"[task] {d.get('status')}: {d.get('task_id', '')[:8]} tokens={usage.get('total_tokens', '?')} "
                  f"tool_uses={usage.get('tool_uses', '?')} duration={usage.get('duration_ms', '?')} ms")
        return
    if isinstance(message, AssistantMessage):
        prefix = "  [subagent] " if message.parent_tool_use_id else ""
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                body = block.text if len(block.text) <= text_limit else block.text[:text_limit] + " ..."
                print(f"{prefix}[assistant]\n" + textwrap.indent(body, prefix + "  "))
            elif isinstance(block, ToolUseBlock):
                print(f"{prefix}[tool_use] {block.name}({_short(block.input, 140)})")
            elif isinstance(block, ThinkingBlock):
                pass    # thinking text is omitted by default on current models
        return
    if isinstance(message, UserMessage):
        prefix = "  [subagent] " if message.parent_tool_use_id else ""
        if isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    flag = "ERROR " if block.is_error else ""
                    print(f"{prefix}[tool_result] {flag}{_short(_result_text(block), result_limit)}")
        return
    if isinstance(message, ResultMessage):
        usage = message.usage or {}
        cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd is not None else "n/a"
        print(f"[result] subtype={message.subtype} turns={message.num_turns} duration={message.duration_ms} ms "
              f"(api {message.duration_api_ms} ms) cost~{cost}{' (simulated)' if is_mock() else ''} "
              f"tokens in={usage.get('input_tokens', 0)} cache_w={usage.get('cache_creation_input_tokens', 0)} "
              f"cache_r={usage.get('cache_read_input_tokens', 0)} out={usage.get('output_tokens', 0)}")
        if message.permission_denials:
            print(f"[result] permission denials: {len(message.permission_denials)}")
