# Day 3 - Context Engineering: Caching, Retrieval & Memory

Everything Claude knows about your problem arrives through the context window: the system prompt, the tool
definitions, the conversation, the documents you retrieve, the tool results you return. Day 2 built agents that
*act*; today is about what they *read* - what goes into the window, what it costs, how to keep it small and cheap,
and what to keep outside it. The running case is Kestrel's field-service and reliability assistant: technicians ask
about manuals, twelve pumps stream condition-monitoring data, and the senior technicians who knew everything by
heart are retiring.

## Learning objectives

By the end of the day you can:

1. Measure a context budget with `count_tokens` and predict what a request - and an agent loop that re-sends its
   history on every turn - will cost.
2. Design prompts for prompt caching: breakpoint placement, TTL choice, break-even arithmetic, verification from
   `usage`, and the silent invalidators that make caching quietly stop working.
3. Choose between long-context stuffing, lexical RAG, semantic/hybrid RAG, agentic search and pre-computed summaries
   for a given corpus and question mix, and justify the choice with numbers.
4. Make retrieval verifiable and measurable: structure-aware chunks, contextual headers, API citations, and an
   evaluation harness (hit@k, MRR, groundedness).
5. Put structured data behind tools that aggregate in code instead of pasting rows into the prompt.
6. Keep long-running agents inside the window with client-side trimming, server-side tool-result clearing and
   compaction - knowing what each one loses and what it does to the cache.
7. Give an agent long-term memory with the memory tool, safely: tenancy, path confinement, a PII guard, audit, retention.

## Agenda (about 7 hours)

| Time | Block | Lab |
|---|---|---|
| 0:00 - 0:50 | The context window as a budget; token counting; re-sent history | 01 |
| 0:50 - 2:00 | Prompt caching deep dive | 02 |
| 2:00 - 2:15 | Break | |
| 2:15 - 3:30 | Retrieval strategies, chunking, citations | 03 |
| 3:30 - 4:15 | Lunch | |
| 4:15 - 5:15 | Structured data via tools; agentic search vs one-shot RAG | 04 |
| 5:15 - 5:50 | Evaluating retrieval | 05 |
| 5:50 - 6:30 | Long-running agents: trimming, context editing, compaction | 06 |
| 6:30 - 7:00 | Memory: what goes where, and how to keep it safe | 07 |

Exercises (`exercises/README.md`, 12 of them, with worked solutions in `solutions/README.md`) are for the evening
or the next morning.

