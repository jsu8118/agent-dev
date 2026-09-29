# Day 2 exercises - Tool engineering at scale

Twelve exercises: concept checks (1, 12), calculations (2, 3), design scenarios (4-7) and hands-on coding (8-11,
with starter files in this folder). Worked answers are in [`../solutions/README.md`](../solutions/README.md);
runnable solutions are in `../solutions/*.py`. Prices: `claude-opus-5` $5 / $25 per million input / output tokens;
cache writes 1.25x (5-minute TTL) or 2x (1-hour TTL); cache reads 0.1x. Token counts quoted from the labs are
mock-mode estimates: the arithmetic and the decisions are the point, not the fourth digit.

Every starter runs as it is (`python advanced/day2_tools_at_scale/exercises/ex08_tool_removal_phase.py`) and prints
`TODO` where your code goes. They import the labs' helpers (`labs/_day2.py`), so the catalog, the backend
(`KestrelOps`) and the agent loop (`run_agent`) are the ones you used in the labs.

---

## 1. Concept check - what survives a change to the toolset?

The copilot is twelve requests into a conversation on a model with preserved thinking (Claude Opus 5.5 or Claude
Fable 5.1). Its `tools` array is the BM25 tool search tool, the nine core tools and 111 deferred tools; it caches
with an explicit breakpoint on the system prompt plus top-level automatic caching; earlier assistant turns carry
thinking blocks. For each change on the next request (one change at a time), answer three questions:
**(1)** does the API accept the request, **(2)** can the next request still read the cached prefix that request 12
wrote, **(3)** do the earlier thinking blocks still verify? Explain each answer.

a. The model's BM25 search returns five references and it calls one of the discovered tools.
b. Your harness appends `get_recall_status` to `tools` as a regular (non-deferred) tool.
c. You append `get_recall_status` to `tools` with `defer_loading: true` and announce it with a `tool_addition` block
   in a `role: "system"` message after the newest user turn, with `betas=["mid-conversation-tool-changes-2026-07-01"]`.
d. The same as (c), without the beta header.
e. A new deploy builds the `tools` array sorted by name; until now it was in catalog order.
f. You fix a typo in `get_invoice`'s description, in place (same name, new text).
g. You withdraw `post_teams_message` with a `tool_removal` block in a `role: "system"` message, and append the next
   user message after it.
h. Instead of (f), you leave `tools` as the first request sent it and append a `tool_addition` whose tool is a
   `tool_definition` carrying the corrected `get_invoice`, with `betas=["inline-tools-2026-09-15"]`.

## 2. Calculation - load everything, or defer and search?

Lab 01 measured the tools block at **12,064** prompt tokens with all 120 tools loaded and **1,322** with the nine core
tools plus the BM25 search tool (the other 111 deferred), and one discovery - a search turn that returned five
references - at **702** tokens added to the conversation tail. The copilot's conversations have **4 requests**; there
are **400** a day; **30%** need no discovery, **50%** one and **20%** two. Assume the tools prefix is always warm (every
request reads it from the cache), that discovery *i* happens in the response to request *i*, and that its blocks are
written to the cache with the next request and read on every request after that. Ignore output tokens: they are the
same under both strategies.

a. What do the 10,742 extra tokens of the loaded strategy cost per conversation? What do the discoveries cost per
   conversation under the deferred strategy, at the stated mix? Per day?
b. How many discoveries per conversation would make deferring as expensive as loading everything?
c. At night the traffic is so thin that every conversation starts on a cold cache. What changes in (a)?
d. A sister product has a 20-tool catalog: the same nine core tools plus 11 more at about 97 tokens each. Redo (a)
   and (b). Which strategy wins, and what does that say about when tool search is worth its search step?
e. What does this calculation leave out that could change the decision?

## 3. Calculation - a code cell, or direct calls?

Lab 05 triaged 11 recall units three ways. With direct calls, **1,337** tokens of tool results entered the context
(about 121.5 per unit); with programmatic tool calling only the cell's stdout did - **276** tokens (about 25.1 per
unit). Assume: the cell costs **600** output tokens to write; direct calls cost **27** output tokens per unit (about 1.4
calls of ~20 tokens each); after the triage the conversation continues for **6** more requests; everything the model
reads is written to the cache once (1.25x) and read on each later request (0.1x). Output tokens cost 5x input tokens.

a. How many tokens per unit does each later request re-read under each approach?
b. Write the cost per conversation of each approach as a function of the number of units *n* (in "units" of input
   tokens at list price). Where is the break-even *n* with caching? Without caching (every later request pays full
   price for everything it carries)?
c. Evaluate both approaches at 11 and at 110 units, in dollars.
d. Lab 05's step 5 shows the code path reading less than parallel direct calls (4,938 billed input tokens against
   6,363) and still costing more ($0.0684 against $0.0384). Where does the difference come from, which assumption of
   this exercise does the lab not share, and what would you measure live before quoting a saving?

## 4. Design - near-duplicate tools

The catalog has three families of near-duplicates on purpose: `get_shipment` / `track_shipment` /
`list_shipments_for_order`; `issue_refund` / `issue_credit_note` / `apply_late_fee_waiver`; `schedule_pickup` /
`schedule_return_pickup`. Lab 07's description set B fixed three of set A's confusions and introduced one: "Reduce
invoice AR-90257 by 5% as a goodwill credit; the customer was unhappy with the delay." now goes to
`apply_late_fee_waiver` instead of `get_invoice`.

a. For each family, write the sentence each description needs so the model can tell the tools apart: what it takes,
   what it returns or does, and what it does *not* do (and which sibling does).
