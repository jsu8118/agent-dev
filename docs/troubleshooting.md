# Troubleshooting

## Setup

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: labkit` / `kestrel` | Install the repo in editable mode: `pip install -r requirements.txt` (it ends with `-e .`), and run scripts with the venv's Python. |
| `pip` resolves `anthropic` 0.x | You're on Python < 3.10. Use Python 3.10+ (the course is tested on 3.11). |
| `TypeError` when passing an `httpx` client/transport to the SDK | SDK 1.x uses **httpx2**: `import httpx2` (or `anthropic.DefaultHttpxClient`), not `httpx`. |
| Labs say MOCK MODE although you have a key | The key isn't visible to the process. `export ANTHROPIC_API_KEY=...` or put it in `.env` at the repo root. `LABKIT_MODE=live` forces live mode (and fails loudly without credentials). |
| Docker build fails on `apt-get` | The image doesn't need system packages; if you added some, check your network or proxy policy. |

## API errors you will meet

| Error | Cause | Fix |
|---|---|---|
| 400 `temperature: sampling parameters are not supported` | Claude Opus 4.7+ / Opus 5 removed `temperature`/`top_p`/`top_k` | Delete them; get consistency from structure and evals (Day 1). |
| 400 `thinking.type: "enabled" is not supported` | `budget_tokens` removed on current models | `thinking={"type": "adaptive"}` + `output_config.effort`. |
| 400 `does not support assistant message prefill` | Last message is `assistant` | Use structured outputs, or end with a user message. |
| 400 `tool_use ids were found without tool_result blocks immediately after` | You didn't answer every `tool_use` in the next user message | Return a `tool_result` for **every** `tool_use` (use `is_error: true` on failures), all in **one** user message. |
| 400 `tool_result blocks must come first` | Text placed before tool results in the user message | Put all `tool_result` blocks first; text after them. |
| 400 `Expected thinking or redacted_thinking ... When thinking is enabled` | You rebuilt the assistant turn and dropped its thinking block | Append `response.content` **verbatim**. |
| 400 `Invalid signature in thinking block` | You edited a thinking block | Never modify thinking blocks. |
| 400 `For 'object' type, 'additionalProperties' must be explicitly set to false` | Structured-output or strict-tool schema has open objects | Close every object (`messages.parse` does it for you). |
| 400 `tool_choice: type "tool" and "any" are not supported for this model` | Forced tool use on Claude Opus 5.5 / Fable 5.1 | `tool_choice` `auto` + instructions + `strict: true`, or structured outputs. |
| 400 `effort is not supported` | `output_config.effort` sent to Claude Haiku 4.5 | Check `labkit.supports_effort(model)` first. |
| 400 `requires the anthropic-beta header` | A beta parameter on the non-beta endpoint or without its beta | Use `client.beta.messages.create(..., betas=[...])`. |
| `ValueError: Streaming is required for operations that may take longer than 10 minutes` | Large `max_tokens` without streaming (raised client-side by the SDK) | Use `client.messages.stream(...)` + `get_final_message()`. |
| 404 `model: ...` | Unknown or retired model id | Use a current id (`docs/cheatsheet.md`); validate ids at startup. |
| 429 / 529 persisting after retries | Sustained rate limit / overload | Backoff with jitter, lower concurrency, batch offline work; for Opus 5 check its separate rate-limit bucket. |
| `stop_reason == "max_tokens"` with no text | Thinking used the whole budget | Raise `max_tokens` or lower `effort`. |
| `stop_reason == "refusal"` | Safety classifier declined | Branch on it; enable `fallbacks="default"` on Claude Opus 5; route to a human. |
| `cache_read_input_tokens` always 0 | Prefix changes each call (timestamps, unsorted JSON, tool changes), prefix below the model's minimum, or TTL expired | Audit the prefix; move volatile content after the last breakpoint; check the minimum (4,096 on Haiku 4.5). |

## Mock mode questions

* **"The mock gave a weird answer."** Mock answers come from rule-based scenario policies, written for each lab's main flow. If you change a prompt drastically or ask something new, the generic fallback answers politely (and schema-valid for structured outputs). Run live for real behaviour.
* **"My request works live but the mock rejects it (or the reverse)."** The mock enforces documented API rules; please report the case. `LABKIT_MOCK_STRICT=0` relaxes validation temporarily.
* **"Latency numbers are ~0 in mock mode."** Expected. Token counts and costs are simulated from list prices; latency is not.
* **"Accuracy is the same for every model in mock mode."** Expected: all models share one heuristic stand-in. Comparisons of *quality* need live mode.