**Running the labs.** `python day3_context_rag_memory/labs/01_token_budget.py` (activate the venv first). Without an
API key everything runs against labkit's offline mock: the SDK code paths are real, the cache, context editing and
compaction are simulated with the documented semantics, and token counts are estimates (~3.8 characters per token).
With `ANTHROPIC_API_KEY` set, the same scripts call Claude; the whole day costs a few dollars (the simulated total is
about $3, most of it lab 06's four long runs). Excerpts below are labelled *mock mode* - live numbers and wording
differ, the shapes and the lessons don't.

---

## 1. The context window is a budget

**What it is.** The context window is everything the model reads for one request plus what it writes: system
prompt, tool definitions (plus a tool-use preamble the API adds whenever `tools` is non-empty), every message
including tool results, documents and images, previous thinking blocks on models that keep them, and the output -
thinking included. Claude Opus 5 and Sonnet 5 have a 1M-token window (billed at standard prices, no long-context
premium); Claude Haiku 4.5 has 200K. If the input alone is too long the API returns a 400 ("prompt is too long").

**Why it matters - three budgets, not one.**

* *Money.* Every input token is billed on every request. Agents re-send the whole conversation each turn, so input,
  not output, usually dominates an agent's bill.
* *Latency.* Time to first token grows with the uncached input the model has to process.
* *Quality.* "More context isn't automatically better. As token count grows, accuracy and recall degrade, a
  phenomenon known as context rot" (Claude docs). Attention is a finite budget spread over every pair of tokens;
  irrelevant tokens are not free even when they fit. A 1M window is capacity, not a recommendation.

**What fills it at Kestrel** (lab 01, mock-mode estimates):

| component | tokens | share of 1M | share of 200K |
|---|---:|---:|---:|
| diagnostic system prompt | 246 | 0.02% | 0.1% |
| 5 tool definitions + tool-use preamble | 1,187 | 0.12% | 0.6% |
| all 11 manuals and policies | 10,161 | 1.0% | 5.1% |
| one pump, 30 days of hourly telemetry, raw CSV | 11,426 | 1.1% | 5.7% |
| the fleet, 30 days, raw CSV (8,640 rows) | 136,441 | 13.6% | 68.2% |
| one pump, 30 days, one metric aggregated in code | 91 | 0.01% | 0.05% |
| top-4 retrieved sections for one question | 579 | 0.06% | 0.3% |

Two lessons hide in that table. The whole document corpus is small - 1% of the window - so stuffing it is a
legitimate option today (section 3 prices it). And data is where budgets explode: one month of fleet telemetry is
two thirds of Haiku's window, while an aggregate computed in code is two orders of magnitude smaller than the rows it
summarises (section 4).

**How agent loops re-send history (the cost math).** An agent with a fixed prefix of *P* tokens (system + tools)
that adds *D* tokens per turn (a tool result and the model's text) sends *P + (t-1)·D* tokens on turn *t*, so a
*T*-turn run sends *T·P + D·T(T-1)/2* tokens in total - quadratic in the number of turns. With lab 01's numbers
(P = 1,433, D = 341) a 10-turn run sends 29,675 input tokens and a 40-turn run 323,300: four times the turns,
eleven times the tokens. Caching reprices the re-sent part at 0.1x (lab 01 shows 73% saved at 10 turns, 85% at 40 - and
-25% for a single call, where the cache write premium buys nothing). Output tokens, including thinking, are billed
once and never cached.

**Token counting.** `client.messages.count_tokens(model=..., system=..., tools=..., messages=...)` returns the input
tokens of a request shape using that *model's* tokenizer. It is free (rate-limited), accepts the same content as
`messages.create`, and is the only correct way to count: OpenAI's tiktoken undercounts Claude tokens by roughly
15-20% on prose and far more on code, CSV or non-English text. Characters-per-token varies with content - prose
tokenises better than JSON, and dense numeric CSV worse still - so a character heuristic is fine for planning and
wrong for enforcement. In responses, remember that `usage.input_tokens` is only the *uncached* remainder: the prompt
size is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.

| approach | when | cost | risk |
|---|---|---|---|
| stuff everything that might help | small, stable corpus that most questions need | high per call unless cached | context rot, stale copies |
| curate per request (retrieve, aggregate) | large or changing corpus, data | low per call, engineering cost | missed context if retrieval fails |
| let the agent fetch just in time (tools) | open-ended, multi-step questions | several calls, small context each | more latency; loop hygiene needed |

**Pitfalls.** Counting characters instead of tokens; forgetting tool definitions and the tool-use preamble (paid
on every request); forgetting that Opus 4.5+ and Sonnet 4.6+ keep previous thinking blocks, which are billed as
input on later turns (the labkit mock counts them as ~0); pasting unbounded user input or tool output; treating the
window size as a target.

**Recipe.** Keep a budget table per request type (like lab 01's). Count before sending anything unbounded, and reject,
truncate or route it to a tool when it is too big. Log `usage` for every call (labkit's metering does this) and alert
when prompts drift up. Prefer tools that return aggregates over tools that return rows.

---

## 2. Prompt caching

**What it is.** A `cache_control` breakpoint tells the API to keep the processed prefix of the prompt - everything up
to and including that block. A later request whose prompt starts with the byte-identical prefix *reads* it at about a
tenth of the input price instead of processing it again, and starts answering sooner.

**Why it exists.** The expensive part of an LLM application is re-reading the same context: the same system prompt,
tools and documents on every request, the same growing history on every agent turn. Anthropic's measurements put
caching as the largest single cost lever - agent-loop cost cut by a factor of 2.5 to 3.7 at 81-90% hit rates.

**How it works (to the depth you need to debug it).**

* **Render order and the key.** The prompt is rendered tools -> system -> messages. The cache key is the exact bytes
  of the prefix up to a breakpoint, per model; caches are isolated per workspace (per organisation on Bedrock and
  Google Cloud) and never shared across organisations. One changed byte at position N invalidates every breakpoint
  after N.
* **Breakpoints.** Up to 4 per request, on system blocks, tools, or message content blocks (text, image, document,
  tool_use, tool_result). A top-level `cache_control` on the request is *automatic caching*: one breakpoint on the
  last cacheable block that moves forward as the conversation grows (it uses one of the four slots).
* **Lookup.** Each breakpoint walks back at most 20 positions looking for an entry an earlier request wrote (a run of
  consecutive tool_use or tool_result blocks counts as one position); the longest hit is read, and tokens between the
  hit and the last breakpoint are written. A turn that adds more than 20 positions can silently miss.
* **Minimum length.** 512 tokens on Claude Opus 5, 1,024 on Sonnet 5, 4,096 on Haiku 4.5. Below it, nothing is cached
  and nothing errors - `cache_creation_input_tokens` is simply 0.
* **TTL.** Five minutes by default, or `{"type": "ephemeral", "ttl": "1h"}`. The lifetime runs from the *start* of the
  request that wrote or read the entry, and every read refreshes it.
* **Prices.** Writes cost 1.25x the input price (5-minute TTL) or 2x (1 hour); reads cost 0.1x (0.05x on Opus 5.5,
  0.025x on Fable 5.1). Output is never cached.
* **Concurrency.** An entry becomes readable only once the first response starts streaming; N parallel requests with
  the same new prefix pay N writes. Send one, wait for its first token, then fan out.
* **Tiers.** The API keeps tools, system and messages caches; some parameters invalidate only their tier and below:

| change | tools cache | system cache | messages cache |
|---|:---:|:---:|:---:|
| tool definitions added / removed / reordered | lost | lost | lost |
| model switch | lost | lost | lost |
| citations toggle, web search, speed | kept | lost | lost |
| system prompt content | kept | lost | lost |
| `tool_choice`, images anywhere in the prompt | kept | kept | lost |
| thinking or effort change | model-specific | model-specific | lost |
| message content | kept | kept | lost from the change on |

Three rows have escape hatches that move the change *after* the cached prefix. On Claude Opus 5, a mid-conversation
`{"role": "system", ...}` message changes instructions without touching the prefix (no beta needed; it must follow a
user turn and be last or followed by an assistant turn). Tool additions go through tool search or `tool_addition`
blocks (beta), and per-message effort through a system message with `output_config` (beta).

**Placement patterns for agents.**

| situation | breakpoints |
|---|---|
| large static system prompt + tools | explicit breakpoint on the last system block (caches tools + system together) |
| multi-turn chat or agent loop | the robust combination: that explicit static breakpoint **plus** top-level automatic caching for the moving tail |
| shared documents, varying question | breakpoint on the last shared document block, *not* on the question (lab 03, step 4) |
| sections that change at different rates | one explicit breakpoint per stability boundary (tools never, library daily, session per user) |
| prompt that differs from its first token | don't cache - you would pay the write premium for nothing |

**Economics and break-even.** With the 5-minute TTL, two requests break even (1.25 + 0.1 < 2); with the 1-hour TTL,
three (2 + 0.2 < 3). Choose the TTL by the start-to-start gap between requests that share the prefix: under 5 minutes,
the 5-minute TTL is strictly cheaper (every request refreshes it); between 5 and 60 minutes, the 1-hour TTL is the
only one that survives; over an hour, neither helps - accept the miss or pre-warm (a `max_tokens: 0` request with the
same prefix writes the cache without generating anything). Lab 02 measured a 6,477-token manual library: four questions
cost $0.0735 instead of $0.1529 (52% less, 61% on the input side); one question every 12 minutes for an hour costs
$0.1943 uncached, $0.2429 with the 5-minute TTL (it expires between requests - worse than nothing) and $0.0810 with the
1-hour TTL.

**Caching vs batching vs shrinking.** They stack. The Message Batches API halves the price of *every* token, cache
reads and writes included, for work nobody is waiting on (results within 24 hours; cache hits inside a batch are
best-effort because requests run concurrently). Caching is for repeated prefixes in interactive or sequential work.
Shrinking the prompt (retrieval, aggregation) attacks the tokens themselves. For 1,000 overnight questions over lab 02's
prefix: $3.28 synchronous and cached, $16.19 batched without cache_control, about $1.64 batched with it if the hits land.

**Measuring it.** `usage.cache_creation_input_tokens` (written, 1.25x or 2x - split by TTL in `usage.cache_creation`),
`usage.cache_read_input_tokens` (read, 0.1x) and `usage.input_tokens` (the uncached tail). The healthy agent-loop
signature: reads cover the whole prior prefix and grow turn by turn; writes are about one turn's worth. If writes are
the size of the whole conversation on every turn, something upstream of the breakpoint changes. Find it by diffing two
consecutive request payloads (strip `cache_control` first; the first divergence inside the overlap is the invalidation
point) or, on the Claude API, with the cache-diagnostics beta (`cache-diagnosis-2026-04-07`). Then put an assertion in
CI - "the second identical request reads from the cache" - because caching regressions are silent.

**Silent invalidators** (grep for them in anything that builds the prefix): `datetime.now()` or request IDs in the
system prompt; `json.dumps` without `sort_keys=True`; iterating sets or plugin registries to build tools; per-user
f-strings in the system prompt; conditional system sections (every flag combination is a separate prefix); switching
models or effort per request; rebuilding `system`/`tools` differently in a "fork" (summariser, subagent) that should
share the parent's prefix.

**Pitfalls.** Automatic caching on prompts that end in unique content (retrieved rows, a one-off question) writes an
entry per request that is never read - put an explicit breakpoint at the end of the shared part instead. Prompts below
the model's minimum. The same prompt spread across workspaces. Every context-editing pass (section 5) rewrites the
cached conversation.

**Recipe.** Order content by stability (tools, then the frozen system prompt, then documents, then the conversation,
then per-request data). Put an explicit breakpoint on the static prefix and use automatic caching for the tail. Keep
volatile values out of the prefix. Choose the TTL from real start-to-start gaps. Verify from `usage`, in CI.

---

## 3. Retrieval: getting the right text into the window

**The options, compared.**

| strategy | how it works | strengths | weaknesses | Kestrel fit |
|---|---|---|---|---|
| long-context stuffing | put the whole corpus in a cached prefix | perfect recall; no retrieval errors; simple | cost and latency grow with the corpus; context rot; impossible beyond the window | today's 11 documents (10K tokens) - yes, cached |
| lexical RAG (BM25) | rank chunks by term overlap | exact codes, part numbers, numbers; explainable; no model; cheap | vocabulary mismatch, no morphology, long chunks diluted | strong baseline; lab 03-05 |
| semantic RAG (embeddings) | nearest neighbours in a vector space | paraphrase and synonyms | weak on exact identifiers; needs an embedding model and index; opaque failures | pair with BM25, not instead |
| hybrid + rerank | fuse lexical and semantic rankings, rerank the top-N | best of both; the production default | more moving parts | the architecture for a large corpus (exercise 5) |
| agentic search | retrieval exposed as tools the model calls, refines and repeats | adapts queries to what it finds; combines documents with data | more calls and latency; needs loop hygiene | diagnosis (lab 04) |
| pre-computed summaries | summarise documents or data offline; retrieve summaries | tiny context; fast | lossy; stale; summary errors propagate | fleet dashboards, not troubleshooting |

Anthropic's own guidance is blunt about the first row: if a knowledge base is under about 200,000 tokens, just put it
in the prompt with caching. Lab 03 bears that out: on the first question stuffing costs $0.0706 (it writes the 10K-token
prefix) against about $0.01 for four retrieved sections, and later questions cost $0.012-0.013 because they read the
cached prefix. Retrieval wins when the corpus outgrows the window, changes too often to cache, or when precision matters -
the stuffed model reads eleven documents to use one or two.

**BM25, briefly.** Each query term contributes IDF (rare terms count more) times a saturating term frequency
(parameter `k1`), normalised by chunk length relative to the average (parameter `b`). That explains its failure modes,
all visible in lab 05: a long chunk (the 2,545-character fault-code table) is penalised, so "F10" ranks fourth; words
must match exactly, so "continuously" misses "continuous" and "history" misses "log"; and the tokenizer splits "KP-250"
into "kp" and "250", so a KP-250 limits query can rank the KP-400 manual first (it mentions "KP-250" several times).

**Embeddings and vector databases (conceptual).** An embedding model maps text to a vector such that similar meanings
are close (cosine similarity). You embed every chunk once (as `document`), the query at run time (as `query` - models
like Voyage's are trained with that distinction), and search an approximate-nearest-neighbour index (HNSW or IVF) in
a vector store: pgvector in Postgres, FAISS in process, Qdrant, Weaviate, Milvus, or Elasticsearch/OpenSearch with
hybrid BM25 + kNN in one query. Anthropic does not offer an embedding model; its docs point to Voyage AI (the `voyage-4`
family, contextualised-chunk models, and `rerank-2.5` rerankers), and open-source local models (sentence-transformers
and similar) are an option where data must not leave your network. Practicalities: changing the embedding model means
re-embedding everything; embeddings blur exact identifiers ("F10" vs "F01"), which is why hybrids keep BM25; filters
(product variant, revision) must be applied inside the vector search, not afterwards. The labs stay with BM25 and a
pure-Python subword retriever (exercise 8) so they run offline.

**Chunking.** Chunk by the document's structure: one chunk per section (the manuals' H2/H3 headings), with long
tables split into row groups. Fixed-size windows (500 characters with 100 of overlap in lab 05) are the fallback for
unstructured text; they rescue long tables but cut context. Either way, add a **contextual header** to what you index -
"document title > section path" - because the words that identify a chunk often live in its heading, not its body
("KP-400", "Operating limits"). Anthropic's Contextual Retrieval goes further and has Claude write a 50-100-token context
for every chunk: in their benchmark it cut top-20 retrieval failures by 35% with embeddings, 49% with embeddings plus
contextual BM25, and 67% with reranking on top. Lab 05's deterministic headers are the zero-cost version: +0.068 MRR
for sections, +0.037 for windows.

**Top-k: recall, precision, tokens.** More chunks raise recall and cost precision and tokens (lab 05, sections + headers):

| k | hit@k | precision@k | tokens retrieved |
|---:|---:|---:|---:|
| 1 | 0.70 | 0.70 | ~128 |
| 3 | 0.80 | 0.28 | ~455 |
| 5 | 0.90 | 0.20 | ~841 |
| 8 | 1.00 | 0.14 | ~1,258 |

At this chunk size k = 4-5 costs under 1K tokens; a reranker lets you retrieve 50 cheaply and keep 5. Irrelevant chunks
are not harmless: they cost tokens and can distract the model, so "just raise k" has a quality cost as well as a price.

**Citations: making answers verifiable.** Pass retrieved text as `document` blocks with `"citations": {"enabled": true}`
and Claude's answer comes back as text blocks carrying `citations`: `char_location` (character range, 0-indexed, end
exclusive) for plain-text documents, `page_location` for PDFs, `content_block_location` for custom-content documents.
Alternatively return `search_result` blocks (`source`, `title`, `content` text blocks) - top level in the user turn or
inside a tool result - and get `search_result_location` citations that carry *your* source identifier. Rules worth
knowing: `title` and `context` are passed to the model but are not citable; citations must be enabled on all documents
(or all search results) in a request or none; the smallest citable unit of a search result is one content block, so
split tables into rows if you want row-level citations; `cited_text` is extracted by the API and is not billed as output
(nor as input when sent back); and citations are **incompatible with structured outputs** (`output_config.format`
returns a 400). A citation guarantees a valid pointer into text you sent - lab 03 verifies every pointer in code - but
not that the text supports the claim; that is a groundedness check (Day 6).

**Evaluating retrieval.** Measure retrieval separately from generation, with a labelled question set (question ->
gold document and section). *hit@k*: is a gold chunk in the top k? *MRR*: the mean of 1/rank of the first gold chunk
(rewards putting it first). *precision@k* and *tokens retrieved*: what you pay for recall. Then, end to end:
*groundedness/faithfulness* (every claim supported by cited text - an LLM judge calibrated on human labels, on samples)
and answer accuracy. Keep sets per content type (exercise 10: the best chunking for policies is not the best for
manuals), read failures one by one, and re-run on every index change.

**Pitfalls.** Evaluating only end-to-end answers (you can't tell a retrieval miss from a generation error); a question
set written by people who know the documents' vocabulary; tuning on the set you report; chunks without their context;
ignoring product variants; stale indexes.

**Recipe.** Start with BM25 over structure-aware chunks with contextual headers, a 20-question labelled set and
hit@k/MRR. Add citations. Add a semantic retriever and a reranker when the failure analysis says vocabulary mismatch.
Move to agentic search when questions need several lookups or data.

---

## 4. Structured data: query it with tools, don't paste it

**What.** Telemetry, work orders and ERP tables are structured data. The anti-pattern is to paste rows into the prompt
and ask the model to spot trends; the pattern is a tool that computes in code and returns a small, self-describing
result (`query_telemetry(asset_id, metric, start, end, agg)` returning a median, percentiles, a first-vs-last-week
change, a daily series or an hour-of-day profile).

**Why.** Tokens: 30 days of one pump is 11,426 tokens of rows versus 91 for a one-metric summary (lab 01). Scaling:
raw rows grow linearly with history - a year is ~148K tokens per pump per question (lab 04's projection) - while an
aggregate stays constant. Accuracy and auditability: a model reading 720 rows does arithmetic by attention and cannot
show its work; code is exact, repeatable and testable. Data hygiene: the vibration guide says single-sample spikes above
50 mm/s are electrical noise and runs of exactly 0.0 while running are sensor faults; the tool filters them *and reports
what it filtered* (lab 04 surfaces a 59.7 mm/s spike and ignores it; lab 06 does the same for a 75.86 mm/s spike on an
ATEX pump that a naive "max vibration" alert would have escalated).

**How it compares.** Lab 04's scoreboard (mock mode): one-shot RAG without data cannot diagnose either pump (it
correctly answers "undetermined"); one-shot RAG with 720 raw rows diagnoses both, at ~12K input tokens per question; the
agent diagnoses both with evidence computed in code, in six calls (~22K input tokens in total, mostly cache reads), for a
similar price - and its cost does not grow with the history. Other options: SQL tools with read-only credentials and
row limits; Claude's server-side code-execution tool or programmatic tool calling when the analysis is open-ended; and
pre-computed features (daily medians, alarms) maintained by a data pipeline.

**Pitfalls.** A "raw" mode without a cap (lab 04's returns at most 168 rows and says so); silently dropping bad samples;
results without units, window or asset metadata (they become meaningless once other turns are trimmed); time zones
(everything here is UTC).

**Recipe.** One tool per question shape, narrow parameters, aggregates by default, raw rows only on request and capped,
filtered samples reported, units and windows in every result.

---

## 5. Long-running agents: trimming, context editing, compaction

An agent that works for hours accumulates tool results until it hits the window - and degrades well before that.
Four tools, often combined:

| | client-side trimming | tool-result clearing | compaction | memory tool / subagents |
|---|---|---|---|---|
| **what** | your code rewrites old history (e.g. raw results -> digests) before sending | `clear_tool_uses_20250919`: the API replaces old tool results with a placeholder before the model reads them | `compact_20260112`: past a threshold the API summarises earlier turns into a `compaction` block | state kept outside the context; a subagent absorbs a bulky subtask |
| **who decides** | you, at natural boundaries | trigger / keep / clear_at_least / exclude_tools | trigger (>= 50,000 input tokens), your `instructions` | the model (memory), your design (subagents) |
| **what is lost** | whatever your digest drops - you choose | the raw results; the model's own notes survive | anything the summary omits | nothing, if it was written down |
| **your history** | changed | unchanged (edits are applied server-side, per request) | you append the compaction block; the API ignores what precedes it | unchanged |
| **cache impact** | one miss per prune, from the first edited block | every clearing pass rewrites the cached conversation | a new prefix after compaction; the summarisation pass is billed | none |
| **beta** | - | `context-management-2025-06-27` | `compact-2026-01-12` | memory: none |

**Internals.** *Clearing*: with `trigger` (default 100,000 input tokens) exceeded, all but the most recent `keep` tool
uses (default 3) have their results replaced; `clear_at_least` skips the pass unless it frees at least that many tokens
(so each cache break buys real space); `exclude_tools` protects small, vital results; `clear_tool_inputs` also clears
the tool_use parameters. The response reports `context_management.applied_edits` (`cleared_tool_uses`,
`cleared_input_tokens`). A sibling strategy, `clear_thinking_20251015`, manages old thinking blocks and must be listed
first if you combine them. *Compaction*: the trigger defaults to 150,000 and must be at least 50,000 input tokens;
`instructions` replaces the default summarisation prompt entirely; the response starts with a `compaction` block that
you must keep by appending the **full** `response.content` (append only the text and the next request has no summary);
the API then ignores everything before the block; `usage.iterations` lists the compaction pass separately and the
top-level usage excludes it, so bill by summing the iterations; `pause_after_compaction: true` returns
`stop_reason: "compaction"` so you can re-insert content before continuing. Compaction is available on Opus 5, Sonnet 5
and other current models (not Haiku 4.5). An on-demand variant (beta `compact-2026-09-04`) lets your code decide when to
compact.

**What lab 06 measured** (mock mode, same 12-pump review four times, 14 days of raw rows per pump):

| strategy | peak prompt the model read | total prompt billed | cost | assets reported with numbers | follow-up flags |
|---|---:|---:|---:|---:|---:|
| none | 52,097 | 158,111 | $0.4488 | 10/12 | 5 |
| client-side trimming | 18,735 | 60,202 | $0.4142 | 10/12 | 5 |
| tool-result clearing | 19,365 | 76,064 | $0.4108 | 10/12 | 5 |
| compaction | 34,575 (then 1,709) | 125,483 | $0.6626 | 7/12 | 3 |

(The two standby pumps have no running data, so 10/12 is complete.) Three things stand out. Trimming and clearing cut
the peak by ~63% but the bill by only ~8%: each prune is a cache write at 1.25x replacing reads at 0.1x - context editing
is a context-window tool, not a savings lever, exactly as Anthropic's cost guidance says. Compaction cost the most in
this short session (its summarisation pass re-read 51K tokens) but made every later turn nearly free; the lab computes
that it pays for itself after about 8 more turns - and it is what keeps a long session inside the window at all. And
compaction is lossy: the mock's summary (a deterministic stand-in that keeps only the user requests and the list of tool
calls) lost the first site's notes, so the follow-up question missed the pump that needs action for cavitation. A real
Claude summary follows your `instructions` and usually keeps such notes - but "usually" is why you verify what survived,
and persist what you cannot afford to lose outside the context (section 6).

**Pitfalls.** Clearing small, high-value results (exercise 9: a guard that trimmed the asset register made the agent
review 8 pumps instead of 12); clearing every turn (set a high trigger and `clear_at_least`); a compaction trigger below
the documented 50,000-token minimum (the mock does not check it); appending only the text of a compaction response; counting
with a char heuristic; letting a paused turn or a max_tokens stop execute a truncated tool call.

**Recipe.** Make tools return compact results first (section 4). Write working notes as you go. Trim or clear at
natural boundaries with `keep` sized to the work unit and `exclude_tools` for registries. Add compaction with explicit
`instructions` for sessions that genuinely run long, and a memory file for facts that must outlive any summary.

---

## 6. Memory: what goes where

"Memory" means very different things; confusing them is how agents end up with stale facts or leaked personal data.

| kind | lifetime | who writes | holds | Kestrel example |
|---|---|---|---|---|
| conversation state | one conversation | the loop (messages) | the dialogue, tool results | the current diagnosis |
| working memory | one task | the model (notes in its output, a scratch file) | intermediate findings | lab 06's per-site note lines |
| long-term memory (memory tool) | across sessions | the model, via your handler | preferences, working notes | "HF-KP250-03 can only stop on Sundays 06:00-10:00" |
| retrieval (RAG) | as long as the document | authors, your index | authoritative, versioned content | the regreasing interval |
| system of record (via tools) | forever | business systems | facts, transactions, people | work orders, contacts (CRM), orders (ERP) |
| fine-tuning | the model's lifetime | a training job | behaviour and style, not facts | not needed here |

Rule of thumb: facts that have an owner live in the owner's system and are *retrieved*; documents are *retrieved*;
memory holds what nobody else records - preferences and working notes - and points to the system of record rather than
copying it. Fine-tuning is for behaviour, not for knowledge that changes.

**The memory tool.** `tools=[{"type": "memory_20250818", "name": "memory"}]` (no beta header). Claude issues `view`,
`create`, `str_replace`, `insert`, `delete` and `rename` commands on paths under `/memories`; **your code** executes them
against storage you control - files, a database, encrypted blobs. When the tool is present the API adds an instruction
to check the memory directory before doing anything else and to record progress, assuming the context may be reset at
any time. The Python SDK provides `BetaAbstractMemoryTool` (subclass it) and `BetaLocalFilesystemMemoryTool`, and
`client.beta.messages.tool_runner` runs the loop. Memory pairs with context editing (Claude gets a chance to save what
is about to be cleared) and with compaction (memory keeps what a summary might drop).

**Governance under PRV-004.** Policy PRV-004 §3 says assistants must not store customer personal data in long-term
memory; memory may hold preferences and working notes keyed by customer ID. Lab 07 turns that into code:

* **Tenancy by the harness:** the memory root (`.runs/day3_memories/<customer_id>`) is chosen from the authenticated
  work context, never from anything the model says. A Cobalt Chemical session gets Cobalt's (empty) memory.
* **Path confinement:** only `/memories...`; `..`, encoded traversal (`%2e%2e`), backslashes and symlinks that resolve
  outside the root are refused.
* **A PII guard in code:** regexes (email, phone, IBAN, card numbers) plus a dictionary of people's names from the CRM,
  applied to file content and file names; the error tells the model what to do instead.
* **Size caps, owner-only file permissions, an audit log that never records content (nor a rejected, PII-bearing
  path), and retention** (delete notes untouched for N days; erase on request).
* **Treat memory as data, not instructions,** when you read it back: whatever was written can be wrong, stale or
  injected.

**Pitfalls.** Memory as a second copy of facts that belong in the CMMS or the manuals (they diverge); cross-tenant
leakage through a shared root; unbounded growth; secrets; relying on the prompt alone to keep PII out.

**Recipe.** Decide per data item where it lives (exercise 6). Scope memory per tenant in the harness, guard it in code,
audit it, expire it. Keep notes short and point to records ("see WO-24502") instead of copying them.

---

## Case study: Kestrel's field-service and reliability assistant

Kestrel monitors twelve pumps at three customer sites (Granite Bay Water District, Harbor Foods Plant 2, Cobalt
Chemical) with hourly vibration, bearing temperature, pressure, flow and motor current. Five faults hide in the last 30
days: bearing wear on HF-KP250-03 (a 24/7 chilled-water pump), night-time cavitation on GB-KP400-02, a vibration-sensor
dropout on CC-KP100-04, overload on CC-KP600-01 after a demand increase, and misalignment on GB-KP250-03 after a coupling
was replaced without an alignment check - plus red herrings such as single-sample spikes, one of them on an ATEX pump.
The knowledge the retiring technicians carried is in five manuals and six policies; junior technicians need it on a
phone, with sources, at the pump. Leadership's constraints (company profile) apply: auditable actions, safety first,
customer data minimised, and an average cost per resolved request below $0.40.

The day's labs are the building blocks of that assistant:

```
technician question ──> retrieval (BM25 over sectioned manuals, contextual headers) ──> cited answer   (labs 03, 05)
          │                                  cached manual library / stable prefix       (labs 01, 02)
          └──> diagnostic agent ── search_manuals / read_section ── query_telemetry (aggregates) ── work orders
                    │                                                                      (lab 04)
                    ├── long sessions: trimming / tool-result clearing / compaction        (lab 06)
                    └── per-customer memory: site notes and preferences, PII-guarded       (lab 07)
```

Design decisions and the evidence behind them: keep the 11 documents in a cached prefix for quick Q&A (10K tokens;
section 3), but give the diagnostic agent retrieval *tools* because diagnosis needs data and several lookups (lab 04);
never paste telemetry, aggregate it (sections 4 and 1); measure retrieval before tuning prompts (lab 05); clear raw
results in long reviews but protect the asset register (lab 06, exercise 9); and write site notes to a guarded,
per-customer memory rather than into prompts or the CRM (lab 07).

---

## Lab walkthrough

### Lab 01 - `01_token_budget.py`: the context budget

1. **Count every component** with `count_tokens` (each document's marginal count is its count minus a one-character
   baseline). The budget table prints tokens, characters per token, share of the 1M and 200K windows, and the price of
   carrying each component on one request.
2. **Price one question** four ways. *Mock mode:*
   ```
   stuff corpus, no caching                       $0.0622  $62.16   $1,865
   stuff corpus, cached prefix (steady state)     $0.0153  $15.32     $460
   RAG: top-4 sections, no caching                $0.0142  $14.25     $427
   RAG: cached system + top-4 sections            $0.0131  $13.14     $394
   ```
   At this corpus size the answer's 400 output tokens ($0.01) dominate once the prefix is cached: retrieval saves little
   money here. It saves a lot when the corpus is 100x larger.
3. **Agent-loop math.** The quadratic re-send table (10 turns: 29,675 input tokens, 73% saved by caching; 1 turn: -25%).
4. **Estimate vs bill.** One real call: `count_tokens predicted 10,200 input tokens; the request was billed 10,200`.
   Live, the two agree closely (count_tokens is documented as an estimate) and both differ from the mock's.

### Lab 02 - `02_prompt_caching.py`: caching the manual library

1. **Writes, then reads.** *Mock mode:*
   ```
   request  uncached in  cache write  cache read  out  cost
   Q1                22        6,477           0  250  $0.0468
   Q2                26            0       6,477  208  $0.0086
   ```
   Total $0.0735 vs $0.1529 uncached (52% less; 61% on the input side).
2. **The silent invalidator.** A `Current time: ...` line at the top of the system prompt: every request writes 6,489
   tokens and reads 0 - each costs more than no caching at all. The lab prints the sha256 of both system prompts and
   the first differing character (inside the timestamp). The fix - timestamp moved into the user turn - reads the
   library again immediately, because the entry written in step 1 is still warm.
3. **Economics.** The break-even table (5-minute TTL pays from 2 requests, 1-hour from 3), the "one question every 12
   minutes" comparison, a real 1-hour write (`ephemeral_1h_input_tokens=3,860`, billed at 2x), and caching vs batching.
4. **Automatic caching in a chat.** Four turns with the explicit system breakpoint plus top-level `cache_control`:
   read share 99-100% from the first turn (the library is already cached), writes of 28-99 tokens per turn - the
   healthy-loop signature. The follow-up "And on the KP-400 split-case pumps?" is answered from context.
5. **Mid-conversation system message.** *Mock mode:*
   ```
   {"role": "system"} message            0          151       6,698  208  $0.0095
   edited top-level system               0        6,845           0  271  $0.0496
   ```
   Same instruction, five times the price when you edit the top-level system prompt.

### Lab 03 - `03_rag_with_citations.py`: retrieval with verifiable citations

1. **Index.** 74 sections from 11 documents (median 376 characters; the largest is the 2,545-character fault-code table).
2. **Retrieve and answer.** For "A KP-400 on a steel-frame skid reads 4.1 mm/s. Do we need to shut it down?", BM25
   ranks *Installation notes* first and *Operating limits (flexible foundation)* third - a reminder that k = 1 would
   have failed. The answer, *mock mode:*
   ```
   4.1 mm/s is in the alert band for a KP-400 on a flexible foundation (normal up to 3.5, alert
   3.5-7.1, alarm/shutdown above 7.1 mm/s), so no shutdown is needed yet.[1]
   [1] IOM-KP400 §4. Operating limits (flexible foundation) (char_location, chars 68-170) verified
   ```
   All five citations across the three questions are verified: `cited_text` equals the slice of the document we sent.
3. **Search results.** The F05 question with `search_result` blocks returns `search_result_location` citations with
   `source` `kb://kc1_controller_manual#5`; because the fault table was sent as a single block, the citation returns
   the whole table - split tables into row blocks when you need row-level citations.
4. **Long context.** All 11 documents with a cache breakpoint on the last one: 10,182 prompt tokens per question, $0.0706
   for the first (cache write) and ~$0.0125 for the next two, versus $0.009-0.014 for RAG. Long context also cited a
   passage retrieval had missed (the vibration guide's response procedure) - recall for tokens.

Live mode: Claude words the answers itself and may cite more or different sentences; the verification code is the same.

### Lab 04 - `04_agentic_search.py`: agentic search vs one-shot RAG

The targets come from `ground_truth.json` (the pumps labelled *bearing_wear* and *cavitation*). For HF-KP250-03 the agent,
*mock mode*, fans out three tool calls in its first turn, asks for daily trends because the summary suggests bearing wear,
searches the manuals twice (the failure signature, and "KP-250 vibration operating limits rigid foundation"), reads three
sections in parallel, and submits:

```
- Vibration median rose from 1.66 to 4.32 mm/s (first vs last 7 days, +160%); last 24 h median 5.03 mm/s.
- Bearing temperature rose together with it: 61.4 -> 82.1 °C (7-day medians).
- Daily vibration median first exceeded the 4.5 mm/s alarm limit on 2026-09-12.
```

For GB-KP400-02 it asks for *hour-of-day profiles* instead - vibration 4.57-5.15 mm/s between 01:00 and 04:00 against
~2.21 during the day, pressure fluctuation ±10.5-13.9% at night - and applies the KP-400's own limits (alert 3.5 mm/s on a
flexible foundation), not the KP-250's. The scoreboard:

```
HF-KP250-03  agentic (tools)         bearing_wear  yes   6   22,155   1,729   $0.0862
HF-KP250-03  one-shot RAG            undetermined  NO    1      547     309   $0.0105
HF-KP250-03  one-shot RAG + raw CSV  bearing_wear  yes   1   11,988     319   $0.0679
```

One-shot RAG retrieved with the question's words ("diagnose HF-KP250-03 ...") and got generic sections (a KP-400 safety
note, the warranty's coverage clause) - it cannot know which symptoms to look up. Live, Claude chooses its own path
(it may read other sections or use `raw` once); compare the scoreboard, not the trajectory.

### Lab 05 - `05_retrieval_eval.py`: measuring retrieval (no API calls)

```
variant              hit@1  hit@3  hit@5  MRR@10  ~tokens in top-3
sections + headers    0.70   0.80   0.90   0.787               455
sections, body only   0.65   0.75   0.80   0.719               448
windows, body only    0.60   0.80   0.90   0.725               435
windows + headers     0.65   0.85   0.90   0.762               438
```

Headers help both chunkings; windows with headers fix the fault-table question (q05: rank 4 -> 1) but lose section
context elsewhere (q10, q11: rank 1 -> 2); q06 ("fault history remotely" vs the *Diagnostic log* section) and q18
("lowest flow ... continuously" vs "minimum continuous flow") fail everywhere - vocabulary and morphology, the
motivation for exercise 8, where stemming and a subword retriever lift MRR from 0.787 to 0.907.

### Lab 06 - `06_context_editing_and_compaction.py`: long sessions

The baseline table shows the context growing by ~16.7K tokens per site. Clearing, *mock mode:*

```
turn  history sent  model read  cache read  cache write  context management
   4        34,574      17,991       1,308       16,683  cleared 4 results (16,607 tok)
   5        51,290      18,558         736       17,822  cleared 8 results (32,780 tok)
```

Your process still holds 51K tokens; the model reads 18.5K. Compaction fires on the request after the third site
(`iterations [('compaction', 51291, 286), ('message', 0, 901)]`), the lab prints the compaction block, and the
follow-up question works because the full `response.content` was appended. The comparison table and the
break-even projection are discussed in section 5. Live, compaction may fire earlier (real CSV tokenises denser), and
Claude's summary will be far richer than the mock's.

### Lab 07 - `07_memory_tool.py`: memory that survives sessions

*Mock mode* (the stand-in deliberately dictates the contact's details into its first write, to exercise the guard):

```
memory.view /memories -> ok
memory.create /memories/site_notes.md -> BLOCKED: personal data: phone number, person name
memory.create /memories/site_notes.md -> ok
memory.create /memories/technician_preferences.md -> ok
```

Session 2 is a new conversation that recalls the Sunday 06:00-10:00 stop window, the hot-work permit, the two BRG-6309
bearings in the site store and "Site contact wants an SMS before we arrive (contact details are kept in the CRM, not in
memory)". Session 3, at Cobalt Chemical, finds an empty memory. Step 4 attacks the guard - `../` traversal, `%2e%2e`,
a path outside `/memories`, an email address, a contact's name as a file name, a symlink out of the root - and every
attempt is `BLOCKED`; step 5 prints the audit log (operations, never content). Live, Claude usually omits the phone
number without being stopped - which is exactly why the guard lives in code.

---

## Key takeaways

1. The context window is a budget of money, latency and attention. Count it with `count_tokens`, per model; 1M is
   capacity, not a target.
2. Agents re-send their history every turn: input cost grows with the square of the turns. Caching reprices the
   re-sent part at 0.1x; only a shorter history removes it.
3. Caching is a byte-exact prefix match. Order content by stability, freeze the system prompt, sort what you serialise,
   put an explicit breakpoint on the static prefix plus automatic caching on the tail, and verify from `usage` in CI.
4. TTL follows traffic: under 5 minutes between requests, the 5-minute TTL; 5 to 60, the 1-hour TTL; a broken cache
   costs more than no cache.
5. For a small, stable corpus, cached long context is a legitimate answer. Retrieval earns its complexity with size,
   churn and precision - and must be measured (hit@k, MRR) before prompts are tuned.
6. Chunk by structure, index with contextual headers, keep BM25 for identifiers, add semantic retrieval and reranking for
   paraphrase, and let an agent search again when one retrieval is not enough.
7. Citations give you verifiable pointers, not proof of support. Verify pointers in code; judge groundedness separately.
8. Structured data goes behind tools that aggregate in code and say what they filtered.
9. Trimming, clearing and compaction keep long sessions inside the window; each edit costs a cache write, and every
   summary can drop something. Protect vital results, instruct the summary, and verify what survived.
10. Memory holds preferences and working notes, scoped per tenant by the harness and guarded in code; facts live in
    their systems of record and documents in retrieval.

## Further reading

* Context windows - https://platform.claude.com/docs/en/build-with-claude/context-windows
* Token counting - https://platform.claude.com/docs/en/build-with-claude/token-counting
* Prompt caching - https://platform.claude.com/docs/en/build-with-claude/prompt-caching
* Batch processing - https://platform.claude.com/docs/en/build-with-claude/batch-processing
* Pricing - https://platform.claude.com/docs/en/about-claude/pricing
* Citations - https://platform.claude.com/docs/en/build-with-claude/citations
* Search results - https://platform.claude.com/docs/en/build-with-claude/search-results
* Embeddings (Voyage AI) - https://platform.claude.com/docs/en/build-with-claude/embeddings
* Context editing - https://platform.claude.com/docs/en/build-with-claude/context-editing
* Compaction - https://platform.claude.com/docs/en/build-with-claude/compaction and
  https://platform.claude.com/docs/en/build-with-claude/compaction-threshold
* Memory tool - https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
* Effective context engineering for AI agents - https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
* Introducing Contextual Retrieval - https://www.anthropic.com/news/contextual-retrieval
* Effective harnesses for long-running agents - https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
