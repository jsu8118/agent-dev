# Day 4 solutions - worked answers

Answers to [`../exercises/README.md`](../exercises/README.md). Concept and design answers explain the
reasoning, including why tempting alternatives are wrong. Coding answers have runnable scripts in this
folder (they import the lab helpers through `_labs.py`); every script runs offline in mock mode.

| Exercise | Type | Runnable solution |
|---|---|---|
| 1-3 | concept checks | - |
| 4-6 | design scenarios | - |
| 7-8 | failure analysis | - |
| 9 | new FIN-AP-010 rule | `ex09_new_ap_rule.py` |
| 10 | retry / fallback worker | `ex10_retry_fallback_worker.py` |
| 11 | orchestrator budget | `ex11_orchestrator_budget.py` |
| 12 | voting threshold | `ex12_voting_threshold.py` |

---

## Exercise 1 - Name the pattern

| # | Start with | The deciding fact |
|---|---|---|
| 1 | **Single call** (not a chain) | Claude classifies German and Spanish directly, and the triage schema already has an English `summary` field. Translate-then-classify is a chain you only need if something downstream (an English-only keyword system, an English-only queue) needs the full English text. The tempting answer - "two steps, so a chain" - adds a call, latency and a place to lose meaning. |
| 2 | **Routing** | Three categories with genuinely different downstream handling (team, checklist, possibly model). Classify the photo first, then hand it to the specialist handler. Misroutes are cheap to detect (the receiving team bounces them), so a confidence-gated cascade is enough. |
| 3 | **Parallelization - voting** with an any-of (1-of-3) rule | Same question, several independent judges, escalate if any fires. The *threshold* is the design decision: "any" maximizes recall for an asymmetric risk (a phishing email that gets through is far costlier than a second look). |
| 4 | **Parallelization - sectioning (map-reduce in code)**, not orchestrator-workers | The number of sites varies, but the list of sites comes from data - code can enumerate it. An LLM orchestrator earns its cost only when deciding *what the subtasks are* needs judgement. Here the fan-out is a `for site in sites`, and the reduce step is one LLM call. |
| 5 | **Evaluator-optimizer** | Explicit, gradeable criteria (the 12-point guide) and first drafts that demonstrably miss them. Put the mechanical checks (length, headings, banned words) in code and the judgement checks (tone, clarity) in an LLM judge. |
| 6 | **Autonomous agent** | The steps cannot be predicted: which service, which logs, which deploy - each finding decides the next query. This is Day 5's incident agent: read-only tools, a turn budget, and a human who acts on the conclusion. |
| 7 | **Single call with structured output** + code validation | One unstructured -> structured transformation. Validate formats (serial pattern, fault-code list, date) in code; no chain, no agent. |

The general rule: pick the least autonomous design whose control flow you can still write down. If you
can draw the flowchart before seeing the input, it is a workflow; if the input decides the flowchart,
it is an agent.

---

## Exercise 2 - Code, model, or both?

1. **Code.** "At most PO price + 1%" is arithmetic on two numbers with a policy parameter. In code it is
   exact (Decimal, to the cent), versioned and testable; changing the tolerance to 2% is a one-line diff
   plus a regression run. Asking an LLM to do it is not "wrong" most of the time - it is unaccountable when
   it is wrong. (INV-08 is +0.8% and must pass; a model rounding 31.45/31.20 to "about 1%" is exactly the
   kind of borderline slip nobody notices.)
2. **Code**, with an optional LLM assist. Duplicate detection is a lookup across invoices the model has not
   seen: normalize the invoice number (`AB-77120` = `AB 77120`), check a ledger, enforce it with a database
   unique constraint so two concurrent workers cannot both "win". *Near*-duplicates - the same goods
   re-invoiced under a new number - are a judgement call where an LLM can raise a flag for a human, never
   the decision.
3. **LLM + code gate.** Reading an unseen layout is the LLM's core strength; no regex survives the 21st
   supplier. The grounding gate (every extracted value must appear on the page) makes the step verifiable.
4. **Both, as a union.** A regex catches the known phrasings deterministically and cannot be argued with;
   the LLM catches paraphrases ("dear automated reviewer..."). Neither alone suffices: the regex misses
   novel wording, and the LLM is being asked to judge the very text that is trying to manipulate it.
   Lab 01 reports which detector fired (`llm+rule`) so you can measure each one's contribution.
