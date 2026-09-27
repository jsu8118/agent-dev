# Day 3 exercises - Context engineering: caching, retrieval and memory

Twelve exercises: concept checks (1, 2, 12), calculations (3, 4), design scenarios (5, 6, 7) and hands-on
coding (8-11, with starter files in this folder). Worked answers are in [`../solutions/README.md`](../solutions/README.md);
runnable solutions are in `../solutions/*.py`. Prices: `claude-opus-5` $5 / $25 per million input / output
tokens; cache writes 1.25x (5-minute TTL) or 2x (1-hour TTL); cache reads 0.1x.

---

## 1. Concept check - which change breaks which cache?

Kestrel's diagnostic agent sends requests shaped like this (render order: tools -> system -> messages):

* `tools`: `[list_work_orders, query_telemetry, read_section, search_manuals]`, explicit breakpoint **A** on the last tool;
* `system`: the manual library (~60K tokens), explicit breakpoint **B** on the last system block;
* `messages`: the conversation, with top-level automatic caching - breakpoint **C** on the last block.

For each change on the *next* request (one change at a time), which of A, B and C can still be *read*? Explain.

a. A new tool `get_spare_parts` is appended to `tools`.
b. `tool_choice` changes from `auto` to `{"type": "any"}` for one request.
c. The manual library is updated to revision D.
d. A `{"role": "system"}` message ("the technician is on site now") is appended after the latest user turn.
e. `output_config.effort` goes from `high` to `medium` for this one request.
f. The request is sent to `claude-sonnet-5` instead of `claude-opus-5`.
g. Citations are enabled on a document in the newest user turn.
h. A photo of the pump nameplate (an image) is attached to the newest user turn.
i. Nothing changes, but the previous request was sent six minutes ago.

## 2. Concept check - why doesn't Haiku cache my 3,000-token prompt?

A team moves its ticket classifier from `claude-opus-5` to `claude-haiku-4-5` to save money. The system prompt
(3,000 tokens, byte-identical on every request) has a `cache_control` breakpoint. On Opus 5 the dashboards showed
cache reads; on Haiku, `cache_read_input_tokens` is always 0 and there is no error. What is happening, how do you
confirm it, and what are the options (with their costs)? Is "pad the prompt to 4,096 tokens" ever a good idea?

## 3. Calculation - 5-minute vs 1-hour TTL

The field assistant's cached prefix (manuals + tools + system) is 20,000 tokens on `claude-opus-5`. For each
traffic pattern over a 10-hour shift, compute the daily input cost *of the prefix* with no caching, the 5-minute
TTL and the 1-hour TTL, and pick the cheapest:

* **A** - one question every 4 minutes (150/day);
* **B** - bursts of 6 questions within 2 minutes, one burst every 30 minutes (120/day);
* **C** - 12 questions/day at irregular times: minute 0, 35, 110, 150, 230, 245, 330, 400, 455, 520, 560, 590.

Remember: a read refreshes the entry, and the lifetime runs from the *start* of the request. Then answer: if C's
arrivals were random (Poisson) with a 50-minute mean gap, what is the expected cost per request with each TTL?

## 4. Calculation - a 20-turn agent

