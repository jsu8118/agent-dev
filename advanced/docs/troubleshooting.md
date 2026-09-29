# Troubleshooting — advanced course

The first course's [troubleshooting page](../../docs/troubleshooting.md) covers setup and the common API
errors (tool_result pairing, thinking blocks, strict schemas, forced `tool_choice`). This page adds the errors
the advanced surfaces produce — in the real API and, identically, in the mock.

## Betas and shapes

| Error | Cause | Fix |
|---|---|---|
| 400 `... requires the anthropic-beta header mid-conversation-tool-changes-2026-07-01` | a `tool_addition` / `tool_removal` block without the beta | `client.beta.messages.create(..., betas=["mid-conversation-tool-changes-2026-07-01"])`; inline definitions also need `inline-tools-2026-09-15` |
| 400 `messages.N.clear_at: Extra inputs are not permitted (requires the anthropic-beta ...)` | `clear_at` without its beta | add `mid-conversation-system-clear-at-2026-08-21` |
| 400 `clear_at ... followed by an assistant message` | the turn-scoped reminder sits directly before an assistant turn | place it after the user message it applies to; it is the last message before the model answers |
| 400 `output_config.effort in a system message requires mid-conversation-output-config-2026-07-01` | per-message effort without the beta | add the beta; use Opus 5 or Fable 5.1 |
| 400 `... does not support per-turn effort` | per-message effort on Fable 5 (or another model that accepts system messages but not per-turn effort) | switch model or use request-level `output_config.effort` |
| 400 `task_budget ... requires task-budgets-2026-03-13` | budget without the beta | add the beta |
| 400 `task_budget.total must be at least 20000` | budget too small | ≥ 20,000 tokens |
| 400 `thinking.block_binding: Extra inputs are not permitted` | `prefix_mismatch_behavior` without the beta | add `thinking-binding-controls-2026-08-01` |
| 400 `thinking.block_binding.prefix_mismatch_behavior: Input should be 'error' or 'drop_block'` | typo | one of the two values |

## Tool search, code execution, programmatic tool calling

| Error | Cause | Fix |
|---|---|---|
| 400 `tools: at least one tool must not be deferred` | every tool has `defer_loading: true` | keep a core set loaded |
| 400 `tool_result for srvtoolu_...` / `tool_use ids were found without tool_result` involving a `srvtoolu_` id | you answered a server tool use | never send `tool_result` for server tools; only answer client `tool_use` blocks |
| the model "calls" a deferred tool and the API rejects it | the tool was never discovered | make sure the search ran first (`tool_search_tool_result` with a `tool_reference` for it) or surface it with a `tool_addition` |
| `tool_search_tool_result_error` with `invalid_tool_input` | bad regex (unbalanced parenthesis) | the model retries; in a policy, fix the query |
| 400 `allowed_callers requires a code execution tool` | `allowed_callers` on a client tool without `code_execution_*` in `tools` | add the code-execution tool |
| 400 `container is required to continue a paused code cell` | continuation without `container=` | pass `container=first.container.id` |
| 400 `the continuation must contain only tool_result blocks` | text mixed into the continuation user message | only `tool_result` blocks for the paused calls |
| the cell "hangs" in the mock | the code awaited a tool that is not in `allowed_callers` | the tool must list the code-execution version in `allowed_callers` |
| container expired | `container.expires_at` passed | start a new cell; containers are not forever |

## Preserved thinking

| Error | Cause | Fix |
|---|---|---|
| 400 `prefix_mismatch` on `claude-fable-5-1` | history before a thinking block was edited (reworded message, dropped tool result, changed system prompt or tool names) | append-only edits; or `thinking: {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}` under the beta and read `input_transformations` |
| `input_transformations` lists `thinking_dropped` after a model switch | blocks from Opus 5.5 sent to Opus 5 | expected: Opus 5 cannot read them; Fable 5.1 can read Opus 5.5's blocks |
| `thinking_mismatch_allowed` on Opus 5.5 | recorded binding: the mismatch is listed, not enforced | fix the edit anyway; it becomes an error when you set `prefix_mismatch_behavior` |
| cache misses after a resume | the rebuilt messages differ from the sent ones | rebuild from the log byte-for-byte (`DurableRunner.rebuild`) |

## Managed Agents (mock)

| Error | Cause | Fix |
|---|---|---|
| 400 `the session is waiting for user.custom_tool_result events` | you sent a `user.message` while `requires_action` is pending | answer the listed `event_ids` first |
| 400 `custom_tool_use_id: no pending custom tool use` | wrong or already-answered id | use the `agent.custom_tool_use` event's id from the idle event |
| 400 `... one level` | a coordinator's roster contains a coordinator | rosters hold workers and `{"type": "self"}` only |
| `session.status_idle` with `budget_reached` | the session's `max_list_cost` was hit | raise the budget or start a new session |
| 400 `session is archived (read-only)` | events sent to an archived session | create a new session |

## Durable runtime and the capstone

| Symptom | Cause | Fix |
|---|---|---|
| `RuntimeError: run ... is leased by another worker` | a live worker holds the lease | wait for its heartbeat to stop (or its lease to expire); never force |
| a run stays `running` forever | the worker died | `RunStore.stuck(older_than_s=...)`; resume it (`run_ops.py --resume`) |
| a resumed run re-executes a tool | the result was never logged (crash before `tool.result`) | expected; make the tool's effect keyed (`ctx.effect`) so re-execution is a no-op |
| an effect is `in_flight` on resume | the worker died between the downstream call and the commit | look the key up in the system of record, then `commit` or execute |
| `decide()` seems to do nothing | deciding an approval twice | it is idempotent; the first decision stands |
| a run asks for approval again after a decision | the decision was recorded but the run was resumed by code that ignores it | `run()` (and `resume_after_decision()`) answer settled approvals before continuing; do not re-create the run or re-execute the gated tool yourself |
| `LeaseLost` while a run is executing | another worker took the run over after this worker's lease expired (it was too slow to heartbeat) | stop the worker's work (the runtime already did); shorten the work between heartbeats or lengthen `lease_ttl_s` |
| the campaign pauses immediately with a budget stop | the persisted spend (`spend_usd`) already exceeds the cap | `--fresh` for a new campaign, or raise the cap deliberately |
| `outbound blocked by the output guard` | the email names another customer or promises compensation | rewrite; the guard is right |
| `slot is in region ...` / `lacks the skill ...` | booking outside the customer's region or without the remedy's skill | ask `get_engineer_slots` for the right region/skill; out-of-region visits need approval |

## Mock-specific

| Symptom | Fix |
|---|---|
| `[mock] ... no scenario policy matched` | the lab's system prompt marker does not match any policy in `advanced/mock_scenarios/`; check the tag and the tool names the policy expects |
| a number in a README differs from the output | the mock's accounting changed or the lab changed; re-run and re-quote (the excerpt check in CI compares them) |
| concurrent requests all write the cache | intended: set `mock_api().cache.ready_delay = 0` to make writes instantly readable, or pre-warm as the lesson teaches |
