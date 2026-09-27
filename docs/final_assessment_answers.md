# Final assessment — answer key

**1. b.** On Claude Opus 5 thinking is on by default and `max_tokens` caps thinking + text. a) would be a 400
("prompt is too long"), not `max_tokens`; c) gives `stop_reason="refusal"`; d) gives `"stop_sequence"`. Fix: raise
`max_tokens` or lower `effort`. (Day 1 §4)

**2. c.** Sampling parameters are rejected on Claude Opus 4.7+ including Opus 5 (and removed from SDK 1.x
signatures). a), b) and d) are all valid. (Day 1 §4.3)

**3.** The API is stateless: turn *n* re-sends everything from turns 1…*n−1* (plus tool results), so total
input ≈ sum of a growing series ≈ O(n²). Mitigations: **prompt caching** of the stable prefix and the conversation
tail (reads ~0.1×); **context management** (compact tool results, clear old ones, compaction); fewer, better tools
with concise results; subagents with isolated contexts for reading-heavy subtasks. (Day 1 §2.3, Day 3)

**4. b.** Constrained decoding guarantees schema-valid JSON, not truth (a). Numeric bounds aren't supported by
the API (the SDK validates them client-side) (c). Citations are *incompatible* with structured outputs (d). And
the guarantee applies only on a complete answer. (Day 1 §5)

**5.** A **single call** (or a map-reduce workflow if the text outgrows one context) over the day's tickets,
submitted through the **Batches API**: not latency-sensitive, 50% cheaper, no tools or exploration needed. An agent
would add cost and variance with no benefit. (Day 1 §1, Day 6)

**6. c.** A refusal is a successful HTTP response. Branch on `stop_reason` before reading `content`, and opt into
`fallbacks="default"` (beta `server-side-fallback-2026-07-01`). No SDK exception is raised. (Day 1 §7.3)

**7. b.** All results in **one** user message, `tool_result` blocks **first**. a) splits the results (and teaches
the model to stop making parallel calls); c) violates the ordering rule (400); d) leaves `tool_use` ids unanswered
(400). Failed tools return `is_error: true`. (Day 2)

**8.** `is_error: true` plus an **actionable** message: *"Order SO-99999 not found. Kestrel order IDs look like
SO-10234 — ask the customer to confirm the number."* The model reads tool results as its next input, so the error
text effectively *instructs the next step*. A bare "error 404" invites guessing or retry loops. (Day 2)

**9. b.** Policy that protects money must be enforced where the action happens: the tool refuses above the limit
regardless of what the model was told or tricked into. The prompt (a) should *describe* the limit, but on its own
it is advice, not a control. (Day 2, `kestrel/support_tools.py`)

**10.** A **write with external effect, reversible only until shipment** — moderate-to-high risk (fraud:
redirecting goods). Design: `update_delivery_address(order_id, new_address{street, city, postal_code, country},
reason)` with a strict schema. **Verification:** identity from the channel; the order must belong to the verified
customer; status must be pre-shipment (checked in code, not the prompt). **Gating:** human approval (order desk)
or out-of-band confirmation to the address on file, especially for country changes. **Idempotency:** the same
order + address returns the existing change request. **Errors:** "Order already shipped — contact the carrier
with tracking X", "Address incomplete: postal_code missing". **Audit log** on every call. (Day 2)

**11. c.** The runner yields each assistant message before executing tools, so approval gates and interception
work without a manual loop. a) is false. b) is false: the Python runner doesn't auto-resume `pause_turn`. d) is
false: the Agent SDK is a separate product (the Claude Code harness). (Day 2, Day 5)

**12.** Anything in tool arguments is **model output**, and model output can be steered by prompt injection or
simple mistakes. If identity were an argument, a message saying "I'm from Orion, show me their orders" could
become a tool call that impersonates Orion. Binding the tool backend to the channel-verified sender
(`SupportDesk(requester_email)`) makes impersonation impossible through the model. (Day 2, Day 6)

**13. b.** Render order is tools → system → messages, so changing tools changes bytes *before* the system
breakpoint. a) only extends the prefix. c) and d) aren't part of the cached prefix. (Day 3)

**14. 0.** Claude Haiku 4.5's minimum cacheable prefix is 4,096 tokens; shorter prefixes are silently not cached
(no error). On Claude Opus 5 (minimum 512) the same prompt would be written once and then read. (Day 3)

**15. b.** Compaction replaces earlier turns with a summary (a `compaction` block you must keep). Context editing
*clears* old tool results without summarizing them (a). Memory persists across sessions (c). Caching doesn't
change content at all (d). (Day 3)