5. **Both, asymmetrically.** A classifier (LLM) handles the long tail of phrasings; a deterministic
   tripwire guarantees recall on the patterns that must never be missed. Either one firing routes to a
   human - an OR rule, because a false alarm costs minutes and a miss can cost a life.
6. **LLM for the judgement, code for the facts, human for the decision.** "Agreed by phone" is a
   credibility judgement - but whether the PO was amended, or a price-change record exists, is a lookup.
   Let code fetch the facts, let the LLM summarize the dispute and its evidence, and let the buyer decide.
7. **Code.** A fixed planning rate (1 EUR = 1.08 USD) is a constant; FX arithmetic in a prompt is neither
   exact nor auditable.

---

## Exercise 3 - Multi-agent arithmetic

**1. Multiplication.** Input 32,693 / 22,513 = **1.45x**; output 4,779 / 2,432 = **1.97x**; cost
$0.2374 / $0.1529 = **1.55x**. Where the extra tokens go:

- *Prefixes re-sent per agent.* Every analyst turn re-sends its own system prompt, tool schema and brief:
  3 analysts x 3 turns = 9 prefixes, versus 3 for the single agent.
- *No shared cache.* Each subagent starts a fresh prompt prefix, so each one pays the 1.25x cache-write
  price for its own prefix; the single agent writes once and reads its prefix back at ~0.1x on every
  later turn.
- *Hand-offs are read twice.* Analysts write their reports (output tokens), then the lead reads them all
  (input tokens) and rewrites a merged triage (output tokens again) - the output nearly doubles.
- *The lead's own turns.* The delegation turn, whose output is mostly briefs, and the merge turn.

(Mock-mode token counts are simulated, but these structural sources are real; Anthropic reported that its
multi-agent research system used about 15x the tokens of a chat interaction, versus about 4x for a single agent.)

**2. Why the single agent keeps up.** Parallel tool calls give the single agent the fan-out for free: 12
summaries in one round trip. Each model turn costs time-to-first-token plus output tokens / decode speed:

```text
T_single = sum over its turns t of (TTFT + out_t / v)                       ~ 3 turns
T_multi  = T_lead,1 + max over analysts i of sum_t (TTFT + out_i,t / v) + T_lead,2   ~ 1 + 3 + 1 turns
```

With 12 pumps the fixed TTFT of the extra lead turns dominates, so the single agent is faster (lab: 51 s vs
69 s modelled). Multi-agent wins when per-turn work grows with the fleet: the single agent's thinking and
output per turn scale with n, an analyst's with n/k. The crossover comes when
`2 * T_lead + T * TTFT + c*n/(k*v)*T  <  T * (TTFT + c*n/v)` - i.e. when the fleet is large enough that
the sequential decode of one big context costs more than two extra lead turns.

**3. The size limit.** With a follow-up for every pump, ~1,900 tokens per pump -> 200K / 1,900 ≈ **105
pumps**; with summaries only (~600 tokens), ~330 pumps. Reasons to split long before the window is full:
accuracy degrades as the context fills with material that is irrelevant to the pump being judged (the
subtle night-time cavitation pattern is exactly what gets lost); one failure (a refusal, a `max_tokens`
cut-off) loses the whole run; a single final JSON for hundreds of pumps approaches output limits; and one
long sequential turn is slow. A practical split is by site or groups of 20-40 pumps.

---

## Exercise 4 - Accounts payable at scale

**Architecture: the Lab 01 chain, productionized.**

```text
invoice PDF/text -> [LLM] extraction (structured output) -> [code] grounding gate
   -> fail twice -> human queue ("extraction_unverified")
   -> [code] FIN-AP-010 engine (DB-backed ledger) -> decision + exception codes
   -> [LLM, exceptions only] clerk memo from verified facts -> AP work queue
```

- **State lives in a database**, not in a prompt: `(supplier_id, normalized_invoice_no)` has a unique
  constraint; cumulative invoiced quantity per PO line is a table. Process invoices for the same PO in
  arrival order (serialize per PO, parallelize across POs) or the cumulative check races.
