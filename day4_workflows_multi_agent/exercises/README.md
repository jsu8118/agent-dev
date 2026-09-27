# Day 4 exercises - workflow patterns and multi-agent systems

Twelve exercises: three concept checks (1-3), three design scenarios (4-6), two failure analyses (7-8)
and four hands-on coding tasks (9-12). Worked answers are in [`../solutions/README.md`](../solutions/README.md);
the coding tasks have runnable solutions in `../solutions/ex09_*.py` ... `ex12_*.py`.

Prices you may need (list prices, USD per million tokens, input / output): Claude Opus 5 $5 / $25,
Claude Sonnet 5 $2 / $10, Claude Haiku 4.5 $1 / $5. Cache reads cost ~0.1x the input price, 5-minute
cache writes 1.25x. The Message Batches API bills every token at 50%.

---

## Concept checks

### Exercise 1 - Name the pattern

For each Kestrel request, name the pattern you would start with (single call, prompt chaining, routing,
parallelization-sectioning, parallelization-voting, orchestrator-workers, evaluator-optimizer, autonomous
agent) and give the one fact about the task that decides it.

1. Support wants German and Spanish tickets translated to English before the existing English triage prompt runs.
2. RMA photos arrive with every return; transit damage, installation damage and manufacturing defects each go to a different team with a different checklist.
3. Security wants three independent checks on every inbound supplier email ("is this phishing?") and escalates if any one fires.
4. The monthly reliability report covers a variable number of sites; each site needs its own analysis, then an executive summary ties them together.
5. Marketing asks for knowledge-base articles that must follow a 12-point style guide; the first draft rarely does.
6. An on-call engineer asks: "customers in the EU say the order portal is slow since this morning - find out why", with access to logs, deploys, runbooks and tickets.
7. Extract the serial number, fault code and install date from each warranty email into the ERP.

### Exercise 2 - Code, model, or both?

For each item decide whether it belongs in deterministic code, in an LLM step, or in both - and why.
Tempting wrong answers are part of the exercise.

1. FIN-AP-010 check 6: unit price must be at most the PO price + 1%.
2. FIN-AP-010 check 1: "the same supplier invoice number has not been processed before".
3. Reading the invoice number off a scanned invoice whose layout the pipeline has never seen.
4. FIN-AP-010 §3: detecting text that "addresses automated systems or AI assistants".
5. Deciding that a support ticket is a safety incident.
6. Deciding whether a supplier's dispute reply ("the price rise was agreed with your buyer by phone") is credible.
7. Converting an EUR invoice total to USD for the $25,000 approval limit.

### Exercise 3 - Multi-agent arithmetic

Lab 06 printed (mock mode):

```
  architecture       issues right  faults found  false alarms  calls  input tok  output tok  cost     latency*  wall-clock  largest prompt
  A lead + analysts  12/12         5/5           0             11     32,693     4,779       $0.2374  69s       0.10s       5,813
  B single agent     12/12         5/5           0             3      22,513     2,432       $0.1529  51s       0.04s       12,431
```

(`latency*` is the modelled critical path; wall-clock is measured and near zero in mock mode.)

1. Compute the token multiplication factor and the cost ratio. Name three concrete places where the
   multi-agent run spends tokens that the single agent does not.
2. The single agent issued its 12 summary calls in ONE turn. Explain why that makes the single agent's
   latency competitive, and write the critical-path formula for each architecture.
3. The per-pump context cost is roughly 600 tokens for the summary plus ~1,300 for a follow-up view.
   Estimate the fleet size at which the single agent's final turn would pass 200K tokens. What else,
   besides the context-window limit, argues for splitting the work well before that point?

---

## Design scenarios

### Exercise 4 - Accounts payable at scale

Kestrel's controller wants the Lab 01 pipeline in production: ~1,200 invoices/month (peaks of 150 on the
first working day), decisions available by 07:00 the next morning, LLM spend under $0.02 per invoice, every
decision auditable, and no LLM allowed to approve a payment or change bank details. Choose the architecture,
the model and effort per step, whether to use the Batch API, and what the harness must log. Estimate the
monthly LLM cost from Lab 01's per-invoice tokens (about 525 input and 165 output tokens per extraction in
mock mode; assume live output is 3x higher because of thinking). Say what you would do about the ~10% of
invoices in a layout nobody has seen.

### Exercise 5 - Routing under an SLA