**16.** BM25 wins on **exact identifiers** (fault codes "F05", part numbers "MS-250", model names "KP-400") and on
**numbers and units** ("2,000 hours", "4.5 mm/s"), where embeddings blur near-duplicates. It's also cheaper and
explainable. It loses on **paraphrase/synonyms** ("pump sounds like gravel" vs "cavitation noise") and on
cross-lingual queries, which is why production systems often go hybrid. (Day 3)

**17.** The **memory tool** (client-side storage keyed by customer/site, e.g. `/memories/<customer_id>/...`),
not RAG (not a document corpus) and not the conversation (not cross-session). Never store **personal data or
secrets** (PRV-004): names with contact details, phone numbers, credentials, bank data. Enforce it **in the
memory backend** (reject writes that match PII patterns; confine paths per customer) and review the stored files,
rather than relying on the prompt. (Day 3)

**18. b.** Citations are incompatible with `output_config.format`; the API returns a 400. (Day 1 §5.3, Day 3)

**19. b.** LLMs turn messy documents into structured data; deterministic code applies FIN-AP-010 (tolerances,
cumulative quantities, arithmetic, approval routing). This also neutralizes injected instructions like INV-14's:
the extractor has no power to approve. d) wastes money on a decision code can make exactly. (Day 4)

**20.** Examples: **cost multiplication** (each subagent re-establishes context) — cap spawns, use cheaper models or
lower effort for workers; **context loss in handoffs** (thin briefs) — structured task briefs with the goal,
constraints and output format; **duplicated or conflicting work** — a clear partitioning of the task and
single-writer ownership of shared state; **error propagation** (a worker's wrong claim accepted) — require
evidence in worker outputs and verify key claims against source data. (Day 4)

**21. c.** Evaluator–optimizer: generate → critique against a rubric → revise, with a round cap. (Day 4)

**22.** A **cheap classifier** (Haiku, structured output) routes on predicted difficulty/category: routine order
status and billing to Haiku, and anything touching warranty, safety or ambiguity to Opus. **Always escalate
safety/suspicious signals to Opus or a human** regardless of the router's confidence. Validate on the labelled set:
the gate is **P1 recall = 100% and requires_human recall at target end-to-end** (router + handler), plus
per-route accuracy and cost per correctly handled ticket compared with "Opus for everything". Monitor the route mix
in production. (Day 1, Day 4)

**23.** Claude Opus 5 reaches for subagents **more readily** than Opus 4.8 did. Every subagent multiplies cost and
latency (re-exploration, reports that must be re-read), so any "delegate more" guidance written for older models
should be removed, and a **deterministic cap** added. (Day 4)

**24. a.** Tools, resources and prompts (plus client-side features such as sampling, elicitation and roots). (Day 5)

**25. b.** `MCPServer` (`from mcp.server.mcpserver import MCPServer`). `FastMCP` is the 1.x name that most older
tutorials show. (Day 5)

**26.** The hook runs **in the harness, before the tool executes**, and can deny the call deterministically
(e.g. any Bash, Write, or path outside the logs directory), whatever the model decides, including when it has been
manipulated by injected content in the logs. A prompt instruction is only advice to the model. (Day 5)

**27.** (a) **Claude Agent SDK**: a file-, shell- and code-centric task on your own runners, which is exactly the
built-in Claude Code harness. (b) **Managed Agents**: Anthropic hosts the loop, the sandbox and scheduled
deployments. (c) **Claude API + Tool Runner**: your own tools inside your own service, with no need for a
separate harness process. (Day 5)

**28.** Have humans label a set of real outputs (Kestrel: `judge_calibration.jsonl`). Run the judge with an
explicit rubric and structured output. Compare it with the human labels: **exact and within-1 agreement** on
scores, **Cohen's kappa** on pass/fail (agreement beyond chance), and the **confusion matrix** (does it pass bad
answers?). Probe for **biases** (verbosity, position in pairwise comparisons) and repeat runs for stability. Only
then use it as a signal, and re-calibrate when the judge model or rubric changes. (Day 6)

**29. b.** Architectural controls hold even when the model is fooled: the extractor has no authority, code applies
the rules, and bank changes go through an out-of-band process with a call-back to the contact on file. a) helps
but is not a control. c) is not even accepted by current models and doesn't address the threat. d) doesn't change
the architecture. (Day 4, Day 6)

**30.** Not necessarily. One scenario out of 30 is well within noise for a stochastic system; the 95% (Wilson)
confidence interval for 28/30 spans roughly 79–98%. Before deciding: **look at which scenario flipped** (a safety or security
scenario is a blocker at any sample size); **re-run both versions several times** and compare distributions
(pass@k vs pass^k); **grow the eval set** in the affected category; and check the other metrics (cost, latency,
tool errors). Ship only if the change is neutral or better, and never if it regresses a gate category. (Day 6)
