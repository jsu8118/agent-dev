# Day 6 exercises — Evaluation, guardrails, observability & production

Twelve exercises: concept checks (1, 2, 4), statistics (3), design scenarios (5, 6, 7), failure analysis (8) and
hands-on coding (9–12). Worked answers are in [`../solutions/README.md`](../solutions/README.md); the numerical and
coding exercises have runnable solutions in `../solutions/`. Try each one before reading the answer. The reasoning
is the point, not the final number.

Everything runs offline in mock mode. Where an exercise asks you to measure something, also say what would differ
in live mode.

---

## Exercise 1 — Reliable judges without `temperature=0` (concept, 15 min)

A teammate ports an LLM-judge from an old project and sends
`client.messages.create(model="claude-opus-5", temperature=0, ...)`. It fails.

1. What error do they get, and why? What happens with `claude-sonnet-5` and with `claude-haiku-4-5`?
2. They switch the judge to Haiku 4.5 "because it still supports temperature 0, so it's deterministic". What is
   wrong with that reasoning, on two levels (determinism and judge quality)?
3. List at least five design choices that make a judge's verdicts reliable without `temperature=0`, and say how
   you would *measure* its reliability.
4. Lab 03's judge is Claude Sonnet 5 while the agent runs on Claude Opus 5. Why, and when would you use Opus 5 or
   Haiku 4.5 as the judge instead?

## Exercise 2 — pass@k vs pass^k (concept, 15 min)

1. An agent succeeds on a ticket type with probability 0.9 per attempt, independently. Compute pass@3 and pass^3.
2. Which metric should Kestrel report for its customer-facing support agent, and which would fit a coding agent
   that generates three patches and keeps the one whose tests pass? Why?
3. You ran one scenario 10 times and it passed 8 times. Estimate pass^3 for it. Why does the unbiased estimate
   differ from 0.8³?
4. The golden set mixes easy and hard scenarios. Why should pass^k be computed per scenario and then averaged,
   rather than from the overall pass rate?

*Runnable check:* `solutions/ex02_pass_at_k.py`.

## Exercise 3 — How many scenarios to detect a 5-point regression? (statistics, 30 min)

The support agent passes 90% of a golden set. You want to detect a regression to 85% with 80% power at a two-sided
α = 0.05 before a release.

1. What is the 95% confidence interval of a 90% pass rate measured on today's 30 scenarios? What does that mean
   for the aggregate pass rate as a release gate?
2. How many scenarios per arm would you need if the two versions were evaluated on *independent* samples?
3. Now use the same scenarios for both versions (a paired design with McNemar's test). How many do you need if 5%
   of scenarios flip from pass to fail and none flip back? If 6% flip to fail and 1% flip to pass? Why does the
   paired design need so many fewer?
4. What do repeated runs (`--reps`) buy you, and what do they not?
5. Propose a release-gate policy for Kestrel that is honest about these numbers.

*Runnable check:* `solutions/ex03_sample_size.py` (formulas plus a Monte-Carlo simulation).

## Exercise 4 — Regression-testing a model migration (concept/design, 25 min)

Kestrel wants to move the support agent from `claude-opus-5` to `claude-opus-5-5` (cheaper per token, stronger
on agentic work). Read `kestrel/support_agent.py`.

1. Which Claude Opus 5.5 changes would make *some* request fail with a 400? Does the reference agent hit any of
   them? Which other course code might?
2. Which changes alter behaviour or cost silently, with no error? For each, name the eval signal that would
   reveal it.
3. Write the CI plan for the migration: which suites, how many reps, which gate rules, and what happens after the
   offline gate passes.
4. The agent uses server-side fallbacks. What changes about a *fallback* turn after the migration?

## Exercise 5 — The go-live checklist (design, 30 min)

Write the checklist Kestrel's review board signs before the support agent answers its first real customer. For
each item give the **evidence** that satisfies it (a lab output, a test, a dashboard), the **threshold**, and the
**owner**. Cover at least: correctness, policy and safety, security, privacy, cost, latency, reliability,
observability, operations (on-call, rollback) and change management. Mark which items are blocking.

## Exercise 6 — Guardrails for a new `change_delivery_address` tool (design, 30 min)

Today, delivery-address changes are escalated to the order desk (scenario E28). Product wants the agent to change
the address itself before shipment. Redirecting goods is a classic fraud vector: an attacker who controls a
mailbox, or a lookalike domain, redirects a $45,000 pump shipment.

1. Walk through the lethal-trifecta analysis for the agent with this tool.
2. Place controls in each layer (prompt, input screening, tool design and authorization, policy in code, output
   checks, human approval, monitoring). For each, say what it catches and what it cannot.
3. Specify the tool: its name, parameters, what it refuses, what it returns, and how it is audited and idempotent.
4. Which golden scenarios and assume-breach tests would you add before enabling it?

## Exercise 7 — Monitoring and alerting plan (design, 30 min)

Design the production monitoring for the support agent: the service-level indicators (SLIs) and objectives (SLOs)
derived from the business constraints, the dashboards and who looks at them, the alerts (page vs ticket, with
thresholds and windows), the online evaluation (sampled judge, feedback signals), and the runbook entry for the
two most likely alerts. Use lab 05's metrics as your baseline and explain how you would avoid alert fatigue.

## Exercise 8 — Root-cause a cost spike from traces (failure analysis, 30 min)

On 2026-09-16 the finance dashboard shows the average cost per ticket jumping from ~$0.05 to ~$0.40 after deploy
`2026.09.16-2`. On-call exported two traces to
[`data/cost_spike_traces.jsonl`](data/cost_spike_traces.jsonl): T-2177 from the day before and T-2203 from the
day after. Each line is one span in labkit's trace format (`name`, `trace_id`, `span_id`, `parent_id`, `start`,
`end`, `status`, `attributes`, `events`).

