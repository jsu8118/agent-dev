"""Request validation for the mock /v1/messages endpoint.

Each check mirrors a documented rule of the real Messages API, so code that passes in
mock mode does not trip over the same 400s in live mode.  Messages are phrased like the
real API's so the lesson transfers: when you meet the real error you'll recognise it.

Rules enforced (non-exhaustive):
  * required fields; unknown model -> 404; max_tokens within the model's output limit
  * sampling params (temperature/top_p/top_k) rejected on models that removed them
  * thinking config validity per model; effort levels per model
  * no assistant prefill on models that removed it
  * tool definitions: names, schemas, uniqueness; tool_choice consistency
  * every tool_use answered by a tool_result in the NEXT user message, results first
  * thinking blocks passed back unmodified (signature check), and kept in tool loops
  * <= 4 cache_control breakpoints; structured-output schema rules
  * beta-only parameters require their anthropic-beta header
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ModelSpec, get_spec, known_model, thinking_active
from . import signing
from .errors import ApiError, bad_request

KNOWN_PARAMS = {
    "model", "max_tokens", "messages", "system", "tools", "tool_choice", "metadata",
    "stop_sequences", "stream", "thinking", "output_config", "output_format", "cache_control",
    "service_tier", "container", "inference_geo", "user_profile_id", "workspace_id",
    "temperature", "top_p", "top_k",
    # beta-only
    "context_management", "compaction", "diagnostics", "fallback_credit_token", "fallbacks",
    "mcp_servers", "speed",
}

BETA_GATED = {
    "mcp_servers": {"mcp-client-2025-11-20"},
    "speed": {"fast-mode-2026-02-01"},
    "diagnostics": {"cache-diagnosis-2026-04-07"},
    "compaction": {"compact-2026-01-12", "compact-2026-09-04"},
}

TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _merge_consecutive(messages: list[dict]) -> list[tuple[int, dict]]:
    """The API merges consecutive same-role turns; return (original_index, merged) pairs."""
    merged: list[tuple[int, dict]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if merged and merged[-1][1]["role"] == role and role in ("user", "assistant"):
            merged[-1][1]["content"] = merged[-1][1]["content"] + _blocks(m.get("content"))
        else:
            merged.append((i, {"role": role, "content": list(_blocks(m.get("content"))), "_raw": m}))
    return merged


def _schema_objects_closed(schema: Any, path: str) -> str | None:
    """Return the path of the first object schema lacking additionalProperties: false."""
    if isinstance(schema, dict):
        is_object = schema.get("type") == "object" or "properties" in schema
        if is_object and schema.get("additionalProperties") is not False:
            return path or "schema"
        for key in ("properties", "$defs", "definitions"):
            for name, sub in (schema.get(key) or {}).items():
                found = _schema_objects_closed(sub, f"{path}.{key}.{name}")
                if found:
                    return found
        for key in ("items",):
            if key in schema:
                found = _schema_objects_closed(schema[key], f"{path}.{key}")
                if found:
                    return found
        for key in ("anyOf", "allOf", "oneOf"):
            for idx, sub in enumerate(schema.get(key) or []):
                found = _schema_objects_closed(sub, f"{path}.{key}.{idx}")
                if found:
                    return found
    return None


def _count_cache_breakpoints(body: dict) -> int:
    count = 1 if body.get("cache_control") else 0
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("cache_control"):
            count += 1
    system = body.get("system")
    if isinstance(system, list):
        count += sum(1 for b in system if isinstance(b, dict) and b.get("cache_control"))
    for m in body.get("messages") or []:
        for b in _blocks(m.get("content")):
            if b.get("cache_control"):
                count += 1
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                count += sum(1 for sub in b["content"] if isinstance(sub, dict) and sub.get("cache_control"))
    return count


def _validate_thinking_config(body: dict, spec: ModelSpec) -> None:
    thinking = body.get("thinking")
    effort = (body.get("output_config") or {}).get("effort")
    if thinking is None:
        return
    if not isinstance(thinking, dict) or "type" not in thinking:
        raise bad_request("thinking: Field 'type' is required")
    kind = thinking["type"]
    if kind == "enabled":
        if spec.budget_tokens == "removed":
            raise bad_request(
                f'thinking.type: "enabled" is not supported for {spec.id}. Use thinking.type "adaptive" '
                'and control depth with output_config.effort.'
            )
        budget = thinking.get("budget_tokens")
        if not isinstance(budget, int) or budget < 1024:
            raise bad_request("thinking.enabled.budget_tokens: Input should be greater than or equal to 1024")
        if budget >= int(body.get("max_tokens", 0)):
            raise bad_request("max_tokens must be greater than thinking.budget_tokens")
    elif kind == "adaptive":
        if not spec.supports_adaptive:
            raise bad_request(f'thinking.type: "adaptive" is not supported for {spec.id}; '
                              'use {"type": "enabled", "budget_tokens": N} on this model.')
    elif kind == "disabled":
        if spec.disable_thinking == "no":
            raise bad_request(f'thinking.type: "disabled" is not supported for {spec.id}; thinking is always on. '
                              "Lower output_config.effort instead.")
        if spec.disable_thinking == "effort<=high" and effort in ("xhigh", "max"):
            raise bad_request(f'thinking.type "disabled" cannot be combined with effort "{effort}" on {spec.id}; '
                              'enable thinking or use effort "high" or lower.')
    else:
        raise bad_request(f"thinking.type: Input should be 'enabled', 'adaptive' or 'disabled' (got {kind!r})")


def _validate_tools(body: dict, spec: ModelSpec) -> set[str]:
    names: set[str] = set()
    tools = body.get("tools")
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise bad_request("tools: Input should be a valid list")
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise bad_request(f"tools.{i}: Input should be a valid dictionary")
        name = tool.get("name")
        kind = tool.get("type")
        is_custom = kind in (None, "custom")
        if kind == "mcp_toolset":
            continue
        if name is not None:
            if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
                raise bad_request(f"tools.{i}.name: String should match pattern '^[a-zA-Z0-9_-]{{1,64}}$'")
            if name in names:
                raise bad_request("tools: Tool names must be unique.")
            names.add(name)
        if is_custom:
            if not name:
                raise bad_request(f"tools.{i}.custom.name: Field required")
            schema = tool.get("input_schema")
            if not isinstance(schema, dict):
                raise bad_request(f"tools.{i}.custom.input_schema: Field required")
            if schema.get("type") != "object":
                raise bad_request(f"tools.{i}.custom.input_schema.type: Input should be 'object'")
            if tool.get("strict"):
                bad = _schema_objects_closed(schema, "input_schema")
                if bad:
                    raise bad_request(f"tools.{i}.custom.{bad}: For 'object' type, 'additionalProperties' "
                                      "must be explicitly set to false when strict is true")
    choice = body.get("tool_choice")
    if choice is not None:
        ctype = choice.get("type") if isinstance(choice, dict) else None
        if ctype not in ("auto", "any", "tool", "none"):
            raise bad_request("tool_choice.type: Input should be 'auto', 'any', 'tool' or 'none'")
        if not tools and ctype != "none":
            raise bad_request("tool_choice may only be specified while providing tools.")
        if ctype in ("any", "tool") and not spec.forced_tool_choice:
            raise bad_request('tool_choice: type "tool" and "any" are not supported for this model.')
        if ctype == "tool" and choice.get("name") not in names:
            raise bad_request(f"tool_choice.name: tool {choice.get('name')!r} was not found in tools")
    return names


def _validate_system_messages(messages: list[dict], spec: ModelSpec) -> None:
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            continue
        if not spec.mid_conversation_system:
            raise bad_request(f"messages.{i}.role: role 'system' is not supported on this model ({spec.id})")
        content = m.get("content")
        effort_only = content == [] and m.get("output_config")
        tool_changes = isinstance(content, list) and content and all(
            isinstance(b, dict) and b.get("type") in ("tool_addition", "tool_removal") for b in content)
        if effort_only or tool_changes:
            continue
        prev = messages[i - 1] if i > 0 else None
        if prev is None or prev.get("role") not in ("user",) and not (
            prev.get("role") == "assistant" and _blocks(prev.get("content"))
            and _blocks(prev.get("content"))[-1].get("type") == "server_tool_use"):
            raise bad_request(f"messages.{i}: a system message must follow a user message")
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        if nxt is not None and nxt.get("role") != "assistant":
            raise bad_request(f"messages.{i}: a system message must be the last message or be followed by an assistant message")


def _validate_conversation(messages: list[dict], spec: ModelSpec, thinking_on: bool) -> None:
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    merged = _merge_consecutive(convo)
    for pos, (orig_idx, msg) in enumerate(merged):
        blocks = msg["content"]
        if msg["role"] == "assistant":
            for j, b in enumerate(blocks):
                if b.get("type") == "thinking":
                    if not signing.verify(b.get("thinking", ""), b.get("signature", "")):
                        raise bad_request(f"messages.{orig_idx}.content.{j}: Invalid `signature` in `thinking` block. "
                                          "Thinking blocks must be passed back exactly as received.")
            tool_ids = [b.get("id") for b in blocks if b.get("type") == "tool_use"]
            if not tool_ids:
                continue
            nxt = merged[pos + 1][1] if pos + 1 < len(merged) else None
            answered: list[str] = []
            if nxt is not None and nxt["role"] == "user":
                seen_other = False
                for k, b in enumerate(nxt["content"]):
                    if b.get("type") == "tool_result":
                        if seen_other:
                            raise bad_request(
                                f"messages.{merged[pos + 1][0]}.content.{k}: tool_result blocks must come first in "
                                "the user message content, before any text or other blocks.")
                        answered.append(b.get("tool_use_id"))
                    else:
                        seen_other = True
            missing = [t for t in tool_ids if t not in answered]
            if missing:
                raise bad_request(
                    f"messages.{orig_idx}: `tool_use` ids were found without `tool_result` blocks immediately after: "
                    f"{', '.join(missing)}. Each `tool_use` block must have a corresponding `tool_result` block in the next message.")
            # Thinking must be preserved in an active tool loop (only checked for turns the mock produced).
            is_last_assistant = all(m["role"] != "assistant" for _, m in merged[pos + 1:])
            produced_by_mock = any(str(t).startswith(signing.TOOL_ID_PREFIX) for t in tool_ids)
            # A compaction block may precede the thinking block ([compaction, thinking, tool_use] is valid), and a
            # turn served by a fallback model (it contains a `fallback` block) carries that model's thinking, if any.
            first = next((k for k, b in enumerate(blocks) if b.get("type") != "compaction"), 0)
            served_by_fallback = any(b.get("type") == "fallback" for b in blocks)
            if thinking_on and is_last_assistant and produced_by_mock and not served_by_fallback and \
                    blocks[first].get("type") not in ("thinking", "redacted_thinking"):
                raise bad_request(
                    f"messages.{orig_idx}.content.{first}.type: Expected `thinking` or `redacted_thinking`, but found "
                    f"`{blocks[first].get('type')}`. When thinking is enabled, a final `assistant` message must start with "
                    "a thinking block (preceding the lastmost set of `tool_use` and `tool_result` blocks). "
                    "Append `response.content` verbatim instead of rebuilding assistant turns.")
        else:
            prev = merged[pos - 1][1] if pos > 0 else None
            prev_ids = {b.get("id") for b in prev["content"] if b.get("type") == "tool_use"} if prev else set()
            for k, b in enumerate(blocks):
                if b.get("type") == "tool_result" and b.get("tool_use_id") not in prev_ids:
                    raise bad_request(
                        f"messages.{orig_idx}.content.{k}: unexpected `tool_use_id` found in `tool_result` blocks: "
                        f"{b.get('tool_use_id')}. Each `tool_result` block must have a corresponding `tool_use` block "
                        "in the previous message.")


def validate_messages_request(body: dict, headers: dict[str, str], *, lenient: bool = False) -> None:
    """Raise ApiError if the request would be rejected by the real API."""
    if not isinstance(body, dict):
        raise bad_request("Request body must be a JSON object")
    for field in ("model", "max_tokens", "messages"):
        if field not in body:
            raise bad_request(f"{field}: Field required")
    model = body["model"]
    if not known_model(model):
        raise ApiError(404, f"model: {model}")
    spec = get_spec(model)
    betas = {b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()}

    if not lenient:
        unknown = sorted(set(body) - KNOWN_PARAMS)
        if unknown:
            raise bad_request(f"{unknown[0]}: Extra inputs are not permitted")

    max_tokens = body["max_tokens"]
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 0:
        raise bad_request("max_tokens: Input should be a valid non-negative integer")
    if max_tokens > spec.max_output:
        raise bad_request(f"max_tokens: {max_tokens} > {spec.max_output}, which is the maximum allowed number "
                          f"of output tokens for {model}")

    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise bad_request("messages: at least one message is required")
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "system"):
            raise bad_request(f"messages.{i}.role: Input should be 'user' or 'assistant'")
        if "content" not in m:
            raise bad_request(f"messages.{i}.content: Field required")
    if messages[0].get("role") != "user":
        raise bad_request('messages: first message must use the "user" role')

    for param in ("temperature", "top_p", "top_k"):
        if param in body:
            if spec.sampling_params == "no":
                raise bad_request(f"{param}: sampling parameters are not supported for {model}. "
                                  "Remove temperature/top_p/top_k (steer with prompts, effort, or structured outputs).")
            if spec.sampling_params == "default-only" and not (param == "temperature" and body[param] == 1):
                raise bad_request(f"{param}: only the default value is supported for {model}")

    _validate_thinking_config(body, spec)
    effort = (body.get("output_config") or {}).get("effort")
    if effort is not None:
        if not spec.effort_levels:
            raise bad_request(f"output_config.effort: effort is not supported for {model}")
        if effort not in spec.effort_levels:
            raise bad_request(f"output_config.effort: {effort!r} is not supported for {model} "
                              f"(supported: {', '.join(spec.effort_levels)})")

    last = messages[-1]
    if last.get("role") == "assistant" and not spec.prefill:
        blocks = _blocks(last.get("content"))
        resuming_server_tool = bool(blocks) and blocks[-1].get("type") == "server_tool_use"
        if not resuming_server_tool:
            raise bad_request(f"This model ({model}) does not support assistant message prefill. "
                              "The conversation must end with a user message.")

    _validate_system_messages(messages, spec)
    _validate_tools(body, spec)
    _validate_conversation(messages, spec, thinking_active(model, body.get("thinking")))

    n_breakpoints = _count_cache_breakpoints(body)
    if n_breakpoints > 4:
        raise bad_request(f"A maximum of 4 blocks with cache_control may be provided. Found {n_breakpoints}.")

    fmt = (body.get("output_config") or {}).get("format")
    if fmt is not None:
        if not isinstance(fmt, dict) or fmt.get("type") != "json_schema" or not isinstance(fmt.get("schema"), dict):
            raise bad_request("output_config.format: expected {\"type\": \"json_schema\", \"schema\": {...}}")
        bad = _schema_objects_closed(fmt["schema"], "schema")
        if bad:
            raise bad_request(f"output_config.format.{bad}: For 'object' type, 'additionalProperties' must be "
                              "explicitly set to false")
        for m in messages:
            for b in _blocks(m.get("content")):
                if b.get("type") == "document" and (b.get("citations") or {}).get("enabled"):
                    raise bad_request("Citations are not supported together with structured outputs (output_config.format).")

    if not lenient:
        for param, needed in BETA_GATED.items():
            if param in body and not (needed & betas):
                raise bad_request(f"{param}: requires the anthropic-beta header {sorted(needed)[0]!r}")
        if "context_management" in body:
            edits = (body.get("context_management") or {}).get("edits") or []
            for e in edits:
                etype = e.get("type") if isinstance(e, dict) else None
                if etype == "compact_20260112":
                    if "compact-2026-01-12" not in betas:
                        raise bad_request("context_management: compact_20260112 requires the anthropic-beta header 'compact-2026-01-12'")
                elif etype in ("clear_tool_uses_20250919", "clear_thinking_20251015"):
                    if "context-management-2025-06-27" not in betas:
                        raise bad_request(f"context_management: {etype} requires the anthropic-beta header "
                                          "'context-management-2025-06-27'")
                else:
                    raise bad_request(f"context_management.edits: unknown edit type {etype!r}")
        if "fallbacks" in body:
            value = body["fallbacks"]
            if value == "default":
                if "server-side-fallback-2026-07-01" not in betas:
                    raise bad_request("fallbacks: the \"default\" form requires the anthropic-beta header "
                                      "'server-side-fallback-2026-07-01'")
            elif isinstance(value, list):
                if "server-side-fallback-2026-06-01" not in betas:
                    raise bad_request("fallbacks: the array form requires the anthropic-beta header "
                                      "'server-side-fallback-2026-06-01'")
            else:
                raise bad_request("fallbacks: expected \"default\" or a list of {\"model\": ...} entries")
