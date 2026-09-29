# Glossary — advanced course

Terms introduced in the second week. The first course's [glossary](../../docs/glossary.md) covers the basics
(agent loop, adaptive thinking, prompt caching, MCP, evals, guardrails).

**Agent-to-agent (A2A) protocol** — a typed message contract between agents (an envelope with a schema, a sender, a task id and an expected reply shape), so that delegation, handoff and broadcast are validated exchanges rather than free text. Day 4.

**allowed_callers** — a client tool property listing which code-execution tool versions may call it from code (`["code_execution_20260120"]`); without a code-execution tool in the request it is a 400. Day 2.

**Append-only rebuild** — reconstructing a resumed run's `messages` array byte-for-byte from the event log, so that prompt caches and preserved-thinking bindings stay valid. Day 1.

**Approval (durable)** — a paused run (`waiting_approval`) whose pending action is recorded so that a person in another process can decide later; the run resumes from the decision. Day 1, Day 7.

**Bradley–Terry model** — a way to turn pairwise preferences between arms into a ranking with strengths (and uncertainty), used to compare prompt or model variants judged in pairs. Day 6.

**Budget (swarm)** — a cap on what a set of agent runs may spend (dollars, tokens, time), checked at run boundaries and persisted across restarts; complemented by per-run caps and in-run guards. Day 4, Day 7.

**Canary** — routing a small share of traffic to a new version and comparing it with the baseline under a sequential test before widening the rollout. Day 6.

**Capability** — an authorisation derived from the channel, the role and the tenant, expressed as the tools a run may call, the rows it may touch and the approvals it needs; enforced in the tool layer, never in the prompt. Day 5, Day 7.

**Circuit breaker** — a guard that stops calling a tool (and tells the model to escalate) once the error rate over a window crosses a threshold. Day 4.

**clear_at** — `{"role": "system", ..., "clear_at": "next_user_message"}`: a turn-scoped system message that renders nothing once a later user message exists; beta `mid-conversation-system-clear-at-2026-08-21`. Day 3.

**Code execution** — the server tool (`code_execution_20260120` and other versions) that runs model-written Python, bash and file edits in a container the API hosts; results come back as `*_code_execution_tool_result` blocks. Day 2, Day 5.

**Compaction strategies** — truncation, own summarisation, server-side compaction, context editing (clearing tool results) and structured state extraction, compared by retained facts and cost. Day 3.

**Container** — the per-conversation sandbox of the code-execution tool (`response.container.id`, `expires_at`); passed back as `container=` to continue a paused cell. Day 2.

**Coordinator (hosted)** — a Managed Agents agent with `multiagent: {"type": "coordinator", "agents": [...]}` that delegates to workers (one level) through threads. Day 4.

**Defer loading** — `defer_loading: true` on a tool: it costs no tokens and cannot be called until tool search discovers it (or a `tool_addition` surfaces it). Day 2.

**Drift detection** — statistics (PSI, KS) on the distribution of inputs and outputs over time to notice that traffic or behaviour has moved away from what the eval covered. Day 6.

**Durable run** — an agent run whose every step is written to a log before it is acted on, keyed for idempotency, leased to one worker at a time, and resumable by any worker. Day 1.

**Eager input streaming** — `eager_input_streaming: true` on a client tool so large inputs stream as `input_json_delta` while generated; the client validates the parsed input before acting. Day 2.

**Effect (keyed)** — a side effect recorded under an idempotency key with the states *started* → *done*; a resumed run replays a done effect and treats a started one as unknown. Day 1.

**Event sourcing** — keeping the run's state as an append-only sequence of events (`model.response`, `tool.started`, `tool.result`, …) from which the current state is rebuilt. Day 1.

**Exfiltration channel** — any path that can carry data out of the system: markdown image/link URLs, tool parameters, memory writes, outbound email, logs. Day 5.

**Forensics** — reconstructing, from the durable log and the audit trail, what the agent read, what it decided and who told it what. Day 5, Day 7.

**Hard negative** — a benign input that resembles an attack (a customer saying "ignore my previous email"); the cases that measure a filter's false positives. Day 5.

**Heartbeat** — periodically extending a run's lease while a worker is still working, so a slow worker is not mistaken for a dead one. Day 1.

**Hill-climbing (prompt)** — iterating a prompt against a training split and validating on a holdout, with a stopping rule, so improvements are real rather than memorised. Day 6.

**Idempotency key** — a deterministic identifier for one intended side effect (run id + tool_use id), passed to the system of record so that a retry is a no-op. Day 1.

