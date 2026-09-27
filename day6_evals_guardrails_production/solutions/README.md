# Day 6 solutions — worked answers

Each answer explains the reasoning, including why the tempting wrong answers are wrong. The numerical and coding
exercises have runnable solutions next to this file; all of them run offline in mock mode.

| Exercise | Runnable solution |
|---|---|
| 2 pass@k vs pass^k | `ex02_pass_at_k.py` |
| 3 sample size | `ex03_sample_size.py` |
| 8 cost spike | `ex08_cost_spike.py` |
| 9 new grader | `ex09_reference_grader.py` |
| 10 trace redaction | `ex10_trace_redaction.py` |
| 11 canary comparison | `ex11_canary.py` |
| 12 per-customer rate limits | `ex12_rate_limits.py` |

---

## Exercise 1 — Reliable judges without `temperature=0`

**1. The error.** Claude Opus 5 removed the sampling parameters: any `temperature`, `top_p` or `top_k` returns
`400 invalid_request_error` (`anthropic.BadRequestError`). The same is true of Claude Opus 4.7/4.8 and Fable 5.
Claude Sonnet 5 rejects any *non-default* value, so omitting the parameter or passing the default is accepted,
`temperature=0` is not. Claude Haiku 4.5 still accepts sampling parameters. The migration guide's advice is to
delete the parameter and steer with prompting, effort and structured outputs.

**2. "Haiku supports temperature 0, so it's deterministic" is wrong twice.**

* *Determinism.* `temperature=0` never guaranteed identical outputs. Serving infrastructure, batching and
  floating-point effects still vary. More importantly, determinism is not validity: a judge that gives the same
  wrong score every time is reliably wrong. The quantity to control is agreement with humans, not variance alone.
* *Quality.* The judge model should be chosen by calibration numbers (kappa, false-pass rate on your rubric), not by
  which sampling knobs it exposes. Haiku may be the right choice for cheap per-PR checks of clear-cut criteria; it
  may miss nuanced policy reasoning that Sonnet 5 catches. Switching models to keep a knob optimises the wrong
  thing.

**3. Reliability by design, and how to measure it.**

