# Decision tables — advanced course

Each table answers one recurring question with the options side by side. The first course's
[decision tables](../../docs/comparisons.md) cover the basics (autonomy, structured data, workflow patterns,
retrieval, guardrail placement).

## Where should an agent run's state live? [Day 1, Day 4]

| Option | You get | You still own | Choose when |
|---|---|---|---|
| In-memory loop (first course) | simplicity | everything after a crash: nothing survives | demos, single-request tasks |
| Event log in your database (`advanced/lib/durable.py`) | resume, replay, keyed effects, leases, approvals; full control | the runtime code, the sweeper, the schema | you already run a database and need control over tools and data |
| Workflow engine (Temporal, Step Functions, Durable Functions) | retries, timers, signals, history, scaling | mapping the agent loop onto activities; determinism constraints; cost of the engine | many long workflows beyond agents; an ops team that knows the engine |
| Managed Agents sessions | the loop, the sandbox, event history, budgets, hosted tools | tools that stay on your side, policy, evals, the coordinator's rules | you want Anthropic to run the loop and the container; per-session cost is acceptable |

## Exactly-once side effects [Day 1]

| Approach | Duplicate after a crash? | Cost | Notes |
|---|---|---|---|
| retry without keys | yes | none | the default failure mode |
| check-then-act | sometimes (races) | one read | two workers both check before either acts |
| keyed effect table (local) | no for done; unknown for in-flight | one row per effect | the in-flight case needs the system of record |
| idempotency key pushed downstream | no | the downstream must support it | the only complete answer; combine with the local table |

## Getting tools into context [Day 2]

| Strategy | Tokens per request | Selection accuracy | Cache | Choose when |
|---|---|---|---|---|
| all tools always | highest (every definition) | degrades past a few dozen | stable prefix, but large | < ~20 tools |
| hand-picked per route | low | high if the router is right | prefix changes per route | routes are known and stable |
| deferred + tool search | low + one search turn | high with good descriptions | deferred tools outside the prefix | large catalogs, open-ended tasks |
| code execution / PTC | one cell instead of N calls | n/a (code composes tools) | container state, not prefix | bulk, loops, aggregation |

## Changing the toolset mid-conversation [Day 2]

| Mechanism | Cache | Beta | Use |
|---|---|---|---|
| re-send a different `tools` array | invalidates from the tools block | none | rare, at conversation boundaries |
| `tool_addition` / `tool_removal` (references) | preserved | `mid-conversation-tool-changes-2026-07-01` | phases within one conversation |
| inline `tool_definition` in a `tool_addition` | preserved | `inline-tools-2026-09-15` | tools that did not exist at the start |

## Controlling reasoning over a long task [Day 3]

| Lever | Scope | Beta | Best for |
|---|---|---|---|
| `max_tokens` | one response | none | a hard ceiling per call |
| `output_config.effort` | one request | none | a fixed depth for the whole request |
| per-message effort | the following turns | `mid-conversation-output-config-2026-07-01` | changing depth as the task moves through phases |
| task budget | the whole task, across turns | `task-budgets-2026-03-13` (≥ 20,000) | pacing a multi-turn task against one budget |
| thinking display updates | presentation only | `thinking-display-updates-2026-08-18` | showing progress before tool calls |

## Keeping a 40-turn session healthy [Day 3]

| Strategy | Facts retained | Cost | Breaks binding? | Notes |
|---|---|---|---|---|
| truncation | worst (oldest facts gone) | lowest | yes (prefix edit) | last resort |
| own summarisation | depends on the prompt | one extra call | yes unless appended | you control what survives |
| server-side compaction | good, model-chosen | built in | resets the prefix (allowed) | the default for long agents |
| context editing (clear tool results) | keeps decisions, drops bulk | none | yes for edited turns | tool-heavy loops |
| structured state extraction | best for known facts | small | no (state in the prompt) | when you know the schema of what matters |

## Persistent vs turn-scoped system text [Day 3]

| Mechanism | Lifetime | Cache | Placement rule |
|---|---|---|---|
| top-level `system` | whole conversation | invalidates everything when edited | at the top |
| `{"role": "system"}` message | from that point on | preserves earlier prefix | after the message it follows |
| `clear_at: "next_user_message"` | until the next user message | 0 tokens once cleared | after the user message; never directly before an assistant turn |
| text in the user turn | one turn, but stays in history | grows the transcript | anywhere in the user message |

## Coordinating many agents [Day 4]

| Pattern | Durability | Coordination cost | Control | Choose when |
|---|---|---|---|---|
| in-process gather (first course) | none | low | full | short, parallel, disposable |
| durable coordinator + work queue | full | medium | full | multi-hour/day work; crashes expected |
| hosted coordinator (Managed Agents) | provided | medium–high (threads, per-session cost) | tools yes, loop no | when the sandbox and hosting are the point |
| single agent | n/a | none | full | the task fits the context window and the budget |

## Where security controls belong [Day 5]

| Threat | Prompt | Input screen | Tool layer | Output check | Person |
|---|---|---|---|---|---|
| instruction in a tool result | second line | — | **decides** (policy in code) | — | — |
| lookalike sender | — | **decides** | — | — | security queue |
| exfiltration via URL/parameter | — | — | recipient/URL allow-lists | **decides** | — |
| unauthorised row/tool | — | — | **decides** (scope, capabilities) | — | approvals |
| destructive bulk action | — | flags | approval gate | — | **decides** |

## Trusting an eval result [Day 6]

| Question | Tool | Rule of thumb |
|---|---|---|
| is A better than B? | paired comparison + bootstrap CI | same tickets, report the CI, not just the point estimate |
| how many cases do I need? | power analysis / MDE curve | size before you run; 10-point differences need hundreds of unpaired cases, far fewer paired |
| can I trust the judge? | kappa vs humans, position swap, identical pairs | below moderate agreement, fix the judge before using it |
| did a slice regress? | per-category gates with CIs | the average is not a gate |
| will the eval hold in production? | canary + drift monitors | the eval covers yesterday's traffic; drift tells you when it stops applying |

## Code, model or person in a multi-day campaign [Day 7]

| Decision | Owner | Why |
|---|---|---|
| who is contacted first | code (risk class, tier, SLA) | deterministic, auditable, testable |
| what a reply asks for | model (scoped run) | judgement over free text |
| whether a reply may reach the model | code (screen) | the model is not a security boundary |
| which slots fit the constraints | model proposes, code validates (region, skill, parts) | judgement inside guard rails |
| a goodwill credit above the threshold | person (durable approval) | money and policy |
| stopping the campaign | code, with a person to lift it | must hold when the model is wrong |
