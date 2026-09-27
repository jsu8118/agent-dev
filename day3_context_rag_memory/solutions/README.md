# Day 3 solutions - worked answers

Runnable solutions: `ex03_ex04_cache_math.py` (exercises 3-4), `ex08_hybrid_retrieval.py`,
`ex09_context_budget_guard.py`, `ex10_policy_questions.py` (+ `policy_questions.jsonl`), `ex11_cache_debugging.py`.
Numbers below are from those scripts (the retrieval ones are exact; the API ones are mock-mode estimates).

---

## 1. Which change breaks which cache?

The rule behind every answer: caching is a **prefix match** over the rendered prompt (tools -> system -> messages).
A breakpoint can be read only if every byte before it is identical to a request that wrote it, with the same
model, within the TTL. The API additionally keeps three cache tiers (tools, system, messages) and some parameters
only invalidate their own tier and below (the "invalidation hierarchy" in the prompt-caching docs).

| Change | A (tools) | B (system) | C (messages) | Why |
|---|:---:|:---:|:---:|---|
| a. append a tool | no | no | no | Tools render first; A's own bytes changed and everything after it shifts. Cache-preserving alternatives: tool search with `defer_loading`, or `tool_addition` blocks (beta `mid-conversation-tool-changes-2026-07-01`, Opus 5). |
| b. `tool_choice` -> any | yes | yes | no | `tool_choice` sits in the messages tier: tools and system survive, the conversation is re-processed. Vary it per request if you must; don't expect message reads on that request. |
| c. library revision D | yes | no | no | The system block changed; A precedes it. The conversation after it is invalidated too. Schedule library updates, don't hot-patch mid-session. |
| d. `{"role": "system"}` message | yes | yes | yes | It is appended *after* the cached history, so every earlier entry is intact - that is the whole point of mid-conversation system messages (supported on Claude Opus 5 without a beta). |
| e. effort high -> medium | model-specific | model-specific | no | Thinking/effort changes always invalidate the messages cache; on models that render the thinking configuration ahead of tools and system, those too. Pin effort per route, or use the per-message effort system message (beta `mid-conversation-output-config-2026-07-01`, supported on Opus 5). |
| f. switch to Sonnet 5 | no | no | no | Caches are model-scoped. Use a subagent for cheaper sub-tasks instead of switching the main loop's model. |
| g. enable citations | yes | no | no | The citations toggle is in the system tier (the API adds citation instructions): A survives, B and C don't. |
| h. attach an image | yes | yes | no | Images, like `tool_choice`, invalidate the messages tier - even when added at the end. A surprise worth remembering. |
| i. six minutes later | no | no | no | With the default 5-minute TTL all entries expired (reads refresh an entry, but nothing read it). Entries written with `ttl: "1h"` would survive. |

Tempting wrong answers: "(d) invalidates B because it is a system prompt" - no, it is a *message*; "(h) is just new
content at the end, so C survives" - images are a documented exception; "(i) is fine because nothing changed" - TTLs
are wall-clock.

## 2. Why doesn't Haiku 4.5 cache a 3,000-token prompt?

**What happens:** every model has a minimum cacheable prefix length - 512 tokens on Claude Opus 5, 1,024 on
Claude Sonnet 5, **4,096 on Claude Haiku 4.5**. Below it the API silently skips caching: no error,
`cache_creation_input_tokens = 0` and `cache_read_input_tokens = 0`. The minimum is not monotonic across model
generations, so a migration can switch caching off without any code change.