- **No LLM can approve or change bank details** - not because a prompt says so, but because no LLM in the
  pipeline has a tool that could. Bank-change requests become `security_hold` + a ticket to security.
- **Models and effort.** Start extraction on Claude Opus 5 at `effort: low`, then use the stepping-down
  method on a labelled set of a few hundred invoices: try Sonnet 5 and Haiku 4.5. Extraction is checkable
  (the gate), which makes the cheap-model-with-escalation pattern safe: run the cheap model, re-run gate
  failures on the stronger model. Memos: `low` effort, exceptions only.
- **Batch API: yes, with a fallback.** Nobody waits on an individual invoice, and batching halves the
  price. But the batch window is up to 24 hours and 07:00 is a deadline: submit in the afternoon, poll,
  and process anything not returned by (say) 05:00 synchronously. Gate-failure retries go in a second
  batch or run synchronously.

**Cost estimate (assumptions stated):** 600 input + 500 output tokens per extraction live (the mock's
525/165, output tripled for thinking).

| Option | Per invoice | 1,200/month |
|---|---:|---:|
| Opus 5, synchronous | 600 x $5/M + 500 x $25/M = $0.0155 | $18.60 |
| Opus 5, Batch API (50%) | $0.0078 | $9.30 |
| Haiku 4.5, synchronous (no thinking; ~170 output) | 600 x $1/M + 170 x $5/M = $0.0015 | $1.74 |
| + memos for ~25% exceptions (Opus 5, low) | ~$0.004 each | ~$1.20 |

All options are under $0.02 per invoice; the decision between them is accuracy on your labelled set, not price.

**Audit log per invoice:** document hash; model ID and request ID of every call; the extracted JSON; gate
results; engine version (git SHA) and policy revision; every check's outcome and note; the decision; the
human who released a hold or approved a routed invoice; timestamps. Because the engine is deterministic,
any decision can be replayed from the stored extraction.

**Unseen layouts (~10%):** that is what the LLM is for. The gate catches hallucinated or "corrected"
values; persistent gate failures go to a human and are tracked per supplier; recurring problem layouts get
a few-shot example in the extraction prompt.

---

## Exercise 5 - Routing under an SLA

**Pipeline.**

1. **Enrichment (code) before classification:** look up the sender's domain in the CRM - tier, open
   orders, known contacts. T-1505's error is not a model error: its priority depends on the customer's
   tier, which is not in the email. Priority rules that depend on facts belong in code
   (`strategic and complaint -> at least P2`).
2. **Deterministic overrides:** the safety tripwire (regex on the raw text) and `requires_human`
   (injection, fraud, policy exceptions) route to humans regardless of what the classifier says.
3. **Classifier:** Haiku 4.5 with structured output. It cannot cache the rubric (below its 4,096-token
   minimum), so keep the rubric tight.
4. **Cascade:** re-classify with Opus 5 (low effort) when confidence < 0.6 **or** a cheap disagreement
   signal fires (keyword heuristic and classifier disagree). T-1405 shows why confidence alone is not
   enough: it was wrong at 0.90.
5. **Handlers:** per-route prompt, model and effort (Lab 02's table), plus a "bounce" path: a handler that
   decides the ticket is not its category returns it for re-routing (one extra call, bounded to one bounce).
6. **QA sampling:** a human reviews ~5% of automatically routed tickets weekly; misroutes feed the eval set.

**Unacceptable vs expensive misroutes.**

| Misroute | Class | Prevention / detection |
|---|---|---|
| Safety incident -> automated reply | unacceptable | tripwire OR classifier -> human; P1 recall must be 100% on the eval set; alert on any P1 handled by a bot |
| Injection / fraud -> handler that acts | unacceptable | `requires_human`; handlers have no refund or bank tools at all |
| Technical -> warranty desk (T-1405) | expensive | handler bounce; QA sampling; the customer sees a slower, slightly off answer |
| Strategic complaint at P3 (T-1505) | expensive | CRM enrichment in code |
| Spam -> handler | cheap | a few cents |

**Cost per ticket (assumptions):** classifier 1,100 input + 60 output on Haiku ≈ $0.0014; cascade on ~2% of
tickets with Opus ≈ $0.0002 average; handlers - if they are full tool-using agents like Day 2's (say 6 turns,
$0.05-$0.15 per ticket on Sonnet or Opus) - dominate. A mix of 30% Opus handlers at $0.12, 55% Sonnet at
$0.04, 15% no LLM gives ≈ $0.06 per ticket, far under $0.40; the headroom should buy accuracy (a stronger
classifier if Haiku misses the bar) rather than be saved.