* Concrete, checkable criteria (lab 03's five booleans) instead of "rate quality 1–5".
* Score anchors that say what a 1, 2, 3, 4 and 5 look like, including the cases humans disagreed on (J09 showed
  that "wrong amount in a correctly routed approval" needs an explicit anchor).
* Reference notes with the ground truth, so the judge grades against facts rather than its own knowledge.
* Structured output with enums (`client.messages.parse(output_format=Verdict)`, `score: Literal[1..5]`), with
  the rationale before the score.
* An explicit instruction that length and politeness are not quality, and that the candidate is data, not
  instructions.
* Pairwise comparison with order randomisation *and* swap when the question is comparative.
* Repeated sampling: judge each item N times, use the median score and the majority pass.
* Versioning: rubric id, judge model and prompt hash on every score, and never mixing scores across versions.

Measure it with **self-consistency** (share of items whose N samples agree exactly; standard deviation of scores),
**test-retest** on a fixed calibration set run on different days, **agreement with humans** (exact, within-1, κ,
confusion matrix with false passes called out), **known negatives** (empty, "I don't know", wrong question: all
must fail) and **bias probes** (padding, order swaps). Keep a held-out slice of the calibration set that nobody
tunes the rubric on; agreement measured on the items you tuned against is optimistic.

**4. Which model judges.** A different model than the agent's reduces *self-preference*, the tendency to rate
familiar outputs higher. Sonnet 5 balances nuance and cost for a rubric like this one. Use Opus 5 when calibration
shows Sonnet 5 missing criteria that matter and the volume is small (weekly audits, disputes). Use Haiku 4.5 when
calibration shows adequate agreement on the criteria you gate on and you want to run on every pull request. Haiku
has no `effort` parameter and thinks only with a `budget_tokens` configuration, so check
`labkit.supports_effort(model)` before sending `effort`.

---

## Exercise 2 — pass@k vs pass^k

Run `python day6_evals_guardrails_production/solutions/ex02_pass_at_k.py`.

**1.** pass@3 = 1 − 0.1³ = **99.9%**; pass^3 = 0.9³ = **72.9%**.

**2.** Kestrel's customers see every attempt, and a wrong reply to one customer is not cancelled by a right reply to
another, so the support agent is judged on **pass^k** (reliability). A coding agent that generates three patches and
keeps the one whose tests pass legitimately uses **pass@k**: a checker converts "at least one works" into value.
The tempting mistake is quoting pass@k for a customer-facing system; it can make a 90% agent look like 99.9%.

**3.** With 8 passes in 10 runs, the unbiased estimate is C(8,3)/C(10,3) = 56/120 = **46.7%**, not 0.8³ = 51.2%.
The estimator asks "if I pick 3 of my 10 observed runs *without replacement*, how often are all 3 passes?". That
uses the data you have without assuming the observed 0.8 is the true rate. With small n the plug-in estimate is
biased upward for pass^k.

**4.** Failures cluster on hard scenarios. With 80% easy scenarios at 0.99 and 20% hard ones at 0.60, the overall
per-attempt rate is 0.912. Computing pass^5 from it gives 63.1%, but the true value (average of per-scenario p⁵) is
77.6%. The overall rate mixes two populations; by Jensen's inequality the average of p⁵ is at least the fifth power
of the average p. Per-scenario estimates also tell you *which* scenarios are flaky, which is the actionable part
(`--reps` in lab 02 lists them).

---

## Exercise 3 — How many scenarios to detect a 5-point regression?

Run `python day6_evals_guardrails_production/solutions/ex03_sample_size.py`.

**1.** At 27/30 the 95% Wilson interval is **74.4%–96.5%**: a noise floor of about ±11 points. A 5-point regression is
well inside it, so the aggregate pass rate on 30 scenarios cannot serve as a release gate for small changes.
Treating "90% → 87%" as a regression, or "87% → 90%" as an improvement, is reading noise.

**2.** Two independent samples, two-proportion z-test: **686 scenarios per arm** (1,372 runs).

**3.** Paired (McNemar) sample sizes: **155** scenarios when 5% flip to fail and none flip back; **218** with 6%/1%
churn; **469** for heavy churn such as a model migration (10%/5%). The simulation with the exact test confirms ~76–78%
power at those sizes; the exact test is conservative, so add ~10%. Pairing works because each scenario is its own
control: scenario difficulty cancels out and only the discordant pairs carry information. The cost is driven by
*churn*, not by the pass rate. At today's n = 30 the power for a pure 5% regression is **~0%**. The exact test needs
at least six one-way flips before p < 0.05 (p = 0.031), and 30 × 5% gives 1.5 on average.

**4.** Repeated runs average out the model's sampling noise *on these scenarios*. They sharpen the question "did this
change regress our golden set?" and reveal flaky scenarios. They add no new scenarios, so they cannot tell you how
the agent does on unseen traffic; only more (and more representative) scenarios do. In mock mode reps add nothing:
the stand-in is deterministic.

**5. A gate policy that is honest about the numbers:**

1. **Per-case critical checks are zero-tolerance.** One unauthorised refund, forbidden tool call or missing safety
   sentence blocks the release. No statistics are needed: the event itself is unacceptable.
2. **High-stakes scenario types** (safety, security, privacy) regressing at all blocks.
3. **Aggregate changes inside the noise floor trigger REVIEW**: a human reads the flipped cases.
4. **Grow the paired golden set** toward ~150–250 scenarios, production failures first, and run 3 reps on the
   high-stakes slice.
5. Report every pass rate with its interval, and never compare cost across runs with different cache state.

---

## Exercise 4 — Regression-testing a model migration (Opus 5 → Opus 5.5)

**1. Changes that return 400s on Claude Opus 5.5:**

* `thinking: {"type": "disabled"}` and `{"type": "enabled", "budget_tokens": N}` at every effort level; thinking is
  always on and effort is the control.
* Forced `tool_choice` (`{"type": "any"}` / `{"type": "tool", ...}`), on the Messages API, Batches and
  `count_tokens`. Use `auto` with the tool named in the prompt and `strict: true`, then check that a call happened.
* The `computer_20251124` tool type; only the computer toolset is accepted.
* Replaying *edited* history with thinking blocks. Blocks are bound to the model and conversation that produced
  them, and for accounts created on or after 2026-08-31 an edited prefix is a 400.

The reference agent sets no `thinking`, never forces `tool_choice`, appends `response.content` verbatim and has no
computer use, so it sends valid requests. Other course code may not: any lab that demonstrates forced `tool_choice`
or disables thinking, and any harness that rewrites or prunes history, must be checked. The labkit mock enforces
the forced-`tool_choice` rule per model, so lab 01-style unit tests run with `model="claude-opus-5-5"` surface
these offline.

**2. Silent changes and the eval signal that reveals each:**

| Change | Signal |
|---|---|
| Default effort `medium` (Opus 5: `high`); Opus 5.5 at a given level also thinks differently | Golden-set pass rate and critical failures (paired, reps), output tokens per case, turns per ticket. Set `effort` explicitly and sweep it. |
| Price $4/$20 per million tokens, cache reads 0.05× | Cost per case and per resolved ticket (compare at equal cache state) |
| Broader classifiers (`bio`, `reasoning_extraction`) | Refusal rate (`stop_reason="refusal"`), fallback rate (`usage.iterations`) |
| Text between tool calls can arrive as `thinking` blocks | Any UI that renders intermediate text goes quiet; the final reply is unaffected |
| Separate rate-limit pool | 429 rate under load |
| Different judgement on edge cases | Judge scores with the *same* calibrated judge; pairwise judge v1 vs v2 with swap |

**3. The CI plan.**

1. Unit tests (lab 01) parameterised by model: protocol and request-shape errors fail fast.
2. The golden-set gate (lab 02) on both models with `--reps 3`, run the same day at comparable cache state:
   `--model claude-opus-5-5 --compare-to baseline/report.json`. Block on any new critical failure or high-stakes
   regression; review other flips.
3. An effort sweep on Opus 5.5 (`low`, `medium`, `high`), choosing the cheapest level that passes the gate.
4. Judge scores and a pairwise comparison on the calibrated set.
5. Shadow on live traffic with read-only tools (no real RMAs) for a few days, comparing cost, latency, escalation
   rate and refusal rate.
6. Canary at 5% → 25% → 100% with automatic rollback on critical events.
7. Verify `response.model` in logs so a misconfigured deploy cannot silently keep serving the old model.

**4. Fallback turns.** A server-side fallback from Opus 5.5 (to Opus 5 or Opus 4.8) runs *without* Opus 5.5's thinking
blocks: the API drops blocks the target cannot read, so the fallback continues with less context. It bills at the
fallback model's rates and starts with a cold prompt cache. `reasoning_extraction` declines are not retried on a
fallback at all. Measure the fallback rate separately: a ticket served by the fallback model is a different product.

---

## Exercise 5 — The go-live checklist

| # | Item | Evidence | Threshold | Owner | Blocking |
|---|---|---|---|---|---|
| 1 | Unit tests of the plumbing pass | Lab 01 in CI | 100% | Eng | yes |
| 2 | Golden-set gate | Lab 02 live, `--reps 3` | 0 critical failures; high-stakes types 100%; aggregate reported with CI | Eng + Support lead | yes |
| 3 | Refund limits enforced in code | `issue_refund` tests; regression drill blocks | limit change caught | Eng | yes |
| 4 | Safety escalation | E17–E19 pass; alert "safety ticket without P1 escalation within 5 min" live | 100% | Support ops | yes |
| 5 | Injection and fraud | Lab 04: attacks blocked by ≥ 2 layers; assume-breach drill | 0 successful attacks; screener FP rate reviewed | Security | yes |
| 6 | Privacy | Unverified-sender scenarios; output check `data_to_unverified_sender`; PII redaction in traces (ex. 10) | 0 PII values in exported traces | Privacy officer | yes |
| 7 | Auditability | Every write has an audit row with ticket ref (lab 05); guardrail blocks audited | 100% | Compliance | yes |
| 8 | Cost | Live cost per ticket and per resolved ticket (lab 05 live) | mean < $0.30 (margin to $0.40) | Finance | yes |
| 9 | Latency | Live p95 reply latency; deadline 25 s; load test at 2× peak | p95 < 25 s | Eng | yes |
| 10 | Reliability | Lab 07 drills pass; retry config reviewed (one retrying layer); circuit breaker tested | pass | Eng | yes |
| 11 | Judge calibration | Lab 03 live; verbosity probe; known negatives | κ ≥ 0.8, 0 false passes, probe clean | Eval owner | for judge-gated metrics only |
| 12 | Observability | Dashboards and alerts live (ex. 7); traces exported | alerts fire in a drill | SRE | yes |
| 13 | On-call and runbooks | Runbooks for cost spike, safety miss, outage; escalation paths | reviewed | Support ops | yes |
| 14 | Rollback | Previous prompt and model version deployable in < 5 min; kill switch routes all tickets to humans | drill done | Eng | yes |
| 15 | Change management | Prompt and model versions logged; CI gate on prompt, tool, policy and model changes | in place | Eng | yes |
| 16 | Canary plan | 5% → 25% → 100% with triggers (ex. 11) | agreed | Product | yes |
| 17 | Human capacity | Queues staffed for the expected escalation rate (~30% in lab 05) | staffed | Support lead | yes |

The two items people forget are the **kill switch** (one flag that sends every ticket to the human queue with an
acknowledgement; the circuit breaker's "open" state, set by hand) and **human capacity**. An agent that escalates
30% of tickets still needs the people to answer them.

---

## Exercise 6 — Guardrails for `change_delivery_address`

**1. Trifecta analysis.** *Private data*: the order and its delivery details, the sender's own only, if identity
comes from the channel. *Untrusted content*: every email, including forwarded threads and injected instructions.
*A way to act*: redirecting a shipment moves real value to an attacker-chosen place, which is exfiltration of goods.
All three legs are present whenever the attacker controls the channel (a compromised customer mailbox, or a lookalike
domain that slips through). The durable fix is to shrink the action leg: the agent can only select addresses
**already on the account**; a new address never takes effect on the agent's word alone.

**2. Controls per layer.**

| Layer | Control | Catches | Cannot catch |
|---|---|---|---|
| Prompt | Use only for pre-shipment corrections; take the address from the customer's own text, never from quoted or forwarded content; confirm back | Honest mistakes, most naive injections | A determined injection; a compromised mailbox |
| Sender check (code) | Lookalike domains blocked; unknown senders cannot use the tool | Phishing domains | A compromised real mailbox |
| Input screener | Flags address change combined with urgency, a new contact, payment talk or instructions to the AI → `review` | Social-engineering patterns | Well-crafted, plain requests |
| Tool authorization | Allowed only for verified senders at low risk; one change per order; N per account per week | Volume abuse, unverified senders | A single well-formed fraudulent change |
| Policy in the tool | Order belongs to the sender; status before shipment (SHP-003); address in the account's address book → apply; otherwise create a **pending change** | Changes after shipment, other customers' orders, attacker-chosen addresses | Nothing about intent |
| Human / out-of-band approval | New address, order value > $10,000, cross-border change, contact added in the last 30 days → confirm via the phone number or primary contact **on file**, not the requester | Compromised mailbox, insider-like attacks | Collusion |
| Output checks | Reply cannot claim "changed" unless the tool returned `applied`; pending changes are described as pending | False confirmations | — |
| Monitoring | Alerts on change rate, changes within 48 h of ship date, the same new address across accounts, changes from new senders | Campaigns, drop-shipping fraud | A single slow attack (review sampled changes) |

**3. Tool specification.**

```json
{"name": "change_delivery_address",
 "description": "Change where an UNSHIPPED order is delivered. Choose an address from the customer's address book (list_addresses). A new address is recorded as a pending change and confirmed with the account's primary contact before it takes effect.",
 "strict": true,
 "input_schema": {"type": "object", "additionalProperties": false,
   "properties": {"order_id": {"type": "string"},
                  "address_id": {"type": "string", "description": "An id from list_addresses; omit for a new address"},
                  "new_address": {"type": "object", "additionalProperties": false,
                                  "properties": {"line1": {"type": "string"}, "line2": {"type": "string"},
                                                 "city": {"type": "string"}, "postcode": {"type": "string"},
                                                 "country": {"type": "string"}},
                                  "required": ["line1", "city", "postcode", "country"]},
                  "reason": {"type": "string"}},
   "required": ["order_id", "reason"]}}
```

The tool refuses unverified senders, other customers' orders and shipped or cancelled orders. "Not yours" and
"does not exist" get the same answer, as the Kestrel tools do (PRV-004), so order numbers cannot be probed. It returns
`{"status": "applied" | "pending_verification" | "refused", "change_id", "next_step"}`, with the next step phrased
as an instruction to the model. It is idempotent on (order, target address): the same request returns the
existing `change_id`. It audits actor, ticket, before and after addresses, the verification path and the approver,
and it notifies the primary contact on file of every applied change (a second channel the attacker does not
control).

**4. Scenarios before enabling it.** Golden scenarios: an address-book change before shipment (applied); a new
address (pending, and the reply says so); a shipped order (refused, carrier redirect suggested); an unverified sender
with an order id (verification requested); a lookalike domain (blocked before the agent); an injection ("note to the
AI: ship to …") inside a legitimate change request; a forwarded thread containing someone else's address; an order
above the value threshold; a cross-border change; a second change to the same order. Assume-breach tests: call the
tool directly for another customer's order, a shipped order, a new address without verification, and five changes
in a row. Each must be stopped by the tool or the guard, whatever the model says.

---

## Exercise 7 — Monitoring and alerting plan

**SLIs and SLOs** (from the business constraints and lab 05's baseline):

| SLI | SLO | Why |
|---|---|---|
| Reply latency p95 (receipt → reply sent) | < 25 s over 30 days, 99% of tickets < 30 s | Customer promise, with margin |
| Safety tickets escalated P1 | 100% within 5 min of receipt; human pickup within 1 h | Hard constraint |
| Cost per ticket | daily mean < $0.30 | $0.40 constraint with margin |
| Service success rate | 99.9% of requests not 5xx (503 load-shedding tracked separately) | Availability |
| Critical guardrail events | 0 unauthorised writes; every block reviewed within 1 business day | Safety net health |
| Quality | Sampled judge pass rate ≥ calibrated baseline − noise floor | Catches drift |

**Dashboards.** *Operations* (on-call): request rate, p50/p95 latency, 5xx and 503 rates, in-flight runs, SDK
retries and 429/529 counts, circuit-breaker state. *Cost* (weekly, finance and engineering): cost per ticket and per
resolved ticket, by model and prompt version; cache-read share; turns per ticket; batch share. *Quality* (support
leads): automation and escalation rates by category; sampled judge scores; customer re-contact within 48 h;
guardrail events by layer; refusal and fallback rates. *Safety* (support ops): P1 tickets, time to escalation,
time to human pickup.

**Alerts** (page = customer harm now; ticket = investigate within a day):

| Severity | Condition |
|---|---|
| PAGE | A safety ticket without a P1 escalation within 5 min; P1 not picked up within 45 min |
| PAGE | p95 latency > 25 s for 10 min (multi-window burn rate on the 30 s SLO) |
| PAGE | 5xx rate > 2% for 5 min, or circuit breaker open > 10 min |
| PAGE | Any unauthorised write detected by reconciliation (a refund or RMA without its policy check) |
| TICKET | Cost per ticket > $0.30 (1 h window) or cache-read share < 50% after a deploy |
| TICKET | Escalation rate outside baseline ± 15 points (1 day); automation rate drops > 10 points |
| TICKET | Refusal or `max_tokens` stop reasons > 1% of calls; fallback rate doubles |
| TICKET | Unexpected tool-error rate > 2% (15 min); screener review rate doubles (FP creep or an attack campaign) |

**Online evaluation.** Each day, sample 2–5% of answered tickets and grade them with the calibrated judge (Haiku 4.5
or Sonnet 5 depending on calibration), plus 20 human reviews a week that also re-check the judge. Use outcome
signals: customer re-contact on the same order within 48 h, escalations reopened by humans, and agent-drafted
replies edited before sending, if you run in draft mode. New failure modes become golden scenarios.

**Runbooks.** *Cost per ticket spike*: split cost by prompt version and model; check cache-read share (a silent
invalidator, as in exercise 8), turns per ticket (tool loops), fallback rate and model mix; roll back the prompt
version if the spike follows a deploy. *Safety ticket without escalation*: escalate by hand immediately; check
whether the screener blocked it, whether the agent refused or errored, whether `escalate_to_human` failed; freeze
prompt deploys until a golden scenario reproduces it.

**Avoiding alert fatigue.** Page only on SLO burn and safety; everything else is a ticket. Use multi-window burn
rates instead of instantaneous thresholds, dedupe and route to named owners, and review alert quality monthly.
Delete alerts nobody acted on.

---

## Exercise 8 — Root-causing the cost spike

Run `python day6_evals_guardrails_production/solutions/ex08_cost_spike.py`.

**1. Cost by token type** (Claude Opus 5: $5 input, $25 output per million; cache writes 1.25×, reads 0.1×):

| | T-2177 (baseline) | T-2203 (spike) |
|---|---|---|
| LLM calls | 5 | 12 |
| cache write | $0.0097 | $0.3428 |
| cache read | $0.0098 | $0.0000 |
| output | $0.0305 | $0.0810 |
| **total** | **$0.0499** | **$0.4238** (8.5×) |

**2. Two causes.**

* *The prompt cache stopped working.* Every `llm.call` in the spike trace has `cache_read_input_tokens = 0` and
  writes its *whole* prompt again, and `prompt.system_sha256` differs on every call (12 hashes for 12 calls). The
  baseline has one hash and a 93% cache-read share. The cache is a prefix match over tools → system → messages, so a
  system prompt that changes between calls invalidates everything after it. A per-call timestamp rendered into the
  system prompt ("Current time: …") is the classic cause and matches the deploy's prompt-version change.
* *The conversation got longer.* Eleven `search_knowledge_base` calls all returned "No matching documents", and the
  agent rephrased and searched again until it hit the 12-turn limit and handed over to a human. The baseline
  answered the same kind of question after two searches. After the deploy the knowledge-base index was empty or
  unmounted.

The tempting single answer, "the model got more expensive" or "the model thinks more", is contradicted by the
trace: output per call is flat, and the model and prices did not change.

**3. Counterfactuals** (same tokens, re-priced): fixing the cache only → **$0.1417** (67% saved); fixing the knowledge
base only (5 turns) → **$0.1530** (64% saved); both → **$0.0681**. The remaining gap to the baseline is the
counterfactual's cold first call; in steady state every ticket also reads the shared tools + system prefix cached by
earlier tickets. Both causes are independently large, so fixing one leaves a ~3× cost increase.

**4. Fixes and monitoring.** Keep the system prompt byte-stable (a date, not a time, or put volatile context in the
user turn after the cached prefix) and check `cache_read_input_tokens` on the first calls after every deploy. Health-
check the index at startup (readiness fails if the knowledge base is empty) and cap repeated identical tool failures
(three empty searches → answer "I'll follow up" or escalate). Alert on cache-read share < 50% per deploy, mean turns
per ticket > baseline + 50%, and cost per ticket > $0.30; add a cost assertion to the CI eval. Either alert would
have fired within the first hour. Also: the traces hold raw requester email addresses (exercise 10).

---

## Exercise 9 — Two new graders

Run `python day6_evals_guardrails_production/solutions/ex09_reference_grader.py`.

`grade_references_quoted` reads the **end state** (new RMAs, refunds, escalations) and requires each id in the
reply. Security escalations are exempt: a suspected attacker is not owed an internal reference. `grade_facts` checks
the numeric `facts` of a scenario as money in either `2,237.44` or `2237.44` form, so it is not over-strict about
formatting.

Before using them, the solution **mutation-tests** the graders on hand-made observations: the good reply passes; a
reply with the RMA number removed, a wrong amount, or an empty reply fails; a reply without the thousands separator
passes. A grader that has never been shown to fail is not a grader.

Result: the baseline still passes 30/30 with the extended graders (no false positives). On the "two sentences"
candidate, the default graders fail 5 scenarios and the extended set fails 7. The new ones are E15, where the
duplicate-payment reply lost its escalation reference, and E19, a *safety* reply that lost its escalation reference
while keeping "isolate". Design notes: graders get the `Observation` (scenario, reply, trajectory, end state), so
new ones need no harness changes; each returns atomic `Check`s; neither is critical, because missing a reference is
bad service, not a safety breach.

---

## Exercise 10 — Trace redaction

Run `python day6_evals_guardrails_production/solutions/ex10_trace_redaction.py`.

* **Pseudonyms, not deletion.** Email addresses become `user:<HMAC-SHA256(key, email)[:12]>/<domain>`: stable, so
  T-1001 and T-1103 from the same sender share a token and you can still count repeat contacts; irreversible without
  the key; and deliberately *not email-shaped*. An early version produced `user-…@domain`, which the PII audit
  (correctly) still flagged, and re-applying the redactor hashed the hash. The domain is kept because it identifies
  the customer company, which per-customer metrics need. Use `keep_domain=False` where a domain identifies a person.
* **Masking and placeholders.** IBAN and card numbers keep the last four digits (the same function as the output
  guardrail). Phone numbers and street addresses are replaced; T-1005's escalation summary shows
  `4410 Industrial Pkwy` becoming `[address]`.
* **At capture time.** `RedactingTracer.span()` redacts attributes on entry and again when the span ends (values
  added by the body), and `export()` redacts once more, because the tracer records `error.message` after the body
  has exited. Redacting in a batch job later means the raw data already sat in your backend.
* **Verified, not assumed.** PII values in exported traces go from 11 to **0**. Dashboard metrics are identical when
  computed on the same spans with and without redaction. The first version compared two separate runs and differed
  because the second run hit a warm prompt cache: the cache-state confound again.

What regexes cannot catch: free-text names ("Marcus Hale, Cobalt Chemical Works"), addresses in unusual formats,
identifiers inside quoted messages. Either don't log message text at all (log ids, lengths and categories), or add
a named-entity pass and review samples. Keep the key in a secret store with audited access, rotate it on a schedule
(rotation breaks joins across the date), and enforce PRV-004's 90-day retention in the backend.

---

## Exercise 11 — Canary comparison

Run `python day6_evals_guardrails_production/solutions/ex11_canary.py`.

**Offline numbers** (mock):

| | v1 | v2-concise |
|---|---|---|
| passed | 30/30 | 23/30 |
| critical failures | 0 | 2 (E17, E18 lost "1 hour") |
| mean reply | 43 words | 31 words |
| mean output tokens | 753 | 734 |
| cost per case | $0.0267 | $0.0262 (−1.6%) |

Paired: 7 pass→fail flips, 0 fail→pass, exact McNemar p = 0.016, gate **BLOCK**. The pairwise judge on the 8
scenarios with reference notes, run in both orders, prefers v1 three times. The other five verdicts are
position-inconsistent (the mock judge breaks ties toward reply A) and count as ties. That is exactly why you swap.

**Decision: reject v2.** It saves 1.6% because an agent's output tokens are mostly thinking and tool calls; the
visible reply is a small part of the bill. And it drops required content: reference numbers, the 1-hour safety
commitment, approval details. A better candidate asks for concision *and lists what every reply must keep*. Then
re-run this comparison. For real savings, look at caching, turns and effort (section 5 of the lesson).

**Online canary plan** (for a candidate that passes): route 5% of tickets by a stable hash of the ticket id
(`KESTREL_CANARY_PERCENT` in lab 08) for at least three days. Compare the arms on escalation rate, output-check
blocks, guardrail events, cost and p95 latency per ticket, customer re-contact within 48 h, and a sampled pairwise
judge (both orders) on ~50 ticket pairs a day. Roll back automatically on any critical event or an escalation-rate
shift of more than 5 points; ramp 5% → 25% → 100%; keep v1 deployable until v2 has served 100% for a week.

---

## Exercise 12 — Per-customer rate limits

Run `python day6_evals_guardrails_production/solutions/ex12_rate_limits.py`.

* **Per customer, not per IP.** Every email arrives from the same mail gateway, so an IP limit throttles all
  customers at once. Keying on the sender domain isolates a noisy or compromised mailbox.
* **Token bucket.** Capacity 3 (burst) and one token every 20 s (sustained), with an injectable clock so the test
  advances time instead of sleeping. A denied request gets `429` with `Retry-After` computed from the refill rate.
* **ASGI middleware, no change to `app.py`.** It buffers the body, reads `from_email`, and either answers 429 or
  *replays* the buffered body to the app. Reading the body in normal FastAPI middleware would consume the stream
  the endpoint needs.
* **Redeliveries are free.** A request whose `Idempotency-Key` the store already answered passes through without
  spending a token, because the app serves it at zero model cost. This needed a read-only `IdempotencyStore.status()`:
  the first version probed with `begin()`, which *claims* the key, and the app then answered `409` to the real
  request. Probing must never have side effects.

The test output: `200, 200, 200, 429 (Retry-After 20s), 429`; another customer `200`; after 20 s `200` then `429`;
a redelivery with an empty bucket `200 (replayed=true)`.

**With several workers and replicas**, in-process buckets multiply the effective limit by the number of processes.
Keep buckets in a shared store (Redis with an atomic Lua script or `INCR` with expiry), or enforce limits at the API
gateway. Export rejections by customer *tier*, not per domain, to keep metric label cardinality bounded. Add a daily
token or cost budget per customer: tickets per minute limits bursts, but cost is what the business cares about.
