# Final assessment — answer key (advanced course)

## A. Durable agents (Day 1)

**1. b.** The effects table shows the step *started* but not *done*; the resumed run cannot know whether the refund API received the call, so it asks the system of record by idempotency key and commits what it finds. (a) is what happens without a key; (c) loses a legitimate refund; (d) is what a runtime without effect tracking must do to stay safe.

**2. c.** The log gives replay, resumption and an append-only rebuild. Exactly-once effects need *keyed* effects and a downstream system that honours the keys; the log only records that a step began. (Day 1, lab 03.)

**3.** Prompt caching matches the byte-identical prefix of the previous request, and preserved-thinking prefix binding (enforced on Fable 5.1, recorded on Opus 5.5) signs thinking blocks to the conversation prefix. A "functionally equivalent" rebuild invalidates the cache and breaks the binding (a 400, or dropped blocks). That is why `DurableRunner.rebuild()` re-creates the exact assistant turns and tool-result messages from the log.

**4. b.** Leases protect against dead workers, not slow ones; heartbeats keep a live worker's lease alive, and keyed effects make the rare duplicate harmless. Shorter leases (a) make the race more likely; longer ones (d) delay takeover after a real death; a process lock (c) does not span workers.

**5.** (iii). Blocking a worker for days wastes a worker and dies with a deploy; `tool_choice`-driven asking still lives inside one process's conversation. A durable approval parks the run (`waiting_approval`) with a record containing the action (tool name, input, amount, summary), the approver role required, who requested it and when, and later who decided, when, and the note — enough for a manager in another process to decide and for an audit to explain the credit.

**6.** Only the two regenerated turns are billed: input 2 × 8,000 = 16,000 tokens → $0.08; output 2 × 400 = 800 tokens → $0.02. Total ≈ **$0.10**. Rebuilding the first ten turns from the log costs no tokens at all.

## B. Tools at scale (Day 2)

**7. b.** Deferred tools cost nothing until discovered; the six always-loaded tools and the search tool are in the prefix. (c) is wrong because non-deferred tools are always sent; (d) does not exist.

**8. b.** Server tool results (`tool_search_tool_result`) are attached by the API inside the same assistant turn; sending a `tool_result` for a `srvtoolu_` id is a 400. The discovered tools are referenced through `tool_reference`s, not re-declared (d).

**9. b.** A `tool_addition`/`tool_removal` under `mid-conversation-tool-changes-2026-07-01` changes availability from that point on without touching the tools block. Changing `tools` (a, c) or `tool_choice` (d) invalidates the prefix from the tools block onward.

**10.** A user message containing **only** `tool_result` blocks for the paused calls, and the `container` id from the paused response (`container=response.container.id`). Without the container the API returns a 400: the cell's state lives in that container and cannot resume elsewhere; containers also expire (`container.expires_at`).

**11.** (a) 11 × 4,500 = 49,500 input → $0.2475; 11 × 150 = 1,650 output → $0.04125; total ≈ **$0.289**. (b) input 6,000 + 6,800 = 12,800 → $0.064; output 700 + 300 = 1,000 → $0.025; total ≈ **$0.089**. Reasons (b) may still be wrong: the code path moves logic the model writes into an execution you must sandbox and review (Day 5), the cell can fail in ways a round trip cannot, latency of container start, and the per-call results are not individually visible to the model unless printed.

**12. b.** The SDK's tolerant parsers can return a silently truncated input; the client owns validation, treats a failure like invalid JSON, and checks `max_tokens`/`refusal` before running tools. Leave it off for server tools.

## C. Long-horizon context (Day 3)

**13. b.** `total` must be at least 20,000 tokens; the mock and the API both reject 15,000 with a 400. `max_tokens` is separate (c).

**14. b.** Turn-scoped system messages go after the user message they apply to (tool results first, then the reminder) and are rejected when directly followed by an assistant turn; once a later user message exists they render nothing.

**15.** The signed thinking block no longer matches the prefix: on Fable 5.1 the request fails with a `prefix_mismatch` 400. With `thinking.block_binding.prefix_mismatch_behavior: "drop_block"` under `thinking-binding-controls-2026-08-01`, the API drops the block and continues; the harness learns what was dropped from `response.input_transformations` (`{"type": "thinking_dropped", "path": ...}`). Opus 5.5 records instead of enforcing (`thinking_mismatch_allowed`) unless the behaviour is set.

**16. b.** Blocks are bound to the producing model family; Opus 5 cannot read Opus 5.5's, so they are dropped and listed. Fable 5.1 can read Opus 5.5 blocks. (a) is what happens for a prefix mismatch on Fable 5.1, not for a model switch.

**17.** (a) A written entry is readable only once its writer has started responding, so all eight write: 8 × 30,000 × 1.25 = 300,000 token-equivalents → $1.50. (b) One write (30,000 × 1.25 = 37,500) + eight reads (8 × 30,000 × 0.1 = 24,000) = 61,500 → $0.3075. Pre-warming saves about $1.19 per fan-out.

**18. c.** Consecutive `tool_use`/`tool_result` runs count as one position for the 20-position lookback, which is why an agent's long tool sequence does not push its cached prefix out of reach.

## D. Orchestration at scale (Day 4)