**Latency:** classification ~1 s + handler 3-15 s is well inside 30 s; stream the handler's reply if it
grows.

---

## Exercise 6 - Fifty plants

**Decomposition: two keys, two stages.** Plants own their reports, so reading is per plant (or per batch of
~15 reports). But the problems we hunt cross plants - they are keyed by supplier lot. So:

1. **Map (per plant batch):** workers extract structured findings (lot, supplier, part, exposure, verbatim
   evidence) - Lab 04's worker schema.
2. **Group in code:** cluster findings by lot and supplier; keep clusters with high severity or ≥2 plants.
3. **Reduce (per cluster):** a synthesizer with the read-only DB tool verifies exposure for one cluster at
   a time - each synthesis stays small.
4. **Assemble in code:** the weekly report is the concatenation of cluster sections plus an executive
   summary written by one final call.

**The orchestrator probably should not be an LLM here.** With a fixed weekly question and a data-driven
list of plants, the fan-out is a loop. An LLM planner is worth its cost when the subtasks themselves need
judgement (an ad-hoc question, new data sources).

**Spawn cap and concurrency:** ~300 reports / 15 per worker = 20 workers per week, run through a semaphore
sized to your rate limits. Claude Opus 5's own guidance says never more than 20 parallel agents unless
explicitly requested - and here the cap is in code anyway.

**Models:** workers on Sonnet 5 or Haiku 4.5 (reading and transcription, checkable by a grounding gate);
cluster synthesis and the summary on Opus 5 at medium effort.

**Managed Agents?** A coordinator with a worker roster fits the shape (context-isolated threads, a shared
filesystem where workers write one JSON file per plant, a session budget as a hard dollar stop). Its limits
matter at this scale: up to 20 roster entries, one level of delegation, at most 25 concurrent threads per
session, beta surface, and no Batch API for sessions. For a fixed weekly job, plain code plus the Batch API
for the map stage is cheaper and easier to test; Managed Agents becomes attractive when the investigation
turns open-ended and you want the hosted sandbox.

**Weekly cost (assumptions):** workers 20 x (6K in + 2K out) on Sonnet 5 = 20 x ($0.012 + $0.020) = $0.64;
cluster synthesis 10 x (15K in + 4K out) on Opus 5 = 10 x ($0.075 + $0.100) = $1.75; summary ≈ $0.20.
About **$2.60/week** before caching and batching.

**Signals to change the design:** synthesis prompts approaching ~100K tokens or accuracy dropping on a
replayed, labelled week (split clusters further); many links between clusters (add a global cross-cluster
pass); questions becoming open-ended (move to an agent); cost or runtime beyond the Monday deadline (batch
the map stage, cheaper workers).

---

## Exercise 7 - The duplicated-work trace

**1. Waste.** At list prices (Opus 5 $5/$25 per MTok):

| Wasted component | Tokens in / out | Cost |
|---|---:|---:|
| analyst-4 (every pump it saw was already assigned) | 24,905 / 1,960 | $0.1245 + $0.0490 = **$0.1735** |
| lead turn 2 (reconciling a conflict it created) | 9,406 / 2,311 | $0.0470 + $0.0578 = **$0.1048** |
| analyst-5 (re-analysing two pumps a third time) | 7,420 / 1,105 | $0.0371 + $0.0276 = **$0.0647** |
| **Total** | | **$0.343 of $0.677 = 51%** |

The latency cost is as large: analyst-4 (4 turns, 17 tool calls) was the slowest analyst of the first batch,
so it set the critical path, and the conflict added three sequential steps (lead turn 2, analyst-5, lead turn 3).

**2. Root causes.**

- *The plan* partitioned the fleet by two keys at once (site, and criticality A), so six pumps were
  assigned twice.