**How to confirm:** count the prefix with `client.messages.count_tokens(model="claude-haiku-4-5", ...)` (the count is
model-specific - never estimate with another vendor's tokenizer) and read the `usage` fields of two identical
requests. Lab 02's mock enforces the same per-model minimums, so you can reproduce it offline.

**Options, with costs** (Haiku 4.5: $1 / MTok input):

* *Accept it.* 3,000 uncached tokens cost $0.003 per request - at 10,000 requests/day, $30/day. Caching would save
  at most ~90% of that ($27/day). Often fine.
* *Grow the stable prefix past 4,096 tokens with useful content* - more few-shot examples, the full label guide -
  if it also improves accuracy. Then each request reads ~4,100 tokens at 0.1x ($0.00041) plus one 1.25x write per TTL
  window; from the second request in a window it is cheaper than 3,000 uncached tokens. Padding with filler is the
  same arithmetic but spends attention on noise; prefer content that earns its place.
* *Route differently:* Sonnet 5 (1,024 minimum) or Opus 5 (512) cache this prompt; compare cost per *correct*
  classification, not per token.

## 3. 5-minute vs 1-hour TTL

Prefix price: 20,000 tokens x $5/MTok = **$0.10** per request uncached. Units below are multiples of it.

| pattern | requests | no cache | 5-min TTL | 1-hour TTL | cheapest |
|---|---:|---:|---:|---:|---|
| A: every 4 min | 150 | $15.00 | **$1.61** (1.25 + 149 x 0.1) | $1.69 (2 + 149 x 0.1) | 5-min |
| B: 6 in 2 min, every 30 min | 120 | $12.00 | $3.50 (20 x (1.25 + 5 x 0.1)) | **$1.39** (2 + 119 x 0.1) | 1-hour |
| C: 12 irregular | 12 | **$1.20** | $1.50 (12 x 1.25) | $1.26 (6 writes x 2 + 6 reads x 0.1) | none (1-h ~ break-even) |

* **A:** consecutive requests start 4 minutes apart, so every read refreshes the 5-minute entry and it never
  expires during the shift; the 1-hour TTL only adds the more expensive first write.
* **B:** the 5-minute entry dies in every 28-minute lull, so each burst pays a new write; the 1-hour entry is
  refreshed every 30 minutes and survives all day. This is the "5-60 minute gap" case the 1-hour TTL exists for.
* **C:** gaps of 15-85 minutes. The 5-minute TTL is pure surcharge (every request writes at 1.25x). With the 1-hour TTL,
  gaps of 70-85 minutes still miss and each miss pays 2x - six of the eleven gaps are over an hour, so it lands at
  break-even. At this volume caching is irrelevant (a dollar a day); don't add complexity for it.
* **Poisson version of C** (mean gap 50 min): P(next request within the TTL) = 1 - e^(-TTL/50): 0.10 for 5 minutes,
  0.70 for 60 minutes. Expected cost per request = P x 0.1 + (1 - P) x write: **1.14x** with the 5-minute TTL (worse than
  no cache) and **0.67x** with the 1-hour TTL. Irregular traffic with the same mean can land anywhere between - simulate
  your real timestamps (the script's `ttl_walk`) instead of reasoning from the average gap.

## 4. A 20-turn agent

a. Turn t sends P + (t-1)D = 5,000 + (t-1) x 1,200 tokens. Total = 20 x 5,000 + 1,200 x (20 x 19 / 2) =
   100,000 + 228,000 = **328,000 input tokens** = $1.64, plus 20 x 350 = 7,000 output tokens = $0.175 -> **$1.82**.
   The history term grows with T²: double the turns, roughly quadruple the history cost.
b. With automatic caching, turn 1 writes 5,000 tokens (1.25x); every later turn reads what the previous turn sent
   (0.1x) and writes only the new 1,200 tokens (1.25x). Reads total 300,200 tokens, writes 27,800:
   (300,200 x 0.1 + 27,800 x 1.25) x $5/MTok = $0.324 input + $0.175 output = **$0.50 - 73% cheaper**. Output tokens are
   never cached, so the saving on the whole bill is smaller than on input.
c. A clearing pass rewrites the history from the first cleared block on, so that request pays a large write
   (the solution assumes, conservatively, that only the 5,000-token static prefix is still read: 10,200 - 5,000 =
   5,200 tokens re-written at 1.25x, versus 1,200 on a normal turn). From then on every turn reads 8,000 fewer
   tokens. Net: **$0.486 vs $0.499** - a 3% saving, because 8 more turns follow. Had it happened at turn 19, it
   would have *cost* money. That is why Anthropic describes context editing as a context-window tool rather than a
   savings lever: use it to stay inside the window (and away from context rot), set `clear_at_least` so each cache
   break buys a lot of space, and clear rarely, in large batches.

## 5. Retrieval for 50,000 pages across 300 variants

**Size first.** 50,000 pages at ~500-700 tokens/page is 25-35M tokens: 25-35x the largest context window. Even one
variant's documentation (~170 pages, ~100K tokens) fits in a 1M window, but re-reading it per question costs ~$0.50
uncached (~$0.05 cached) and still exposes the model to ~99% irrelevant text. So: retrieval is mandatory; long context
remains a tool for *one variant's core documents within one session* (cached), not the architecture.

**Proposed architecture**

1. *Ingestion:* layout-aware PDF-to-text; OCR for scans (flag low-confidence pages); tables kept as tables, one row
   group per unit; figures captioned. Every chunk carries metadata: product family, variant(s), document type,
   revision, effective date, language, page number, section path.
2. *Chunking by structure:* section-level chunks (split long sections and big tables into row groups, as lab 05's
   F10 failure showed), each with a deterministic contextual header "family > variant > document > section" -
   optionally an LLM-written context sentence per chunk (Anthropic's Contextual Retrieval: -49% top-20 retrieval
   failures with contextual BM25 + embeddings, -67% with reranking; generating the context is cheap with caching).
3. *De-duplication across variants:* store shared sections once with a list of applicable variants, instead of 300
   near-identical copies that crowd the top-k with duplicates.
4. *Hybrid index with filters:* BM25 for part numbers, fault codes and torque values; dense embeddings
   (Voyage `voyage-4` family or an open-source model) for paraphrase and synonyms; both in a store that supports
   metadata filters (OpenSearch/Elasticsearch hybrid, Postgres `tsvector` + pgvector, Qdrant, Weaviate). Resolve the
   asset -> variant from the CMMS and **filter** to that variant + shared content before ranking.
5. *Rerank* the fused top-50 with a cross-encoder reranker (e.g. Voyage `rerank-2.5`) down to the top 5-8.
6. *Answering:* pass chunks as `search_result` blocks (`source` = `doc-id#page=17`, citations enabled); the UI turns
   `search_result_location` citations into deep links to the page. For diagnostic questions, expose retrieval as tools
   (`search_manuals(query, variant, doc_type)`, `read_section(id)`) so the agent can search again with the manual's
   vocabulary; cap it at 2-3 turns for the 10-second budget and stream the answer.
7. *Freshness:* re-index changed documents incrementally each quarter; keep revisions; answer from the current one
   and show the revision in the citation.

**Comparison**

| option | verdict |
|---|---|
| long-context stuffing | impossible for the corpus; per-variant core docs in a cached prefix is a useful *complement* |
| BM25 only | excellent on codes and part numbers, misses paraphrases ("lowest flow" vs "minimum continuous flow") |
| embeddings only | good on paraphrase, weak on exact identifiers and numbers; opaque failures |
| hybrid + rerank + variant filter | the default for this corpus |
| agentic search on top | best for multi-step diagnosis and ambiguous questions; more latency and tokens - use it where it earns them |

**Evaluation:** a labelled set per product family (hit@k, MRR, and "correct variant" rate - a right answer from the
wrong variant is a wrong answer), a groundedness check on sampled answers (does the cited text support the claim - an
LLM judge calibrated against human labels), end-to-end accuracy on real technician questions, and the "no answer
found" rate. Re-run everything on each quarterly re-index before switching traffic to it.

## 6. Memory, RAG, system of record, or nowhere?

| # | item | where | why |
|---|---|---|---|
| 1 | stop window Sundays 06:00-10:00 | **memory** (site notes) | a working note, not personal data, not in any system of record; exactly what PRV-004 §3 allows |
| 2 | contact's mobile number | **CRM** via an approved tool | customer personal data: never in long-term memory (PRV-004 §3) |
| 3 | KP-250 regreasing interval | **RAG** over the manual | authoritative, versioned content; a copy in memory would go stale silently |
| 4 | bearings replaced (WO-24533) | **CMMS** via `list_work_orders` | the system of record; duplicating it in memory creates two truths |
| 5 | contact prefers an SMS | **memory** (without the number) | a preference, explicitly allowed ("prefers email updates") |
| 6 | open-order PO number | **conversation only** (from the ERP) | needed for this request's identity check, not afterwards |
| 7 | T. Brooks prefers N·m | **memory** (technician profile) | an employee's working preference; keep it in the technician's scope, not the customer's |
| 8 | supplier bank details | **nowhere** - flag to security | a fraud pattern (SOP-SUP-007 §5, FIN-AP-010); never store or act on it |
| 9 | guest Wi-Fi password | **nowhere** (or a secrets vault) | secrets never belong in model-readable memory |
| 10 | last week's diagnosis summary | **memory** as a dated working note, or the CMMS | useful continuity; give it an expiry and link the work order |

**Governance controls for the memory store:** per-tenant roots chosen by the harness from the authenticated channel
(never a model-supplied customer id); path confinement; a PII guard in code (regexes + a dictionary of known names
from the CRM - lab 07); size caps; an audit log of every operation without the content; retention/expiry (delete
notes untouched for N days) and a way to erase on request; periodic review of what is stored; no secrets, ever.

## 7. Pick the long-running strategy

| agent | strategy | configuration and reasoning |
|---|---|---|
| a. 6-hour migration | **memory tool + compaction** (+ light clearing) | Decisions from hour 1 must survive for hours: write them to a `/memories/decisions.md` log as they are made (memory survives any context reset). Compaction keeps the loop inside the window; steer it with `instructions` ("keep every naming rule and conversion decided"). `clear_tool_uses` with a high trigger and `clear_at_least` removes bulky, already-processed plan dumps. |
| b. 10-minute poller | **clearing** (or client-side replacement) | Only the latest reading matters: `clear_tool_uses_20250919` with `keep` = one poll's worth of results, a trigger well above the steady state and `clear_at_least` so clears are rare. Alarms go to a work order (system of record) or a memory note, never only into the context. Compaction is unnecessary: there is nothing to summarise. Even simpler: a fresh short conversation per poll with a one-line state digest. |
| c. warranty assistant | **no clearing of policy results + re-read on demand** | Summaries paraphrase, and the audit needs verbatim clauses. Exclude the policy tool from clearing (`exclude_tools`) or re-retrieve the clauses at the end; if compaction is needed, instruct it to keep clause IDs, then re-fetch the text by ID (or keep verbatim quotes in a memory file) before writing the audit note. |

The general rule: context management decides what the model *currently sees*; anything that must survive
arbitrarily long belongs outside the context (memory or a system of record) with the context holding a pointer.

## 8. Hybrid retrieval

`solutions/ex08_hybrid_retrieval.py` implements a character 4-gram TF-IDF retriever, reciprocal rank fusion and a
tiny stemmer. Results on the 20 lab 05 questions:

| retriever | hit@1 | hit@3 | hit@5 | MRR@10 |
|---|---:|---:|---:|---:|
| BM25 (lab 05 baseline) | 0.70 | 0.80 | 0.90 | 0.787 |
| char 4-gram TF-IDF | 0.75 | 0.95 | 0.95 | 0.850 |
| RRF(BM25, 4-gram) | 0.70 | 0.90 | 1.00 | 0.814 |
| RRF weights 1:2 | 0.75 | 0.95 | 1.00 | 0.854 |
| BM25 + stemming | 0.85 | 0.95 | 0.95 | 0.899 |
| RRF(BM25 + stemming, 4-gram) | 0.85 | 0.95 | 0.95 | 0.907 |

* **What moved and why:** q18 ("lowest flow ... continuously") goes from rank 8 to 1 with n-grams or stemming because
  "continuously" and "continuous" now share units; q13 ("leaks") similarly; q06 ("fault history") improves only
  because "fault" now matches "faults" in the diagnostic-log section - "history" still matches nothing.
* **Losers exist:** stemming pushed q05 (F10) from rank 4 to 7. Always read the per-question table.
* **Fusion is not free lunch:** plain RRF improved hit@5 (recall) but not hit@1; weighting the stronger retriever
  helped. Tune weights on a held-out set - 20 questions find failure modes, they do not prove 0.01 MRR differences.
* **What no lexical method fixes:** true synonyms ("history" ~ "log", "cabinet" ~ "enclosure"). That needs dense
  embeddings, LLM query rewriting into the manual's vocabulary, or an agent that searches again after a miss.
* **Scale caveat:** on a 74-chunk corpus n-grams look great; on millions of chunks they get noisy (common n-grams
  everywhere) and BM25's precision matters more - which is why production hybrids pair BM25 with *semantic*
  embeddings and a reranker.

## 9. A context budget guard

`solutions/ex09_context_budget_guard.py` (run in mock mode, budget 25,000 tokens):

| turn | tokens before | tokens sent | results trimmed |
|---:|---:|---:|---:|
| 3 | 18,057 | 18,057 | 0 |
| 4 | 34,576 | 18,074 | 4 |
| 5 | 34,790 | 18,737 | 4 |

The final report still covers all 12 pumps (10 with numbers; 2 are standby units). Design choices:

* **Count, don't estimate:** `count_tokens` uses the model's tokenizer and includes tools and system; char-based
  estimates are off by 20-40% on CSV-like content.
* **Oldest first, whole turns, never the newest results:** the model has not read the newest results yet; pruning a
  whole user message at a time means one cache break per prune instead of one per block.
* **Digests in code:** deterministic, free, and they keep the numbers the model may need (median/max, spikes, zeros).
* **`exclude_tools` and a size floor:** the first version of this guard also trimmed the `list_assets` result
  (1.9K characters); the agent lost its asset register and silently reviewed 8 pumps instead of 12. Small, vital
  results stay verbatim - the same reason server-side clearing has `exclude_tools`.
* **Fail loudly:** if everything trimmable is trimmed and the request is still over budget, the guard reports it -
  that is the signal for compaction or for splitting the work across subagents.

## 10. A new question set

`solutions/policy_questions.jsonl` has 10 policy questions (refund approval for "$7,500", "gmail address wants
invoice details", "smoke from the cabinet", ...). Results:

| variant | policy hit@1 | policy hit@3 | policy MRR | lab 05 hit@1 | lab 05 MRR |
|---|---:|---:|---:|---:|---:|
| sections + headers | 0.60 | 0.90 | 0.733 | 0.70 | 0.787 |
| sections, body only | 0.60 | 0.90 | 0.733 | 0.65 | 0.719 |
| windows, body only | 0.80 | 0.80 | 0.825 | 0.60 | 0.725 |
| windows + headers | 0.80 | 0.80 | 0.825 | 0.65 | 0.762 |
| sections + stemming | 0.70 | 0.90 | 0.825 | 0.85 | 0.899 |
| RRF(stemmed, 4-gram) | 0.80 | 1.00 | 0.883 | 0.85 | 0.907 |

Different winners: on manuals, headers matter (the product and section names live in headings) and sections beat
windows; on policies, headings add few new words and windows win at rank 1 because they isolate the one list item
that answers the question. p05 ("How long is a pump covered after it ships?") fails for plain BM25 - the table says
"Pumps", "Warranty period" and "ship date" - and stemming recovers it (rank 2). Lesson: keep separate question sets
per content type and report them separately; an average over a set that is 80% manuals hides this.

## 11. A cache that never hits

`solutions/ex11_cache_debugging.py` finds three invalidators by rendering two consecutive requests in API order
(tools -> system -> messages), stripping `cache_control`, and printing the first divergent bytes:

1. **Tool order** changes between requests (tools assembled from plugins in arbitrary order). Tools render first,
   so nothing after them can ever be read. Fix: sort tools by name.
2. **A timestamp** at the top of the system prompt. Fix: remove it, or move it after the last breakpoint (the user
   turn, or a mid-conversation system message).
3. **`json.dumps(settings)` without `sort_keys=True`** on a dict whose key order is not guaranteed: same data,
   different bytes. Fix: `sort_keys=True`, and move per-site settings after the breakpoint anyway (they differ per
   site; the manual library does not).

After the fix: write 7,327 tokens once, then read 7,327 on each following request. The regression check - "the
second identical request must show `cache_read_input_tokens > 0`" - belongs in CI, because caching regressions are
silent. On the Claude API, the cache-diagnostics beta (`cache-diagnosis-2026-04-07`) reports where two requests
diverged without payload logging; the byte diff works on every platform.

## 12. What does a citation prove?

a. A `char_location` citation guarantees a **valid pointer**: `cited_text` is extracted by the API from the document
   you sent, at the given character range (0-indexed, end exclusive), so it cannot be a hallucinated quote - and it
   is not billed as output (nor as input when sent back). It does **not** guarantee that the cited text *supports*
   the claim next to it: the model can over-generalise, combine two sources wrongly, or cite a nearby sentence.
   Lab 03's check verifies the pointer; groundedness needs a second check (an LLM judge or an entailment model on
   samples, calibrated against human labels - Day 6).
b. Citations interleave citation metadata with generated text, block by block; structured outputs constrain the
   entire response to one JSON document. The two output shapes are incompatible, so the API rejects the combination
   with a 400.
c. Two designs:
   * **Two steps:** (1) a cited answer with documents/search results (citations on); (2) a second call without
     documents that extracts the JSON draft from that answer with `messages.parse` (structured output), given a
     numbered list of the citations so `references` can point at them. Validate that every reference exists. Costs
     one more (small) call; keeps API-verified quotes.
   * **One step with a strict tool:** a `create_work_order` tool whose schema includes `references: [{chunk_id,
     quote}]`; you verify each quote by substring match against the chunk you sent (like lab 03's `verify`). One call,
     but the quotes are model-generated (billed as output, and possibly paraphrased) - your verifier replaces the
     API's guarantee.
