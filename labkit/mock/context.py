"""Server-side context management, simulated: tool-result clearing and compaction.

These features run on the API side *before* the model sees the prompt:

* clear_tool_uses_20250919 (beta context-management-2025-06-27): once the prompt
  exceeds `trigger` input tokens, old tool results (all but the most recent `keep`)
  are replaced by a placeholder.  Your client still holds the full history; the
  response reports what was cleared in `context_management.applied_edits`.
* compact_20260112 (beta compact-2026-01-12): once the prompt exceeds `trigger`,
  earlier turns are summarized into a `compaction` block returned at the start of the
  response.  You MUST append the full `response.content` (not just the text) so the
  next request carries the compaction block; the API then ignores everything before it.
"""

from __future__ import annotations

import copy
from typing import Any

from .tokens import block_tokens, text_tokens

PLACEHOLDER = "[tool result cleared by context management]"


def _blocks(content: Any) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content or [])


def _estimate(body: dict) -> int:
    from .cache import render_positions  # local import to avoid a cycle
    return sum(tokens for _, tokens, _ in render_positions(body))


def apply_compaction_history(body: dict) -> dict:
    """If the history contains a compaction block, drop everything before it."""
    messages = body.get("messages") or []
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") != "assistant":
            continue
        blocks = _blocks(msg.get("content"))
        for b_idx, block in enumerate(blocks):
            if isinstance(block, dict) and block.get("type") == "compaction":
                summary = block.get("content") or ""
                rest = blocks[b_idx + 1:]
                new_messages = [{"role": "user", "content": [
                    {"type": "text", "text": f"<conversation_summary>\n{summary}\n</conversation_summary>"}]}]
                if rest:
                    new_messages.append({"role": "assistant", "content": rest})
                new_messages.extend(messages[idx + 1:])
                new_body = dict(body)
                new_body["messages"] = new_messages
                return new_body
    return body


def summarize_for_compaction(body: dict, instructions: str | None = None) -> str:
    """A deterministic 'summary' of the conversation (stand-in for the model's summary)."""
    user_texts, tool_lines = [], []
    for msg in body.get("messages") or []:
        for block in _blocks(msg.get("content")):
            if not isinstance(block, dict):
                continue
            if msg.get("role") == "user" and block.get("type") == "text":
                user_texts.append(" ".join(block.get("text", "").split())[:160])
            elif block.get("type") == "tool_use":
                tool_lines.append(f"{block.get('name')}({', '.join(f'{k}={v}' for k, v in (block.get('input') or {}).items())})")
    parts = ["Summary of the conversation so far (compacted server-side)."]
    if instructions:
        parts.append(f"Focus requested: {instructions[:200]}")
    if user_texts:
        parts.append("User requests: " + " | ".join(user_texts[-6:]))
    if tool_lines:
        parts.append(f"Tool calls made ({len(tool_lines)}): " + "; ".join(tool_lines[-12:]))
    return "\n".join(parts)


def apply_context_management(body: dict) -> tuple[dict, list[dict], dict | None, int]:
    """Return (effective_body, applied_edits, new_compaction_block, compaction_input_tokens)."""
    config = body.get("context_management") or {}
    edits = config.get("edits") or []
    effective = apply_compaction_history(body)
    applied: list[dict] = []
    compaction_block = None
    compaction_input = 0

    for edit in edits:
        etype = edit.get("type")
        trigger = (edit.get("trigger") or {}).get("value")
        if etype == "clear_tool_uses_20250919":
            trigger = trigger or 100_000
            if _estimate(effective) <= trigger:
                continue
            keep = (edit.get("keep") or {}).get("value", 3)
            exclude = set(edit.get("exclude_tools") or [])
            clear_inputs = edit.get("clear_tool_inputs")
            effective = copy.deepcopy(effective)
            uses: list[tuple[dict, dict]] = []      # (tool_use block, tool_result block)
            by_id: dict[str, dict] = {}
            for msg in effective["messages"]:
                for block in _blocks(msg.get("content")):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        by_id[block.get("id")] = block
                    elif isinstance(block, dict) and block.get("type") == "tool_result":
                        use = by_id.get(block.get("tool_use_id"))
                        if use is not None and use.get("name") not in exclude:
                            uses.append((use, block))
            to_clear = uses[:-keep] if keep else uses
            saved, cleared = 0, 0
            for use, result in to_clear:
                if result.get("content") == PLACEHOLDER:
                    continue
                saved += max(block_tokens(result) - text_tokens(PLACEHOLDER), 0)
                result["content"] = PLACEHOLDER
                result.pop("is_error", None)
                if clear_inputs is True or (isinstance(clear_inputs, list) and use.get("name") in clear_inputs):
                    saved += block_tokens(use) - 8
                    use["input"] = {}
                cleared += 1
            if cleared:
                applied.append({"type": "clear_tool_uses_20250919", "cleared_tool_uses": cleared,
                                "cleared_input_tokens": saved})
        elif etype == "clear_thinking_20251015":
            keep = (edit.get("keep") or {}).get("value", 1) if isinstance(edit.get("keep"), dict) else 1
            assistant_idx = [i for i, m in enumerate(effective["messages"]) if m.get("role") == "assistant"]
            old = assistant_idx[:-keep] if keep else assistant_idx
            cleared_turns = 0
            effective = copy.deepcopy(effective)
            for i in old:
                msg = effective["messages"][i]
                blocks = _blocks(msg.get("content"))
                kept = [b for b in blocks if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
                if len(kept) != len(blocks):
                    msg["content"] = kept
                    cleared_turns += 1
            if cleared_turns:
                applied.append({"type": "clear_thinking_20251015", "cleared_thinking_turns": cleared_turns,
                                "cleared_input_tokens": 0})
        elif etype == "compact_20260112":
            trigger = trigger or 150_000
            total = _estimate(effective)
            if total <= trigger:
                continue
            summary = summarize_for_compaction(effective, edit.get("instructions"))
            compaction_block = {"type": "compaction", "content": summary}
            compaction_input = total
            messages = effective["messages"]
            tail_start = len(messages) - 1
            last_blocks = _blocks(messages[tail_start].get("content")) if messages else []
            if tail_start > 0 and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last_blocks):
                tail_start -= 1          # keep the tool_use turn that the pending tool_results answer
            effective = dict(effective)
            effective["messages"] = [{"role": "user", "content": [
                {"type": "text", "text": f"<conversation_summary>\n{summary}\n</conversation_summary>"}]}]
            effective["messages"].extend(messages[tail_start:])
    return effective, applied, compaction_block, compaction_input