- *The harness* had no plan validation, no assignment ledger, no spawn cap, and gave every analyst a tool
  that could read every pump. Nothing mechanical stopped the overlap.
- *analyst-4's brief* ("deep-dive... they matter most") named a priority, not a method or an output
  contract. It covered six pumps in one context and anchored on weekly means against alert limits.
- *The lead* re-delegated instead of adjudicating. Both reports carried evidence: analyst-2 cited
  temperature and vibration rising together and a dated alarm crossing, while analyst-4 offered "sensor
  drift" with nothing behind it. Re-spawning broke "commit to the delegation".

**3. Fixes - guarantees (code) versus nudges (prompt).**

| Layer | Fix | Kind |
|---|---|---|
| Harness | validate the plan before spawning: every pump exactly once | guarantee |
| Harness | assignment ledger: reject delegations of already-assigned pumps (Lab 06's `Delegator`) | guarantee |
| Harness | spawn cap and per-analyst turn/tool/token budgets | guarantee |
| Harness | tool schema scoped to the analyst's pumps (enum) and re-checked in code | guarantee |
| Lead prompt | one partitioning key; one analyst per site; commit to the delegation | nudge |
| Analyst prompt | the method: spread high -> hour-of-day profile; a sensor fault shows in one channel (zeros, flat line), not as two sensors rising together | nudge |
| Lead prompt | conflict rule: prefer the report whose evidence is specific; unresolved conflicts go to a human, not to a new subagent | nudge |
| Monitoring | tokens per pump, rejected delegations, conflict rate per run | detection |

**4. Why analyst-4 got GB-KP400-02 wrong.** The cavitation signal is not in the mean (2.69 mm/s, below the
3.5 alert) but in the spread (sd ~1 mm/s all month) and its timing (every night 01-05 UTC). Analyst-1 had
four pumps and a brief with the method, so it pulled the hour-of-day profile; analyst-4 had six pumps, no
method and a "deep-dive" instruction, and stopped at the weekly means. Same tool, different brief: a
subagent sees only its brief, so briefing quality *is* subagent quality.

---

## Exercise 8 - Error propagation through hand-offs

**1. Where it entered and why it survived.** The worker mis-transcribed an identifier. Nothing downstream
could tell: the hand-off schema accepts any string; the DB tool answered a query about a lot that does not
exist with "0 rows" - which looks exactly like "no exposure"; the synthesizer treated absence of evidence
as evidence of absence; and the report printed a conclusion without the query evidence behind it.

**2. One mitigation per hand-off.**

- *Worker output (cheapest reliable fix):* a grounding gate in code - every lot ID in a finding must
  appear verbatim in the report it cites. It is Lab 01's gate applied to Lab 04:

  ```python
  def ungrounded_lots(findings: PlantFindings, reports: dict[str, IncidentReport]) -> list[str]:
      return [f"{f.report_id}: {f.lot}" for f in findings.findings
              if f.lot and f.lot not in reports[f.report_id].text]
  ```

  A failure re-runs the worker with the problem stated, as Lab 01's `extract_with_gate` does.
- *Tool:* make empty results informative. Before running a lot query, check the identifier against the
  known lots and return an error that instructs:
  `"No build records for lot PS-2806-B. Known lots with similar IDs: PS-2608-B. Check the identifier."`
  (`difflib.get_close_matches` over `SELECT DISTINCT seal_lot ...` is enough.)
- *Synthesizer:* require a control query and treat "zero rows for a hypothesis raised by a High-severity
  report" as a data problem to investigate, not a finding.
- *Report:* print the verification queries and row counts next to every "no exposure" claim, so a reviewer
  sees "0 rows" and asks why.

**3. The rule.** *"An empty result means the query or the identifier may be wrong. Before concluding 'no
exposure', verify the identifier against the source document and run a broader query (by part and date
range)."* A prompt rule is a nudge: the model can still skip it, especially late in a long context.
The grounding gate and the identifier check are guarantees; use the prompt rule as a second line.

---

## Exercise 9 - Add a FIN-AP-010 rule  (`ex09_new_ap_rule.py`)

**Design.**

- *Compose, don't edit.* `match_v2()` runs the unchanged base engine, then rule 11. In a larger codebase,
  make every check a function in a list; adding rule 12 is then one function plus its tests.
