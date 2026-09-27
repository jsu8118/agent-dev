# Claude API cheat sheet (Python SDK `anthropic` 1.x)

Quick reference for the whole course. Facts current as of the course date (2026); when in doubt, check the
official docs or `client.models.retrieve(model_id)`.

## Models

| Model | ID | Context | $/M in | $/M out | Notes |
|---|---|---|---|---|---|
| Claude Fable 5.1 | `claude-fable-5-1` | 1M | 10 | 50 | thinking always on; forced `tool_choice` → 400; cache reads 0.025× |
| Claude Opus 5 | `claude-opus-5` | 1M | 5 | 25 | **course default**; thinking on by default; can return `refusal` |
| Claude Opus 5.5 | `claude-opus-5-5` | 1M | 4 | 20 | launching; effort default `medium`; thinking can't be disabled; forced `tool_choice` → 400 |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | 2 | 10 | thinking on by default; new tokenizer (~30% more tokens than 4.6) |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | 1 | 5 | no `effort`; thinking only via `budget_tokens`; accepts `temperature`, prefill |

Batches: −50%. Cache writes: 1.25× (5 min TTL) / 2× (1 h). Cache reads: ~0.1×. Output includes thinking tokens.

## A request

```python
from anthropic import Anthropic
client = Anthropic()                                    # ANTHROPIC_API_KEY from the environment
msg = client.messages.create(
    model="claude-opus-5",
    max_tokens=16000,                                   # caps thinking + text; stream if > ~16-21k
    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": "..."}],
    output_config={"effort": "medium"},                 # low | medium | high (default) | xhigh | max
    # thinking={"type": "adaptive", "display": "summarized"},
    # tools=[...], tool_choice={"type": "auto"},
    # cache_control={"type": "ephemeral"},             # automatic caching of the conversation tail
)
if msg.stop_reason in ("refusal", "max_tokens"): ...
text = "".join(b.text for b in msg.content if b.type == "text")
```

**Removed on current models (400):** `temperature` / `top_p` / `top_k`, `thinking.budget_tokens`, assistant prefill.

## stop_reason

`end_turn` · `tool_use` (run tools, send results) · `max_tokens` (incomplete — never run a truncated tool call)
· `stop_sequence` · `pause_turn` (server tool loop paused — re-send to continue) · `refusal` (HTTP 200; check `stop_details`).

## Structured outputs

```python
class Ticket(BaseModel):
    category: Literal["billing", "technical_support", ...]
    order_id: Optional[str] = Field(description="SO-##### if present, else null")
msg = client.messages.parse(model=..., max_tokens=4000, messages=..., output_format=Ticket)
msg.parsed_output            # validated instance (check stop_reason first)
# raw: output_config={"format": {"type": "json_schema", "schema": {..., "additionalProperties": False}}}
```
Not supported in schemas: recursion, numeric bounds, string lengths (the SDK validates those client-side).
Incompatible with citations.

## Tools

```python
tools = [{"name": "get_order", "description": "When to use ... what it returns ...", "strict": True,
          "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}},
                           "required": ["order_id"], "additionalProperties": False}}]
# loop: while stop_reason == "tool_use": append FULL response.content; run every tool_use;
#       send ALL tool_result blocks in ONE user message, tool_results first, is_error=True on failure
```
`tool_choice`: `auto` | `any` | `{"type":"tool","name":...}` | `none` (+ `disable_parallel_tool_use`).
Forced (`any`/`tool`) → 400 on Opus 5.5 / Fable 5.1: prefer `auto` + instructions + `strict`.
Tool Runner: `@anthropic.beta_tool` + `client.beta.messages.tool_runner(...).until_done()`.

## Streaming

```python
with client.messages.stream(model=..., max_tokens=64000, messages=...) as s:
    for text in s.text_stream: print(text, end="", flush=True)
    final = s.get_final_message()
```

## Prompt caching

Order: **tools → system → messages**; byte-exact prefix; ≤ 4 breakpoints; min cacheable prefix 512 (Opus 5) /
1,024 (Sonnet 5) / 4,096 (Haiku 4.5). Verify with `usage.cache_creation_input_tokens` /
`usage.cache_read_input_tokens`. Silent invalidators: timestamps/ids in the prefix, unsorted JSON, changing tools.

## Context management (beta, `client.beta.messages.create`)

* Tool-result clearing: `betas=["context-management-2025-06-27"]`,
  `context_management={"edits": [{"type": "clear_tool_uses_20250919", "trigger": {...}, "keep": {...}}]}`
* Compaction: `betas=["compact-2026-01-12"]`, `context_management={"edits": [{"type": "compact_20260112"}]}` —
  append the full `response.content` (keeps the `compaction` block).
* Memory tool: `{"type": "memory_20250818", "name": "memory"}` (client-side storage; `BetaAbstractMemoryTool`).

## Reliability

```python
client = Anthropic(max_retries=4, timeout=60)           # SDK retries 408/409/429/5xx/529 + connection errors
client.with_options(timeout=5.0).messages.create(...)
# Opus 5 refusal fallbacks:
client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")
```
Exceptions: `BadRequestError` 400 · `AuthenticationError` 401 · `PermissionDeniedError` 403 · `NotFoundError` 404 ·
`RateLimitError` 429 · `InternalServerError` 500 · `OverloadedError` 529 · `APIConnectionError` · `APITimeoutError`.

## Batches

```python
from anthropic.types.messages.batch_create_params import Request
b = client.messages.batches.create(requests=[Request(custom_id="t1", params={...})])
while client.messages.batches.retrieve(b.id).processing_status != "ended": time.sleep(30)
for r in client.messages.batches.results(b.id): r.custom_id, r.result.type   # any order!
```

## Tokens

`client.messages.count_tokens(model=..., system=..., tools=..., messages=...)` — free, model-specific. Never tiktoken.