1. Compute each trace's cost by token type (input, cache write, cache read, output) at Claude Opus 5 prices.
2. Find the root cause or causes. There is more than one contributing factor. Show the evidence in the trace for
   each.
3. Quantify each cause: what would the spike ticket have cost with only one of them fixed?
4. Propose the fixes and the monitoring that would have caught it within an hour.

*Hints:* look at `gen_ai.usage.*`, `prompt.system_sha256` and the tool spans' events. `labkit.pricing.cost_breakdown`
prices a usage dict.

## Exercise 9 — Add a new grader (coding, 40 min)

Extend lab 02's harness (`labs/_evalkit.py` accepts `graders=[...]`) with:

1. `grade_references_quoted`: every reference the run *created* in the system of record (RMA, escalation, refund
   id) appears in the reply. Hint: `Observation.state` has the end-state rows.
2. `grade_facts`: for scenarios that list numeric `facts` (E04), each value appears in the reply as money.
3. Test your graders before trusting them: build hand-made observations whose correct verdict you know (a good
   reply, one with the RMA number removed, one with a wrong amount, an empty reply) and assert the outcomes.
4. Run the baseline with the extended graders, then a candidate prompt that asks for "at most two sentences"
   (`SYSTEM_PROMPT + "\nPrompt version: support-v2-concise. Keep every reply to at most two sentences."`). Which
   failures do your graders catch that the default ones miss?

## Exercise 10 — A PII redactor for traces (coding, 40 min)

Lab 05's check "no PII in exported traces" fails. Implement redaction **at capture time**:

1. A `Redactor` that replaces email addresses with keyed pseudonyms (same sender, same token; not reversible
   without the key), masks IBAN and card numbers to the last four digits, and replaces phone numbers and street
   addresses.
2. A `RedactingTracer(Tracer)` that applies it to every span's attributes and events, including values recorded
   after the span body (errors), and before export.
3. Re-run lab 05's tickets and show: zero PII in exported traces, the same pseudonym for two tickets from the same
   sender, and unchanged dashboard metrics.
4. What can regexes not catch, and what would you do about it?

## Exercise 11 — Canary comparison of two prompt versions (coding, 45 min)

Compare `support-v1` (the reference prompt) with `support-v2-concise` (exercise 9's candidate) the way you would
before a canary:

1. Run both on the golden set with the extended graders; report pass rates, critical failures, flips, McNemar,
   reply length, output tokens and cost per case as absolute numbers.
2. Add a pairwise judge on the scenarios that have reference notes (`labs/_judge_notes.py`). Randomise the order
   and judge both orders; count only verdicts that survive the swap.
3. Decide: reject, fix, or promote to a 5% canary. Justify with the numbers.
4. Write the online canary plan: routing, duration, metrics per arm, rollback triggers, ramp-up.

## Exercise 12 — Per-customer rate limits for the service (coding, 40 min)

Add per-customer rate limiting to the lab 08 service without editing `app.py`:

1. A token-bucket limiter keyed by the sender's email *domain* (the customer), with a burst capacity and a
   sustained refill rate, and an injectable clock.
2. ASGI middleware that reads `from_email` from the POST body, answers `429` with `Retry-After` when the bucket is
   empty, and otherwise replays the body to the app.
3. Do not charge redeliveries that the service would answer from its idempotency store.
4. Test it with `TestClient`: a burst from one customer, another customer unaffected, refill after time passes, and
   a redelivery with an empty bucket.
5. Why per customer and not per IP here? What changes with several workers and replicas?
