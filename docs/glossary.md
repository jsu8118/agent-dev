# Glossary

**Adaptive thinking** — `thinking={"type": "adaptive"}`: the model decides per request whether and how much to reason before answering, and interleaves reasoning between tool calls. On by default on Claude Opus 5 and Sonnet 5.

**Agent** — a system in which the model directs its own process and tool use in a loop until the task is done. Contrast *workflow*.

**Agent loop** — call the model → if `stop_reason == "tool_use"`, execute the requested tools → send the results back → repeat until `end_turn` (or a budget is hit).

**Agentic search** — retrieval performed by the model through tools (search, read section, query), in several refining steps, instead of one up-front retrieval.

**Anchor (course data)** — a named scenario in the dataset (e.g. `late_customs`) with fixed, consistent IDs across the database, tickets and evals; see `data/anchors.json`.

**Approval gate / human-in-the-loop** — a checkpoint where a person must approve an action (usually an irreversible one) before a tool executes it.

**Augmented LLM** — a single model call enhanced with retrieval, tools and/or memory; the building block of workflows and agents.

**Batches API** — `client.messages.batches.*`: asynchronous processing of many requests at 50% of standard prices; results in any order, keyed by `custom_id`.

**BM25** — a classic lexical ranking function; strong on exact terms such as part numbers and fault codes.

**Cache breakpoint** — a `cache_control` marker on a content block; the prefix up to it becomes cacheable. At most 4 per request.

**Citations** — with `"citations": {"enabled": true}` on document blocks, response text blocks carry `citations` pointing at the exact source spans.

**Claude Agent SDK** — `claude-agent-sdk`: the Claude Code harness (agent loop, built-in file/shell/search tools, hooks, subagents, permissions) as a library you host yourself.

**Compaction** — server-side summarization of earlier conversation turns when the context grows past a trigger (`compact_20260112`, beta). The response carries a `compaction` block you must keep.

**Constrained decoding** — generation restricted to tokens that keep the output valid against a grammar/JSON Schema; the mechanism behind structured outputs.

**Content block** — one typed element of a message's `content`: `text`, `thinking`, `tool_use`, `tool_result`, `image`, `document`, `compaction`, `fallback`, …

**Context editing** — server-side clearing of old tool results or thinking blocks (`clear_tool_uses_20250919`, `clear_thinking_20251015`, beta `context-management-2025-06-27`).

**Context engineering** — deciding what goes into the model's context window, in what form and order, and what stays out: prompts, tools, history, retrieved data, memory.

**Context window** — the maximum number of input tokens a model accepts (1M on current Opus/Sonnet models, 200K on Haiku 4.5).

**Effort** — `output_config={"effort": ...}` (`low`…`max`): how thoroughly the model works (thinking depth, number of tool calls, verbosity of work). The primary quality/cost dial.

**Evaluator–optimizer** — a workflow pattern in which one call generates and another critiques against a rubric, in a loop.

**Fallbacks (server-side)** — `fallbacks="default"` with beta `server-side-fallback-2026-07-01`: on a safety refusal, the API re-runs the request on a fallback model within the same call.

**Hook (Agent SDK)** — a callback the harness runs at lifecycle points (e.g. `PreToolUse`, `PostToolUse`) to allow, deny, modify or log actions.

**Idempotency** — an operation that can be repeated without changing the result beyond the first application (e.g. re-creating the same RMA returns the existing one). Essential under retries.

**Indirect prompt injection** — malicious instructions arriving through data the agent reads (documents, tool results, emails, invoices) rather than from the user directly.

**LLM-as-judge** — using a model to grade outputs against a rubric; must be calibrated against human labels.

**Managed Agents** — Anthropic-hosted agents: persisted agent configs, sessions with sandboxed containers, and Anthropic running the loop.

**MCP (Model Context Protocol)** — an open protocol for connecting AI applications (hosts/clients) to tools, resources and prompts exposed by servers, over stdio or Streamable HTTP.

**Memory tool** — `memory_20250818`: a client-side tool through which the model reads and writes files in a `/memories` directory that persists across conversations.

**Mock mode (course)** — running labs against `labkit`'s offline mock of the Claude API, which validates requests like the real API and answers through rule-based scenario policies.

**Orchestrator–workers** — a pattern in which a lead model plans and delegates subtasks to worker calls or agents, then synthesizes their results.

**`pause_turn`** — a stop reason for long-running server-side tool loops; re-send the conversation (including the paused assistant turn) to continue. The Python Tool Runner (SDK 1.8) does this automatically; a manual loop must do it itself.

**Prefill (assistant)** — ending `messages` with a partial assistant turn to steer the output format. Rejected (400) on current models; use structured outputs.

**Prompt caching** — reuse of a byte-identical prompt prefix across requests; writes cost 1.25× (5 min) or 2× (1 h), reads ~0.1×.

**Refusal** — `stop_reason == "refusal"` (HTTP 200): a safety classifier or the model declined; `stop_details.category` explains the class.

**Routing** — classify an input, then send it to a specialized prompt, model or handler.

**Scenario policy (course)** — a deterministic rule-based stand-in for Claude used by the mock for one lab.

**Stop sequence** — a string that ends generation when produced (`stop_sequences=[...]`).

**Strict tool use** — `"strict": true` on a tool definition: guarantees tool inputs validate against the schema.

**Structured outputs** — `output_config.format` (or `messages.parse(output_format=Model)`): responses constrained to a JSON Schema.

**Subagent** — an agent started by another agent with its own isolated context window and (usually) restricted tools.

**Task budget** — `output_config.task_budget` (beta): a token budget the model is told about, so it paces a long agentic task; distinct from `max_tokens`.

**Tool Runner** — `client.beta.messages.tool_runner(...)`: the Anthropic SDK helper that runs the agent loop for tools you define.

**Tool use** — the model emitting `tool_use` blocks (name + JSON input) for your code to execute; results return as `tool_result` blocks.

**Workflow** — LLM calls orchestrated through predefined code paths; the code, not the model, decides the steps.