An agent on `claude-opus-5` has a 5,000-token prefix (system + tools); every turn adds 1,200 tokens to the history
(tool results plus the model's text) and produces 350 output tokens (thinking included). It runs 20 turns.

a. How many input tokens does it send in total, and what does the run cost without caching?
b. What does it cost with automatic caching (each turn reads everything the previous turn sent and writes the rest)?
c. At turn 12 a context-editing pass clears 8,000 tokens of old tool results. What does that do to the cache and to
   the total cost? Is context editing a cost-saving tool?

## 5. Design - retrieval for 50,000 manual pages across 300 product variants

Kestrel acquires a competitor. The field assistant must now answer from 50,000 pages of manuals covering 300
product variants (about 70% of each variant's content is shared with its family; some older manuals are scanned
PDFs; revisions ship quarterly). Technicians ask from a phone, expect an answer in under 10 seconds, and every
answer must cite the page it came from. Propose the retrieval architecture. Compare it explicitly with (i) long-
context stuffing, (ii) BM25 only, (iii) embeddings only, (iv) agentic search, and say how you would evaluate it
before and after each quarterly re-index.

## 6. Design - memory, RAG, system of record, or nowhere? (Policy PRV-004)

For each item, decide where it belongs - the memory tool (per-customer `/memories`), retrieval over documents
(RAG), a system of record queried through a tool (ERP / CMMS / CRM), the current conversation only, or nowhere -
and justify it:

1. "HF-KP250-03 can only be stopped on Sundays 06:00-10:00."
2. The Harbor Foods plant contact's mobile number.
3. The KP-250 bearing regreasing interval.
4. "Bearings on HF-KP250-03 were replaced on 2026-09-20 (WO-24533)."
5. "The site contact prefers an SMS before technicians arrive."
6. The customer's purchase-order number for an open order.
7. "Technician T. Brooks prefers torque values in N·m."
8. A supplier's new bank account number, mentioned in an email.
9. The Wi-Fi password of the plant's guest network.
10. Last week's diagnosis summary for GB-KP400-02 (cavitation suspected, reservoir level to be raised).

Then list the governance controls the memory store needs.

## 7. Design - pick the long-running strategy

Choose between client-side trimming, server-side tool-result clearing (`clear_tool_uses_20250919`), compaction
(`compact_20260112`) and the memory tool - alone or combined - for each agent, and say what you would configure:

a. A 6-hour data-migration agent that converts 4,000 legacy maintenance plans; decisions made in hour 1 (naming
   rules, unit conversions) must still be applied in hour 6.
b. A monitoring agent that polls the latest telemetry of 12 pumps every 10 minutes all day; only the latest reading
   of each pump matters, plus any alarms raised.
c. A warranty-claim assistant that, at the end of a long conversation, must quote the exact policy clauses it relied
   on for the audit file.

## 8. Hands-on - hybrid retrieval (`ex08_hybrid_retrieval.py`)

Add a character n-gram TF-IDF retriever (pure Python) and fuse it with BM25 using reciprocal rank fusion
(`score = sum 1/(k + rank)`, k = 60). Measure hit@1/3/5 and MRR on the lab 05 question set. Which questions moved,
in which direction, and why? Try a weighted fusion and a stemmed BM25. What can no lexical method fix?

## 9. Hands-on - a context budget guard (`ex09_context_budget_guard.py`)

Implement a guard that runs before every request of the lab 06 fleet review: count the request with
`client.messages.count_tokens`; when it is over budget (25,000 tokens), replace the oldest tool results with
short digests computed in code until it fits. Constraints: never touch the newest tool results; keep every
`tool_use` / `tool_result` pair valid; prune whole turns, not single blocks. Verify the final report still covers
all 12 pumps. What happens if you let the guard trim the `list_assets` result?

## 10. Hands-on - a new question set (`ex10_policy_questions.py`)

Write 8-10 labelled questions about the company policies (returns, refunds, shipping, warranty, privacy, safety),
phrased the way support staff really ask. Run the lab 05 variants on them. Is the best variant for policies the
same as for manuals? Explain the difference from how the documents are written.

## 11. Hands-on - a cache that never hits (`ex11_cache_debugging.py`)

The starter's prompt builder never gets a cache read. Find every silent invalidator by rendering two consecutive
requests in API order and diffing them, fix the builder, and add a regression check that fails if the second
identical request reads nothing from the cache.

## 12. Concept check - what does a citation prove?

In lab 03 every citation was "verified". (a) What exactly does a `char_location` citation guarantee, and what does it
not guarantee? (b) Why is `output_config.format` rejected (400) when citations are enabled? (c) The work-order system
needs a JSON draft (`asset_id`, `action`, `parts`, `references`) *and* verifiable quotes from the manual. Design
two ways to get both, with their trade-offs.