b. Fix set B's remaining confusion by changing descriptions only, without breaking any task set B gets right. Check
   it on the 30 tasks (`solutions/ex04_near_duplicates.py` runs A, B and your set C).
c. When would you merge two near-duplicates into one tool instead of sharpening their descriptions? When would you
   split one tool into two?
d. Which of these mistakes does the executor catch, which does a strict schema catch, and which does only an eval
   catch?

## 5. Design - which tools are always loaded?

The copilot keeps nine tools loaded (the catalog's `meta.core` flag) and defers 111. The support agent's 480
production traces (`advanced/data/traces/traces.jsonl`; read the `tools` and `turns` fields only) show which tools
real conversations call. Propose the always-loaded set: the rule you use, the set it gives, and what you will
monitor after launch to revise it. Consider in particular:

* the usage share above which a tool is cheaper loaded than discovered (use exercise 2's cost model);
* `create_rma` - a write tool, not in the core, called in about a quarter of the traces;
* `track_shipment` - not in the core, while its near-duplicate `get_shipment` would stay deferred;
* `get_build_record` - in the core, never called in these traces;
* the tools that must be loaded whatever the traffic says, and why.

(`solutions/ex05_core_set.py` computes the usage table, the break-even shares and four candidate sets.)

## 6. Design - Keystone's scoped toolsets

Keystone Mechanical (tenant `keystone` in `advanced/data/security/tenants.json`) is a field-service contractor whose
people use the copilot from a mobile app in two roles, `contractor_lead` and `contractor_engineer`. Design the
toolset of each role's session: which catalog tools are declared at all, which are loaded and which deferred, what
the harness must add that `tenants.json` does not grant, and which checks cannot live in the toolset and must run in
the executor. Write the invariants you would test before any session starts. Then: a Keystone engineer asks "Who is
the site contact at C-1016? I need to call them." What should happen, and what must never happen?
(`solutions/ex06_keystone_scopes.py` builds both toolsets and checks the invariants.)

## 7. Design - retiring `get_shipment`

`get_shipment_v2` replaces `get_shipment`: it returns the promised ETA as a window and the carrier's last event.
Thousands of conversations are live, two older integrations call `get_shipment` by name, and prompts, evals and
dashboards mention it. Write the deprecation plan: the steps in order; what the model sees at each step
(definitions, descriptions, errors); what happens to the prompt cache and to preserved thinking at each step; what
you measure before you remove v1; and how a live conversation that already called `get_shipment` carries on.
(Lab 07's step 6 runs announce, sunset and measure.)

## 8. Hands-on - a close-out phase that removes tools (`ex08_tool_removal_phase.py`)

Extend lab 04's cache-preserving conversation with a fifth phase, close-out, whose tools are `close_service_ticket`
and `create_task` and which removes the communication tools. Prove three properties: (1) cache reads never fall at
a phase change; (2) the executor refuses a removed tool with `not_available` whatever the model saw; (3) when the
user asks for a Teams post during close-out, it is not made with a removed tool. Then answer: where exactly must the
`tool_removal` message sit, and what does the API do if you put it before the user message instead?

## 9. Hands-on - a selection-eval scorer (`ex09_selection_scorer.py`)

Lab 07 printed accuracy and a list of confusions. Build the scorer you would trust: accuracy with a 95% Wilson
interval, per-tool precision and recall, the confusion pairs, and an exact McNemar test on the paired results of
sets A and B. Then write ten held-out tasks in your users' words - without looking at set B - and score both sets
on them. Is B better? Roughly how many tasks would a paired comparison need to separate 90% from 97%?

## 10. Hands-on - fix the discovery misses (`ex10_discovery_fixes.py`)

In lab 02, BM25 missed one of the 32 tasks (`get_bulletin`) and regex missed 22. (1) Rewrite `get_bulletin`'s
description so BM25 ranks it first, without costing any other task its first place. (2) Write a regex pattern for
each regex miss that puts the expected tool first, and send each through the API (the stand-in uses a pattern in
backticks verbatim, so you can test your own). What do your working patterns have in common, and what does that
say about regex search for prose requests?

## 11. Hands-on - a streaming validator (`ex11_stream_validator.py`)

With `eager_input_streaming` the API no longer validates tool input; your client does. Implement `decide()`: given the
stop reason, the raw `input_json_delta` fragments of one `tool_use` block, whether the SDK raised while streaming,
the tool's schema and the request, return `run`, `retry_bigger`, `stop`, `invalid_json` or `reissue`. The starter
records a real streamed draft and builds eight cases from it. In which order must the checks run, and which case
passes the schema but must still not run?

## 12. Concept check - who do you trust in a code cell?

The recall triage of lab 05 now runs with programmatic tool calling: `get_pump_telemetry`, `get_fault_codes`,
`get_vibration_trend` and `get_stock` are declared with `allowed_callers: ["code_execution_20260120"]`. True or false,
with a reason:

a. Calls made from the cell do not go through my executor, so I do not need to gate them.
b. A tool result that the cell reads never reaches the model, so an instruction hidden in it cannot affect the
   conversation.
c. With `allowed_callers` listing only the code execution tool, the model cannot call these tools directly.
d. I can declare `issue_credit_note` with `strict: true` and make it code-callable, to get both guarantees.
e. The container keeps variables between cells, so one container can serve every user of the copilot and save
   start-up time.
f. A cell that loops over 500 serials is one model turn, so it cannot exceed a per-conversation limit on tool calls.
g. The code arrives in the response before it runs, so I can review it and decide whether to let it run.