**Injection defence in depth** — layered controls (input classification, data/instruction separation, tool-layer policy, output checks, human gates), each measured on attacks and hard negatives. Day 5.

**input_transformations** — a response field listing what the API changed before generation (`thinking_dropped`, `thinking_mismatch_allowed`, with paths), present under `thinking-binding-controls-2026-08-01`. Day 3.

**Inline tool definition** — a `tool_addition` block carrying a full `tool_definition`, under `inline-tools-2026-09-15`. Day 2.

**Lease** — a time-limited claim on a run by one worker (`acquire` / `heartbeat` / `release`); an expired lease may be taken over. Day 1.

**Lookback (cache)** — the API checks the last 20 cache positions for a hit; consecutive tool_use/tool_result runs count as one position. Day 3.

**Loop detection** — a guard that notices the same tool call repeated with the same input and stops it. Day 4.

**Managed Agents** — the hosted runtime (`client.beta.agents`, `environments`, `sessions`, `sessions.events`) that runs the loop and the sandbox for you; you keep tools, policy and evals. Day 4.

**Migration audit** — the checklist run when moving to a new model: prompt cruft, removed or changed parameters, preserved-thinking compatibility, evals. Day 6.

**Model binding** — thinking blocks are signed to the producing model family; a model that cannot read them drops them. Day 3.

**pass^k** — the probability that all k independent runs of a task succeed; the consistency metric for agentic tasks. Day 6.

**Per-message effort** — `{"role": "system", "content": [], "output_config": {"effort": "low"}}` under `mid-conversation-output-config-2026-07-01`; the latest override wins for the following turns. Day 3.

**Position bias** — a judge's preference for an answer because of where it appears; measured with identical pairs and cancelled by swapping orders. Day 6.

**Pre-warming** — a `max_tokens: 0` request that writes the cache before a fan-out, so the concurrent requests read instead of all writing. Day 3.

**Prefix binding** — thinking signatures bound to the conversation prefix; enforced on Fable 5.1 and, for accounts created on or after 2026-08-31, on Opus 5.5 (older accounts: recorded, opt-in - the behaviour the mock models); breaking edits are a 400 or a dropped block (`prefix_mismatch_behavior`). Day 3.

**Programmatic tool calling (PTC)** — the model calls client tools from code (`await tool_name({...})`); the response pauses with `caller`-tagged `tool_use` blocks and resumes when their results arrive. Day 2.

**Quarantine** — routing a message to a human queue without any model call because a security screen matched. Day 5, Day 7.

**Recorded vs enforced binding** — on older accounts Opus 5.5 lists prefix mismatches as `thinking_mismatch_allowed` unless `prefix_mismatch_behavior` is set (recorded); Fable 5.1, and Opus 5.5 on accounts created on or after 2026-08-31, reject them (enforced). Day 3.

**Replay** — feeding a run's logged tool results back into a new run to reproduce a bug offline; a *fork* changes one result to explore an alternative. Day 1.

**Rug pull (MCP)** — a server whose tools or descriptions change after it was trusted; caught by pinning, signatures and diffing manifests. Day 5.

**Saga** — a multi-step business transaction with a compensating action per step, run forward and, on failure, backward. Day 1.

**Scoped desk / toolset** — a tool executor created for one role and one customer, refusing everything outside the scope. Day 2, Day 5, Day 7.

**Sequential test** — a statistical test designed to be checked repeatedly as data arrives (canaries) without inflating false positives. Day 6.

**Simpson's paradox** — an aggregate that improves while a slice regresses; the reason release gates are per slice. Day 6.

**Stop condition** — a rule that pauses a campaign or a swarm in code (safety event, exhausted resources, budget), with a named owner who lifts it. Day 7.

**Stuck run** — a run marked running whose lease expired: a worker died; sweepers resume or fail it. Day 1.

**Task budget** — `output_config.task_budget: {"type": "tokens", "total": N}` (N ≥ 20,000; beta `task-budgets-2026-03-13`) that the model paces its thinking against across turns. Day 3.

**Tenant** — an isolation boundary (customers, users, roles) that capabilities and row filters are derived from. Day 5.

**Tool search** — the server tools `tool_search_tool_regex_20251119` / `tool_search_tool_bm25_20251119` that let the model discover deferred tools; results arrive as `tool_search_tool_result` blocks with `tool_reference`s. Day 2.

**Tool selection eval** — a labelled set of tasks → expected tool, scored across description variants and toolset sizes. Day 2.

**Work queue** — a table of claimable tasks with statuses, claims and retries that workers pull from; the durable alternative to in-process fan-out. Day 4.