Support's constraints (company profile): cost below $0.40 per resolved ticket on average, customer-facing
reply in under 30 seconds, safety tickets to a human within 1 hour, ~1,900 tickets/month. Lab 02's router
misrouted T-1405 ("After we replaced the bearings on our KP-250 the bearing housing runs at 90 °C...") as a
warranty claim with confidence 0.90, and put T-1505 (a strategic customer's invoice complaint) at P3
instead of P2. Design the production router: classifier, cascade rule, deterministic overrides, handlers.
Say which misroutes are unacceptable, which are merely expensive, and how each is prevented or caught.
Estimate the LLM cost per ticket.

### Exercise 6 - Fifty plants

Kestrel acquires a competitor; the quality team now receives ~300 incident reports a week from 50 plants,
and wants Lab 04's investigation every Monday. Would you keep the orchestrator-workers workflow, move to an
autonomous research agent, or run it as a Claude Managed Agents multiagent session? Decide the decomposition
(per plant? per supplier? per part family?), the spawn cap, the models per role, and how the synthesis stays
within one context. Estimate the weekly cost with stated assumptions, and name the signal that would make you
change the design.

---

## Failure analysis

### Exercise 7 - The duplicated-work trace

A colleague ran an early version of Lab 06's harness - no spawn cap, no assignment ledger, analysts' tool
scoped to all 12 pumps - and got a slow, expensive run with contradictory findings. Prices: Opus 5 for every
agent, list prices, no caching.

```text
trace 7f3a  fleet-triage  (lead + analysts all claude-opus-5)
- lead turn 1                                   in=1,812   out=946
    delegate_to_analyst([GB-KP400-01, GB-KP400-02, GB-KP250-03, GB-KP250-04], "Triage the GBWD North pumps ...")
    delegate_to_analyst([HF-KP250-01, HF-KP250-02, HF-KP250-03, HF-KP100-04], "Triage the Harbor Foods pumps ...")
    delegate_to_analyst([CC-KP600-01, CC-KP250X-02, CC-KP250X-03, CC-KP100-04], "Triage the Cobalt pumps ...")
    delegate_to_analyst([GB-KP400-01, GB-KP400-02, HF-KP250-03, CC-KP600-01, CC-KP250X-02, CC-KP250X-03],
                        "Deep-dive all criticality-A pumps: they matter most.")
  - analyst-1  3 turns  8 tool calls            in=10,340  out=927    GB-KP400-02 cavitation (nightly 01-05 UTC)
  - analyst-2  3 turns  5 tool calls            in=9,211   out=697    HF-KP250-03 bearing_wear, alarm crossed 09-12
  - analyst-3  3 turns  6 tool calls            in=8,143   out=842
  - analyst-4  4 turns  17 tool calls           in=24,905  out=1,960  HF-KP250-03 "sensor drift - recalibrate";
                                                                      GB-KP400-02 "normal (2.69 < 3.5 mm/s alert)"
- lead turn 2                                   in=9,406   out=2,311  "Analysts disagree on HF-KP250-03 and GB-KP400-02."
    delegate_to_analyst([HF-KP250-03, GB-KP400-02], "Resolve the disagreement between the analysts ...")
  - analyst-5  3 turns  7 tool calls            in=7,420   out=1,105  confirms bearing_wear and cavitation
- lead turn 3                                   in=11,977  out=1,630  final triage
totals: llm_calls=19  in=83,214  out=10,418  cost=$0.68
```

1. Which calls were wasted, and what share of the cost did they take?
2. Name every root cause - in the lead's plan, in the harness, and in analyst-4's own work.
3. Propose fixes at each layer and say which of them are guarantees and which are only nudges.
4. Why did analyst-4 get GB-KP400-02 wrong while analyst-1 got it right, although both had the same tool?

### Exercise 8 - Error propagation through hand-offs

In a run of Lab 04, the P2 worker's structured finding for INC-P2-0419 came back with `"lot": "PS-2806-B"`
(two digits transposed). The synthesizer queried `build_records` and `rmas` for `PS-2806-B`, got zero rows
for both, and the final report listed "PS-2806-B (MS-250 seals): no units in the field, no returns - low risk".
Nine pumps built with PS-2608-B are at six customers, and two have already come back.

1. Where did the error enter, and why did no later stage catch it?
2. Propose one mitigation at each hand-off (worker output, tool, synthesizer, report) and say which is the
   cheapest reliable one.
3. What general rule about "absence of evidence" should the synthesizer's prompt state, and why is a prompt
   rule alone not enough?

---

## Hands-on

Each task extends a lab. Keep lab behaviour unchanged unless the task says otherwise, and run your
solution in mock mode first (`python your_script.py` with no API key).

### Exercise 9 - Add a FIN-AP-010 rule

Policy revision 2026-10 adds rule 11: *the remit-to account printed on an invoice must match the supplier
master's `bank_account_last4`; otherwise decision `security_hold`, exception `remit_account_mismatch`.*
Implement it on top of `labs/_ap.py` without editing the base engine. Prove that none of the 20 existing
decisions change, and that a synthetic copy of INV-05 remitting to "account ending 9912" is held. Decide what
the rule does when no account is printed (the second invoice layout prints none).

### Exercise 10 - A resilient worker

Lab 04's workers run in parallel; make each one survive transient failures. Retry retryable errors (429,
5xx, 529, timeouts) with exponential backoff and jitter, then fall back to a different model; never retry a
400 on the same input; if everything fails, return an explicit partial result and make the final report
say which plant is missing. Demonstrate it with deterministic fault injection that works live and offline.

### Exercise 11 - A budget for the orchestrator

Give Lab 04's pipeline a hard dollar budget. Estimate every call before making it (the token-counting
endpoint gives exact input tokens), reserve part of the budget for synthesis, prioritise the worker tasks,
downgrade to the fast model or skip tasks when needed, and stop the synthesizer's tool loop before a turn
that would overspend. Show three budgets: generous, tight, very tight. The run must never exceed its budget.

### Exercise 12 - A voting threshold

Lab 03's majority vote caught the bank-change fraud but missed the duplicate invoice. Implement k-of-n
thresholds, veto lenses (named lenses may escalate alone) and a weighted vote; collect the votes once and
evaluate every rule on the 20 invoices against the AP team's escalations (`reject` or `security_hold`).
Choose the rule by expected cost: a miss costs the invoice amount at risk, a false alarm costs $15 of clerk
time. What changes if a false alarm costs $5,000 (for example, because it freezes a strategic supplier)?