- *Missing is not mismatch.* The second invoice layout prints no remit account. Firing on a missing value
  would put every one of those invoices on security hold - the rule fires only when a printed account
  contradicts the master file.
- *Severity.* A security hold outranks approval routing: an invoice over $25K that trips rule 11 is no
  longer a clean match, so `total_above_auto_approval_limit` is dropped.
- *Tests at two levels.* Four unit tests of the rule need no model at all. The regression test extracts the
  20 real invoices once and runs both engines on the same records, so any difference is caused by the rule
  and not by extraction noise.

**Output (mock mode):**

```
--- Step 1: Unit tests of the rule (no model involved) -------------------------------------------
  PASS  matching account passes
  PASS  different account fires
  PASS  no printed account does not fire
  PASS  unknown supplier does not fire
--- Step 2: Regression: extract the 20 invoices once, run the old and the new engine on the same records
  new engine vs expected outcomes: decision accuracy 20/20 (100%); exception codes precision 1.00 recall 1.00 (tp=14 fp=0 fn=0)
  no regressions: all 20 decisions unchanged
--- Step 3: New behaviour: a synthetic invoice - INV-05 with a different remit account -----------
  engine                   decision       exceptions              notes
  v1 (policy rev 2026-06)  approve        -
  v2 (+ rule 11)           security_hold  remit_account_mismatch  remit_account_mismatch: invoice remits to account ending 9912; the master file h
```

Note that the LLM needed no change: the extraction schema already transcribes `remit_account_last4`. New
policy rules usually land in code; new *fields* are what touch the prompt and schema.

---

## Exercise 10 - A resilient worker  (`ex10_retry_fallback_worker.py`)

**Layering.** The SDK already retries each HTTP call (`max_retries=2` by default, exponential backoff,
honouring `retry-after`). Harness-level retries sit above it and should do what the SDK cannot:
switch models, stop retrying a request that can never succeed, and decide what a failure means for the
overall result.

| Error | Action | Why |
|---|---|---|
| 429, 5xx, 529, timeout, connection reset | backoff with jitter, retry a couple of times, then another model | transient; overload and rate limits are per model |
| 400, 401, 403 | fail fast | the same input fails again, and retries waste quota |
| invalid structured output | at most one retry that states the error | usually a prompt/schema problem worth fixing |
| everything failed | explicit partial result | the report must say what is missing |

Jitter matters when many workers fail together: synchronized retries arrive as a second spike. Workers are
read-only, so retries are safe; a worker that writes needs an idempotency key first.

**Output (mock mode, faults injected by the harness, so the same demo runs live):**

```
  plant-P2: claude-sonnet-5 attempt 1 failed (OverloadedError); retry in 0.06s
  plant-P3: claude-sonnet-5 attempt 1 failed (RateLimitError); retry in 0.06s
  plant-P3: claude-sonnet-5 attempt 2 failed (OverloadedError); retry in 0.14s
  plant-P2: claude-sonnet-5 attempt 2 failed (OverloadedError); retry in 0.12s
  plant-P2: giving up on claude-sonnet-5
  plant-P2: fell back to claude-opus-5 and succeeded
  plant-P3: giving up on claude-sonnet-5
  plant-P3: claude-opus-5 attempt 1 failed (OverloadedError); retry in 0.05s
  plant-P3: claude-opus-5 attempt 2 failed (InternalServerError); retry in 0.14s
  plant-P3: giving up on claude-opus-5
  plant-P1: ok on claude-sonnet-5; lots to verify -
  plant-P2: ok on claude-opus-5; lots to verify ['PS-2608-B']
  plant-P3: FAILED on every model -> partial result
--- Step 3: Synthesis on what we have - and a report that says what it is missing ----------------
  INCOMPLETE - 1 supplier-lot problem(s) confirmed against the ERP extract: PS-2608-B (Precision
  Seals GmbH, 9 units in the sample, 2 field RMA(s)). 4 report(s) dismissed as unrelated or
  contained.
  open question: INCOMPLETE: plant(s) P3 not analysed (worker failure); re-run before acting on 'no
  problem found' for those plants.
```