**19. b.** A conditional update is atomic in the database and tells you whether you won the claim (`rowcount == 1`). Select-then-update (a) races; a Python lock (c) does not cover other processes; a delay (d) is not a claim.

**20.** Dollars per swarm and per agent (from the ledger), tokens per run (`max_tokens` × `max_turns`), wall-clock/time per task, plus tool-error rate (circuit breaker) and repeated-call loops. "Tokens per request" bounds one call; a swarm multiplies calls across agents and turns, so the cap must live at the run and campaign level and be persisted across restarts.

**21. b.** `requires_action` names the custom tool uses waiting for results; the client answers them with `user.custom_tool_result` events keyed by `custom_tool_use_id`. Only then does the session continue.

**22. b.** One delegation level: workers plus optionally `self`; a coordinator whose roster contains another coordinator is rejected.

**23.** Measure per architecture on the same 11 units: task success per unit, total cost and cost per unit, coordination tokens (lead reading reports, re-sent prefixes), largest prompt, wall-clock, and failure attribution (which agent failed, from traces). A defensible design: a durable coordinator with a work queue, workers scoped to one unit each with a summary contract, a swarm budget; hosted (Managed Agents) only if the sandbox and state management are worth the loss of control over tools and the per-session cost. Decide with the table, not with the demo.

## E. Security engineering (Day 5)

**24. b.** Data returned by a tool is data whatever it says; the model is not a security boundary. Fees are policy in code (Day 2's lesson) so nothing the model reads can change them. (a) and (c) are additional lines, not the boundary.

**25. b.** Markdown-image exfiltration: the rendered URL carries data out. The remedy is output sanitisation (strip or neutralise URLs with data) — a verified sender does not make the channel safe.

**26.** It uses the same words as an override ("ignore my previous …") with an entirely benign meaning; a keyword filter quarantines it. Measuring detection on attacks alone hides this cost: measure false positives on hard negatives too, and price them (a person handling a false quarantine) against the cost of a miss.

**27. b.** Signature mismatch on a trusted name plus a new parameter that asks for the conversation is the textbook rug pull; block, pin, diff, review. Publisher identity (c) is exactly what a mismatched signature calls into question.

**28.** Toolset: field_service, products, knowledge (+ fleet for the lead), read-only for engineers; row filter `customer_id in tenant.customers` on every read and write; denied tools `get_customer`, `list_contacts`, `list_customer_sites` for engineers; approvals for any write outside field service; enforced in the tool layer (the desk), not in the prompt, and derived from the channel's authenticated identity. The model can never be trusted to enforce which rows it may read or which recipients it may write to.

## F. Evaluation science and release engineering (Day 6)

**29.** Paired: each ticket is answered by both arms, so ticket difficulty cancels out; the test is on per-ticket differences (win/lose/tie), which has far less variance than two independent proportions. A rough sizing: for a 10-point difference with mostly concordant pairs, on the order of 80–150 paired tickets reach 80% power; unpaired proportions at 70% vs 80% need roughly 300 per arm. Assumptions: discordant pairs around 25–30% and α = 0.05 (two-sided); state yours and use the bootstrap in lab 02 for the actual number.

**30. b.** pass^k is the probability that *all* k runs succeed — the consistency an agentic task in production needs; pass@k (a) is the optimistic "at least one".

**31. b.** Position bias is measured with identical pairs; the fix is to judge both orders and accept only consistent verdicts (or average). A bigger model (c) may still be biased; scores (d) have their own drift.

**32.** Simpson's paradox (an aggregate improvement hiding a slice regression). The slice: category × prompt version (return_request under v15). The gate: per-category pass rates with confidence intervals and a rule that no critical category may fall by more than a threshold (e.g. 5 points) versus the baseline, whatever the average does.

**33. b.** Forced `tool_choice` (`any`/`tool`) is a 400 on Opus 5.5 and Fable 5.1; prompt text written for an older model is cruft that the migration guide audits; preserved-thinking binding differs between the two models (recorded vs none), so harness edits that were harmless may start dropping blocks.

## G. The capstone (Day 7)

**34. b.** `security.screen_inbound` flags the lookalike domain (`mail-` + a contact domain) and the redirection pattern, quarantines the reply and escalates to the security queue with zero model calls; gate G1 checks exactly this.

**35.** The resumed run rebuilds the messages from the log, sees the `send_email` tool call without a logged result, and executes it again — where two mechanisms stop a second email: the effect record (`ctx.effect(key)`) is *in flight* or *done* with the stored result, and the message id derived from the idempotency key hits the store's primary key (`INSERT OR IGNORE`), so `record_message` returns False. The run then logs the (replayed) result and continues to its final turn.

**36.** Safety event: enforced by the orchestrator in code before any model call (pause the lot through the orchestrator's desk, P1 escalation to quality); lifted by the quality lead with `run_ops.py --resume-lot`. Parts exhausted: end-of-day stop condition in the orchestrator (pause the remedy, escalate to field service); lifted with `--resume-remedy` after stock is transferred. Budget: checked before every run against the persisted ledger (pause everything, escalate); lifted by raising the cap deliberately and `--resume-campaign`. None is left to the inbound agent because each must hold even when the model is wrong, must be audited, and must be visible to the people who own the decision.