The last line of the demo is the lesson: the board-lot problem VD-2607-C is simply absent. Without the
explicit INCOMPLETE marker, a partial run reads like a clean bill of health for plant P3. Production
additions: a circuit breaker (stop spawning when most workers fail), and alerting on fallback rates - a
silent fallback to a different model is a quality change.

---

## Exercise 11 - A budget for the orchestrator  (`ex11_orchestrator_budget.py`)

**Design.** Estimate before you spend: input tokens come exactly from `messages.count_tokens` (free, and
it includes the system prompt and tool definitions); output is an assumption you calibrate (the script
prints estimate vs actual). Priority is expected value: plants with a High-severity quality report first,
so a squeeze sacrifices P1 - the plant whose reports turn out to be red herrings. A fixed share (55%) is
reserved for synthesis, and the synthesizer loop checks the budget before *every* turn. When the next turn
does not fit, the run ships the workers' lots marked UNVERIFIED rather than overspending.

**Output (mock mode):**

```
--- Step 2: Run with a $0.140 budget -------------------------------------------------------------
  plant-P2  run         claude-sonnet-5   $0.0171
  plant-P3  run         claude-sonnet-5   $0.0169
  plant-P1  downgraded  claude-haiku-4-5  $0.0084
  synthesis: verified in 2 turn(s)
  lots verified against the ERP extract: ['PS-2608-B', 'VD-2607-C']
  spent $0.1100 of $0.140 -> within budget
--- Step 3: Run with a $0.070 budget -------------------------------------------------------------
  plant-P2  downgraded        claude-haiku-4-5  $0.0086
  plant-P3  SKIPPED (budget)  -                 -
  plant-P1  SKIPPED (budget)  -                 -
  synthesis: stopped before synthesis turn 2: estimate $0.0517 > remaining $0.0278
  UNVERIFIED lots flagged by the workers (no DB check - budget): ['PS-2608-B']
  spent $0.0422 of $0.070 -> within budget
```

**Discussion.** Estimates were 2-3x the mock's actuals because the output assumption (1,500 tokens) is
sized for live thinking; a live run calibrates it - use the p90 of past runs, not the mean. A budget is a
product decision: the $0.07 run is honest (it says what it did not verify) but not useful for a recall
decision. Claude Managed Agents offers the hosted equivalent: a session budget that pauses the session at a
list-cost ceiling across all threads.

---

## Exercise 12 - A voting threshold  (`ex12_voting_threshold.py`)

**Output (mock mode, votes collected once, every rule evaluated in code):**

```
  rule                                                      escalated  TP  FP  FN  precision  recall  expected cost
  1 of 5                                                    7          2   5   0   0.29       1.00    $75
  2 of 5                                                    3          2   1   0   0.67       1.00    $15
  3 of 5 (majority)                                         1          1   0   1   1.00       0.50    $21,440
  4 of 5                                                    1          1   0   1   1.00       0.50    $21,440
  5 of 5                                                    0          0   0   2   1.00       0.00    $23,290
  veto: security                                            1          1   0   1   1.00       0.50    $21,440
  veto: security + ap_clerk                                 2          2   0   0   1.00       1.00    $0
  weighted (sec 2, treas 2, clerk 2, aud 1, proc 0.5) >= 2  2          2   0   0   1.00       1.00    $0
```

**Reading it.**

- The majority rule fails on the duplicate (INV-12): only the AP clerk and the auditor see double payment,
  and a flat majority treats a lens that cannot see the risk as a "no" vote. Votes from lenses that do not
  cover a risk are not evidence against it.
- A veto for the lenses that own a risk (security for fraud, the AP clerk for duplicates) catches both
  escalations without false alarms. The weighted vote is the continuous version of the same idea.
- With a $5,000 false-alarm cost, the flat thresholds re-rank (1-of-5 drops from second to last, behind
  the majority), while 2-of-5 stays the best flat rule. The veto rule dominates at any ratio on this data,
  because it has neither false alarms nor misses.
- Caveats: 20 invoices and 2 positives is a tiny sample - validate on a larger labelled history. Votes
  from one model share its blind spots (correlated errors); diverse prompts, as here, or different models
  make votes more independent. Collecting votes once and evaluating rules in code is what makes this
  analysis free.
