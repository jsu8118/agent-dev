# Day 6 - Evaluation science and release engineering

The first course's Day 6 ended at a go-live review: an eval harness with code graders, a judge calibrated against
human labels, a canary with automatic rollback. A month later Kestrel's Service Desk Copilot has answered 480
production tickets across five deployment arms - two prompt versions, three models, three effort levels - and
the questions have changed. Nobody asks "does it work?" any more. They ask "did this change make it better or
worse, for which customers, and how sure are we?", and every one of those questions is statistical.

This day is about the statistics you need before you believe an eval, and the release process you need before you
ship a change. It is taught on real-shaped production traces with a regression planted in them: prompt v15, which
improved the average - fewer policy and citation findings, success and CSAT held - while quietly handing return
requests to "a colleague" nobody assigned. Kestrel found it three weeks after the change was merged, when a
customer complained. By the end of the day you will have found it from the traces alone, measured when it could
have been caught, and built the gate, the canary analysis and the runbook that catch the next one.

Everything builds on the first course's evals day ([`../../day6_evals_guardrails_production/README.md`](../../day6_evals_guardrails_production/README.md)):
the eval pyramid, golden sets and code graders, Wilson intervals, McNemar's test, Cohen's kappa and pass^k are
assumed, not repeated. Today goes past them - where evals come from at trace volume, how big they must be, how to
trust judges you cannot read one by one, how to change prompts and models without fooling yourself, and how to
release.

## Learning objectives

You can:

1. Mine a stratified, labelled, leak-free eval set from production traces, state the error of each label source,
   and version the set like a product.
2. Put an interval on any eval number, prefer paired designs where they help, compute power and the minimum
   detectable effect before you run anything, correct for multiple comparisons, and report pass^k per scenario.
3. Run pairwise judges at scale: agreement against a human ceiling, the swap test, Bradley-Terry rankings with
   intervals, and re-calibration when the judge changes.
4. Find a regression that an average hides - slices, Simpson's paradox, derived metrics from reply text x tool
   calls - and write a release gate with intervals, minimum slice sizes and zero-tolerance checks.
5. Hill-climb a prompt with train / validation / test discipline, recognise overfitting to the eval, and stop.
6. Choose a model x effort operating point with pre-registered gates, and treat cache health as a release metric.
7. Analyse a canary with a sequential test and guardrails, monitor drift against a measured noise floor, write a
   rollback runbook, and audit a model migration as code.

## Agenda (about 7 hours)

| Block | Minutes | What |
|---|---:|---|
| From traces to evals | 50 | Section 1, lab 01 |
| Uncertainty: intervals, pairing, power, pass^k | 60 | Section 2, lab 02 |
| Judges at scale | 50 | Section 3, lab 03 |
| Break | 15 | |
| Slicing and regression hunting, the case study | 60 | Section 4, lab 04 |
| Prompt hill-climbing with holdouts | 40 | Section 5, lab 05 |
| The model x effort staircase and caching health | 50 | Section 6, lab 06 |
| Release engineering: canaries, drift, rollback, migrations | 60 | Section 7, lab 07 |
| The release checklist, exercises | 35 | Section 8 |

Every lab runs offline in mock mode (`python advanced/day6_eval_science_release/labs/0N_*.py`). Labs 01, 02 and 04
make no API call at all: they are statistics over the trace files in `advanced/data/traces/`. Labs 03, 05, 06 and
07 call the Messages API - a pairwise judge, a triager, a model x effort sample, request probes - and run unchanged
against Claude with a key. The traces' hidden ground truth (`_truth`) is never read by a lab; two solutions read it
at the end to grade a method.

---

## 1. From traces to evals

**The idea.** A production trace is the best raw material an eval can have: a real input, what the system did
(tools, tokens, latency, the reply), what happened next (resolved, failed, escalated) and whatever people said
about it (a reviewer's findings, a CSAT score, a human rewrite). Mining evals from traces means choosing items,
labelling them from those signals, removing duplicates, splitting without leakage, and versioning the result. The
first course grew its golden set by hand ("every production failure becomes a golden scenario"); at trace volume
you need sampling and labelling methods, and you need to know how wrong your labels are.

**Where it comes from.** Survey sampling (stratification, inverse-probability weights), weak supervision in
machine learning (combining noisy labelling functions and recording their provenance), and dataset practice
(group splits, dataset versioning).

### Labels from signals the traces already carry

| Source | Coverage at Kestrel | What it measures | Typical error | Delay |
|---|---|---|---|---|
| QA review | 35% (sampled) | policy correctness, issue types | misses some issues, flags a few non-issues | days |
| CSAT | 46% (self-selected) | the customer's experience | noisy in both directions, response bias | hours to days |
| Human edit | 100% | "someone disagreed enough to rewrite it" | rewrites for style; untouched flawed replies pass | immediate |
| LLM judge | as many as you pay for | rubric criteria | bounded by what it sees, position and verbosity bias | minutes |
| Adjudicated label | as many as you pay for | the definition you write | cost | days |

Lab 01 combines the first three with a precedence policy (review, then CSAT, then edit) and records which source
produced each label. The sources agree 77-88% of the time, but kappa - agreement beyond chance - is only
0.45-0.67: they see the same replies differently. Agreement between sources does not tell you which one is right;
exercise 1's solution grades them against the planted truth, and the review (5.6% of flawed replies passed) beats
CSAT (14.0%) and the edit signal (24.0%). A weak label is good enough to find candidates; before a set gates a
release, adjudicate a sample by hand, and route every item whose sources conflict to a person.

### Duplicates, splits and leakage

The 480 traces answer only 62 distinct tickets (median 8 traces per ticket). Replies to one ticket share its facts
and phrasing, so a random split by trace leaks: in lab 01, 13 tickets land on both sides and 17 of 24 held-out
items have a sibling in training. Split by the unit that carries the information - the ticket here, the user or
the account elsewhere (the "group split" of recommender systems) - and, for anything that drifts, by time.
De-duplicate on a key that matches your question: (ticket, arm, reply text) keeps one item per distinct answer of
each arm; the text alone would merge a v14 reply with its identical v15 twin and hide exactly the cells where the
arms agree.

### Sampling: random, stratified, targeted

| Design | How | Good for | Weak at |
|---|---|---|---|
| Simple random | every trace equally likely | an unbiased overall rate | rare cells come out empty |
| Stratified, proportional with a floor | quota per cell, at least k per cell | comparisons per cell | overall rates need inverse weights |
| Census of rare cells | take every trace of a small arm or category | new arms, rare categories | nothing, if you can afford it |
| Targeted (failure-seeking) | oversample flagged traces | finding failure modes | any rate estimate |

Stratify on what you will compare. Lab 01's strata are (category, prompt version): random sampling leaves 4 of the
20 cells empty, the stratified draw none. The same draw gives `opus-v15-high` one item, because arms were not a
stratum - if the next question is "which effort level?", the arm is a stratum, or the rare arms are taken whole.

### The eval set is a product

An eval set has a version, an owner, a changelog and known gaps. Lab 01 writes the set and a manifest - a content
hash, the label policy, counts per split, label source, category and arm, and the known gaps. A gate that quotes a
pass rate must say which set version produced it; a new version must say what changed (new failures mined, labels
re-verified, items retired because the product changed).

---

## 2. Uncertainty: intervals, pairing, power, pass^k

**The idea.** Every eval number is an estimate with sampling error, and most decisions a team makes from evals are
comparisons of two estimates. The tools are old - binomial intervals, the bootstrap, paired tests, power analysis,
multiple-comparison corrections - and they answer the three questions that matter before and after a run: how big
is the error bar, how big must the eval be, and how many "significant" findings are noise.

### Intervals for any number

| Interval | For | Behaves at 0% / 100% | Use |
|---|---|---|---|
| Wald (p +/- 1.96 sqrt(p(1-p)/n)) | proportions | badly (zero width) | never for small n |
| Wilson | proportions | well | the default for a pass rate |
| Clopper-Pearson (exact) | proportions | conservative | regulatory reporting |
| Percentile bootstrap | any statistic (median, ratio, cost per success) | lumpy on tiny n | everything else |

Lab 02 puts Wilson and bootstrap intervals on the per-arm success rates: the canary's 81.8% comes from 11 traces,
52.3%-94.9% (Wilson). The
bootstrap resamples the traces 2,000 times and takes the 2.5th and 97.5th percentiles of whatever statistic you
compute - which is why it is the tool for cost per resolved ticket or a p95 latency.

### "Run it five times and eyeball it"

The common practice - run the eval a few times, compare the averages - has a measurable failure rate. Lab 02
simulates two identical 90% agents on 30 scenarios x 5 runs: an eyeball that calls a 3-point gap "a difference" is
fooled 39.4% of the time, and it misses a real 5-point regression in 611 of 2,000 comparisons, because the
difference of two such arms has a noise floor of +/-6.8 points. Reps average out sampling noise on the scenarios you
have; they add no coverage of scenarios you lack.

### Paired vs unpaired

When both versions answer the same items, compare them item by item: hard items are hard for both, so the
between-item variance cancels and only the items that change carry information (McNemar's test counts exactly
those). On lab 02's simulation with very uneven scenario difficulty the paired interval is 26% narrower than the
unpaired one. On the real arms pairing does not help - opus-v15 answered each shared ticket 1.9 times on average, so
a per-ticket difference is mostly one binary attempt's noise. And a paired test with violated assumptions lies:
collapsing tickets to majority verdicts gives McNemar p = 0.004 for a difference the paired bootstrap cannot see,
because a majority over six baseline traces is measured differently from a majority over one or two v15 traces.

| Design | Needs | Wins when | Loses when |
|---|---|---|---|
| Unpaired (two samples) | two independent samples | live A/B traffic, arms never see the same item | items differ a lot in difficulty |
| Paired (same items, both versions) | re-running the items | offline evals, migrations, prompt changes | one side has one noisy attempt per item |
| Paired with reps | k runs per item per version | stochastic agents | budget |

### Power and the minimum detectable effect

Decide the eval's size before running it. For a two-proportion test at alpha 0.05 and 80% power, lab 02 prints the
minimum detectable drop from Kestrel's 73.8% baseline: 35 points with 30 traces per arm, 17 with 120, 8 with 480;
detecting 5 points takes 1,284 traces per arm. A paired design costs what its churn costs: 155 scenarios for a pure
5% regression, 469 when a model migration flips 10% one way and 5% the other (the first course's Day 6 numbers - the
baseline rate does not enter). Non-inferiority ("at most 5 points worse") costs the same order as detecting a drop
of that size (exercise 2: 952 per arm).

### Multiple comparisons

Ten category slices tested at 0.05 with no real effect anywhere produce at least one "significant" slice 40% of
the time; ten slices x five metrics, 92%. Corrections trade power for honesty:

| Rule | Controls | Use for |
|---|---|---|
| Bonferroni (alpha / m) | the chance of any false flag (family-wise) | small families |
| Holm (step-down) | the same, never less power than Bonferroni | release gates |
| Benjamini-Hochberg | the share of flags that are false (FDR) | monitors that route slices to a person |
| Pre-registration | which slices are gates at all | everything |

The real defence is a mechanism: a flagged slice becomes a finding when you can point at a reply, a tool pattern or
a release note (section 4).

### pass@k and pass^k

For agentic tasks the unbiased estimators from c passes in n runs are pass@k = 1 - C(n-c, k)/C(n, k) and
pass^k = C(c, k)/C(n, k). Report pass^k per scenario for anything a customer sees on every attempt; the plug-in
(c/n)^k overstates it, and the pooled rate cubed hides which scenario fails. With 5 runs per scenario a pass^3
estimate can only be 0%, 10%, 40% or 100% - "run it five times" gives a number, not an estimate.

---

## 3. Judges at scale

**The idea.** At trace volume you cannot read every reply, so an LLM judge reads them for you - and then the judge
is the instrument whose error you must measure. The first course calibrated a pointwise judge on 24 replies. At
scale the questions are different: agreement against what ceiling, position bias across thousands of pairs,
turning pairwise verdicts into a ranking of arms, and noticing when the instrument itself changed.

### Pairwise vs absolute

| | Absolute (score 1-5) | Pairwise (A vs B, tie allowed) |
|---|---|---|
| Question | how good is this reply? | which of these two is better? |
| Stable over time | if the rubric has anchors | only relative to the pair |
| Main bias | leniency, scale drift | position, verbosity |
| Cost | one call per reply | two calls per pair (both orders) |
| Aggregates into | a pass rate | a ranking (Bradley-Terry) |
| Use for | gates, dashboards | comparing versions and arms |

### Agreement against a ceiling

A judge cannot agree with a human more than a second human does, except by luck. Kestrel's two annotators agree on
20 of 39 double-annotated pairs, kappa 0.23 - so lab 03's judge, at kappa 0.15 against H1, is read against 0.23,
not 1.0. The judge's own ceiling is lower still: it sees the reply text and three ticket facts, and a wrong policy
sentence or a missing citation looks like clean text, so most of its verdicts are ties where the reviewer - who had
the policy in front of them - picked a side. Agreement is bounded first by what the judge is given, then by the
human ceiling.

### Position bias and the swap test

Judge every pair twice, A first and B first, and keep a verdict only if it survives the swap. Whether the judge may
say "tie" matters as much as the swap: in lab 03 the tie-allowed judge survives the swap on 119 of 120 pairs, while
a forced-choice judge on the same rubric follows the position on 96. Forced choice turns "equal" into "first"; the
swap filter turns those verdicts back into ties, after which the forced-choice judge matches the tie-allowed one on
all 120 pairs. Report the position-bias rate as a property of the judge, measured on your calibration set.

### Bradley-Terry: from pairwise verdicts to a ranking of arms

Bradley-Terry models P(i beats j) = p_i / (p_i + p_j) and estimates one strength per arm from all comparisons at
once, so beating a strong arm counts more than beating a weak one; lab 03 fits it with Hunter's minorisation-
maximisation algorithm (ties count half a win each) and bootstraps the pairs for intervals.

| Method | What it does | Weakness |
|---|---|---|
| Raw win rate | wins / games | depends on who you happened to face |
| Elo | online updates, one game at a time | order-dependent, needs many games |
| Bradley-Terry | batch maximum likelihood over all games | one quality scale; sparse graphs give wide intervals |
| Per-slice Bradley-Terry | one fit per category | needs comparisons in every slice |

The human table ranks the fast-path canary first - strength 2.17, on 15 comparisons, all on order-status and
product-inquiry tickets, with an interval from 1.08 to 4.88. A ranking is only as good as its comparison graph, and
Bradley-Terry assumes one quality scale; an arm that only faces easy tickets sits on a different one (exercise 4
shows what 60 targeted comparisons buy).

### Judge drift and re-calibration

A rubric edit, a new judge model or a silent model update is a new instrument. In lab 03, rubric v2 ("empathy
counts, prefer concise") changes 46 of 120 verdicts on identical replies and reorders the arms - while its kappa
against the humans stays at 0.15. A flat calibration number does not prove a stable instrument; compare the two
versions with each other. The rules: pin (judge model, rubric id) on every verdict; keep a fixed human-labelled
calibration set and re-run it on every change; re-judge the overlap before splicing a trend line; change one thing
at a time.

---

## 4. Slicing and regression hunting

**The idea.** An aggregate metric is a weighted average of slices, and averages hide things in two ways. A
regression in one slice can be cancelled by improvements in others (v15: +20 points on billing, -31 on returns).
And when arms see different traffic, the aggregate can even point the wrong way - Simpson's paradox. Regression
hunting is slicing with discipline, plus metrics derived from what the traces show, plus a gate that knows what it
cannot conclude.

### Simpson's paradox and standardisation

Lab 04 finds a real reversal in the traces. The fast-path canary beats the opus-v15 arm by 11.5 points overall
(81.8% vs 70.3%) and ties or loses in both categories it serves: it only ever sees order-status and product-inquiry
tickets, which are 12% of opus-v15's traffic. Two remedies, both borrowed from epidemiology:

| Method | How | When |
|---|---|---|
| Compare per slice | one comparison per category | enough traces per slice |
| Direct standardisation | apply each arm's per-category rates to one common mix | both arms cover every category of the mix |
| Indirect standardisation (observed / expected) | successes / the successes the reference would have had on this arm's own traffic | an arm with a narrow or small mix (lab 06) |

### Derived metrics: claims vs actions

The generic metric (no failure, no human rewrite) flags return_request at p = 0.037, which no correction keeps.
Reading the replies finds the mechanism: v15 says "I have escalated your return request to a colleague", and the
trace shows no `escalate_to_human` call - but it does show `create_rma`. A reply is a set of claims; a trace is a
record of actions; a claim the trace contradicts is a metric you can compute on every reply. The derived hand-off
metric reads 5/18 vs 0/44, p = 0.0013, and survives every correction. The QA reviewers had written it down too: a
finding type that never appeared in 114 v14 reviews appears 4 times in 40 v15 reviews - with passing scores, so the
score dashboard stayed green. Count findings by type.

### Release gates that know what they cannot conclude

| Rule type | Example | Needs | Blocks |
|---|---|---|---|
| Critical check (zero tolerance) | a reply claims a hand-off no tool made | a zero baseline | on one occurrence |
| Relative check | refund promises no more often than the incumbent | an interval | when worse beyond the margin |
| Non-inferiority on a slice | v15 - v14 success above -10 points | a Newcombe interval, a minimum n | FAIL when the whole interval is below the margin |
| Minimum slice size | fewer than 10 traces: INSUFFICIENT | patience | never - holds the rollout |
| Monitor | every other slice x metric | BH, a person | never on its own |

Lab 04's gate compares the interval of the difference (not the candidate's own interval against a point
estimate), says INSUFFICIENT instead of pretending, and blocks v15 on the data available one week into the rollout.

---

## 5. Prompt hill-climbing with holdouts

**The idea.** Improving a prompt against an eval is an optimisation loop - read failures, change one thing, re-run,
keep or revert - and every optimisation loop overfits what it can see. The discipline comes from machine learning:
tune on train, choose on validation, report test once.

| Protocol | What you tune on | What you report | Failure mode |
|---|---|---|---|
| Tune and report on everything | all tickets | the tuned score | memorises tickets; the number means nothing |
| Train / validation / test | train failures | test, once, for the round validation chose | tiny validation splits can only veto |
| Cross-validation by ticket | folds | the fold average | a person cannot write a rule "per fold" |
| Fresh-data refresh | train + validation | next week's new tickets | needs a stream of new labelled tickets |

Rules that make the loop honest:

* **Split by ticket and seal the test split.** It is scored once, at the end.
* **Know the noise floor.** Kestrel's validation split has 12 tickets; 9/12 has a 95% interval of 46.8%-91.1%.
  Validation can veto a round that clearly hurts; it cannot rank two rounds that differ by one ticket, so each
  decision also names the mechanism that moved the tickets.
* **Write the keep/revert rule first.** Lab 05's: keep a round only if validation does not drop and train or
  validation improves.
* **One change per round; the headline is the test delta** against the starting point, not the train score you
  climbed.
* **What to change first:** the definitions the model works from, then rules that describe a behaviour seen across
  several failures, then examples - never the words, ids or codes of one ticket.
* **Stop** when two consecutive rounds fail to beat the incumbent by more than the noise floor, or when the
  remaining failures are not the prompt's to fix.

The overfit signature is train up, validation down, the train-validation gap widening. Lab 05 shows it with a round
that patches every remaining train failure with words copied from the failing ticket: train reaches 37/37 while
validation falls, and the test split later shows why - the patch for one ticket's order id (`SO-10257`) turned a
different customer's return request into a warranty claim.

---

## 6. The model x effort staircase and caching health

**The idea.** Model and effort are one cost-quality surface, not two knobs tuned in turn. The cheapest acceptable
configuration is often a stronger model at lower effort - and often not, which is why you measure. Every cost
number in that search is only as good as the cache behind it.

### Effort, per model

`output_config: {"effort": "low" | "medium" | "high" | "xhigh" | "max"}` controls how much the model thinks, and
therefore tokens, cost and latency. The default is `high` on Claude Opus 5 and Claude Sonnet 5 and `medium` on
Claude Opus 5.5; Claude Haiku 4.5 has no effort parameter at all (the API answers 400 - lab 07 finds that
Kestrel's own deployment record claims `effort: medium` for its Haiku route). Changing the top-level effort between
requests invalidates the prompt cache; the per-message effort system message (beta
`mid-conversation-output-config-2026-07-01`) changes it mid-conversation without a reset on the models that
support it.

### Walking the staircase

Lay the cells out with model tier down the side and effort across. Cost rises with effort within a row; quality
rises, weakly, along both axes. A staircase walk enters at the strongest cost-plausible model at low effort, steps
down a tier on a pass and right on a fail, prunes every cell projected to cost more than the current incumbent, and
stops when no cell is left under the incumbent's cost - visiting roughly tiers + effort notches cells instead of the
whole grid. It runs against gates written before the first measurement:

| Gate | Kestrel's value | Why |
|---|---|---|
| Latency | p95 <= 30 s | the go-live SLA |
| Quality band | observed/expected lower bound >= 0.90 | at most 10% worse than the incumbent on the cell's own traffic |
| Cost margin | at least 10% cheaper per successful trace | a switch must pay for its risk |
| Mechanism | the predicted cause is visible | e.g. fewer thinking tokens for lower effort |

In the traces, the only clean axis is effort within Claude Opus 5: same prompt, same week. Low to high buys 13x
the thinking tokens, 1.9x the cost and 7.8x the p95 latency; high effort has the best observed/expected ratio and
fails the SLA at a two-minute p95. The model axis is confounded (every Opus arm runs prompt v15, the Sonnet arm
v14), so the traces rank effort within Opus but cannot rank Opus against Sonnet. Quality differences need hundreds of
traces per cell (lab 06: about 471 to show "at most 10% worse"); cost and latency settle in a few dozen calls. Run
the quality axis as a paired offline comparison on the eval set, and use production cells for cost, latency and
cache health.

### Cache health is a release metric

| Check | What it catches | How |
|---|---|---|
| Cache-read share per route | a release that broke caching (a timestamp in the prefix, reordered tools) | the API's `usage` fields, compared with the incumbent's |
| Prompt size vs the model's minimum | a route that cannot cache at all | per-turn prompt tokens vs 512 (Opus 5, 5.5), 1,024 (Sonnet 5), 4,096 (Haiku 4.5) |
| Cold start after a switch | the first hour of a new model or route | caches are model-scoped: pre-warm, expect a cost bump |
| Telemetry schema | a dashboard that misreads usage | recompute recorded cost from the recorded usage |

Caching cut the baseline arm's cost per trace by 29% with nothing else changed - the largest lever in this system,
and one that breaks silently. Lab 06 also catches two telemetry problems: the trace exporter records `input_tokens`
as the whole prompt (the API's `input_tokens` is the uncached remainder; total prompt = `input_tokens` +
`cache_creation_input_tokens` + `cache_read_input_tokens`), so reading it the API's way overstates cost by 15%; and
the fast path reports cache reads its model cannot produce - its prompts average under 2,000 tokens per turn,
below Claude Haiku 4.5's 4,096-token minimum. On the same triage prompt, the "cheap" model costs more per call than
Claude Sonnet 5 reading the prompt from cache.

---

## 7. Release engineering: canaries, drift, rollback, migration audits

**The idea.** A release is an experiment on customers, run under a deadline, with a way back. The first course
compared shadow, canary, A/B and blue/green rollouts and routed a canary by a stable hash of the ticket id. This
section adds the statistics that make a canary decision defensible, the monitors that notice when the world or the
system moves, the rollback that fixes what was already sent, and the audit that a model migration needs.

### Canary analysis is sequential

A canary is watched continuously, and a fixed-sample test inspected after every trace is a new test at every look.
In lab 07's simulation, checking a 5%-level test after every trace raises the false-alarm rate to 17%. Wald's
sequential probability ratio test (SPRT) is built to be looked at after every observation: it accumulates a
log-likelihood ratio between H0 (the healthy rate) and H1 (the regression you must catch) and stops at boundaries
set by alpha and power, holding the false-alarm rate near 5% and stopping early when the effect is real.

| Canary element | Kestrel's fast path |
|---|---|
| Decision metric | severe defects: a failed run, a wrong order id, a CSAT of 1-2 |
| Control | the concurrent baseline has 6 traces; a historical one (80 traces) is valid only if traffic has not drifted |
| H0 / H1 | the control's rate (floored at 2%) / 12%, written down before the canary starts |
| Guardrails (no statistics) | any reply naming another order than the customer's; any failed run rated 1-2 |
| Planning | the SPRT's average sample number under H1 -> days at the canary's traffic |

The canary's SPRT crosses its boundary on the ninth trace, the same day its guardrails fire - on two events, so the
verdict hinges on the pre-registered H0 (at 3% it would still be sampling). Guardrails are why a small canary is
safe: at 1.8 canary traces a day the SPRT would need about eleven days on average to decide.

### Drift detection

| Statistic | For | Read it as |
|---|---|---|
| PSI | category mixes; numeric features on reference-decile bins | a distance; compare with a noise floor at your sample sizes |
| Kolmogorov-Smirnov D | numeric features | an effect size - with hundreds of traces any p-value is small |
| A rate with an interval | hand-offs, edits, cache reads | the same statistics as section 2 |

The PSI rules of thumb (0.1 stable, 0.25 shifted) come from credit portfolios of thousands of records. At a week of
tickets they are inside the noise: lab 07 measures the noise floor by resampling the reference week (0.324 for the
ten-category mix at 116 traces). Separate input drift (what customers ask) from output drift (what the system does),
join the monitor to the change log, and alert on what the change log does not explain. Watch for missing drift too:
v15 promised "be brief", and reply length never moved.

### Rollback

Roll back the smallest thing that removes the harm: for v15 that is the return_request slice (route it back to v14:
a flag, minutes), not the whole prompt, whose policy and citation fixes hold everywhere else. A runbook states
the trigger, the steps, a verification for each step, a rollback of the rollback, and who decides. A rollback does
not fix what was already sent: lab 07 lists from the traces the customers promised a colleague who was never
assigned, and the one who received another order's status. When a rollback changes the model, check thinking
binding for in-flight conversations: Claude Sonnet 5 reads Claude Opus 5's thinking blocks, but a conversation moved
from Claude Opus 5.5 back to Claude Opus 5 loses them.

### Model-migration audits as code

A migration (here the Opus arm, Claude Opus 5 -> Claude Opus 5.5) is audited by sending the production request
shapes to the target model, not by reading release notes:

| Area | Check | Claude Opus 5 -> 5.5 |
|---|---|---|
| Request shape | probe every route's request on the target | `thinking: {"type": "disabled"}` and forced `tool_choice` are 400s |
| Defaults | where a route omits a parameter | effort default `high` -> `medium`: set it explicitly |
| Prompt cruft | text written for older models | emphatic capitals, "think step by step", word caps, "show your reasoning" (can be declined as `reasoning_extraction`) |
| Thinking binding (Day 3) | a conversation moved between models, or edited | Opus 5 -> 5.5 keeps the blocks; 5.5 -> 5 drops them; an edited system prompt is a 400 where the prefix check is enforced |
| Price and cache | re-baseline cost; the switch starts cold | $4 / $20, cache reads 0.05x; re-measure token volumes |
| Refusals | new classifier categories | handle `stop_reason: "refusal"`, opt into fallbacks |
| Evals | the gates of labs 02-04 on the target | paired, offline, before any canary |

The binding probes use `thinking.block_binding.prefix_mismatch_behavior` and the `input_transformations` report,
which need the `thinking-binding-controls-2026-08-01` beta header (sent through `client.beta.messages`); without
it, dropped blocks are dropped silently. The controls are beta: when they graduate, expect to send the same fields
without the header - check the migration guide then, and keep the probe in the audit either way.
Accounts created on or after 2026-08-31 get the prefix check enforced by default (Day 3 teaches the append-only
harness it requires). The operational consequence is a runbook rule: a prompt hotfix applies to new conversations
or arrives as a mid-conversation system message - it never edits a live conversation's prefix.

---

## 8. The release checklist

| Phase | Item | Lab |
|---|---|---|
| Before | the eval set's version and known gaps are stated; labels adjudicated where sources conflict | 01 |
| Before | the eval is big enough for the effect you care about (power, MDE); otherwise say what it cannot see | 02 |
| Before | one change per arm; if two changed, say which comparison attributes what | 04 |
| Before | the judge (model, rubric id) is pinned and calibrated; position bias measured | 03 |
| Before | gates, slices, margins, minimum sizes, corrections and zero-tolerance checks are written down | 04 |
| Before | prompt changes chosen on validation, reported on a sealed test split | 05 |
| Before | the operating point passes latency, quality-band, cost and mechanism gates | 06 |
| Before | migration audit: request probes, defaults, cruft, binding, price and cache | 07 |
| Launch | canary with a concurrent control, a pre-registered SPRT, guardrails, a planned duration | 07 |
| Launch | caches pre-warmed; cache-read share per route compared with the incumbent's | 06 |
| Watch | drift monitors with measured noise floors, joined to the change log | 07 |
| Watch | critical checks on every reply; review findings counted by type | 04 |
| Decide | REVIEW has an owner and a deadline; INSUFFICIENT holds the rollout | 04 |
| Rollback | the smallest safe rollback, verification per step, customer remediation from traces | 07 |
| After | the failure becomes an eval item and, if it can, a guardrail | 01, 04 |

---

## Case study: Kestrel's v15 rollout

**Requirements.** Prompt v15 for the Service Desk Copilot had two goals: fix the wording of policy citations
(reviewers kept finding wrong or missing citations) and shorten replies. Kestrel's go-live constraints still held:
under $0.40 per ticket, replies within 30 seconds, every safety ticket in front of a human within the hour.

**Design.** v15 was merged on 2026-08-24 after passing the first course's golden set. It went to 50% of traffic on
2026-08-31 - on Claude Opus 5, while the baseline stayed on Claude Sonnet 5 with v14. The release notes read:
"Reworded policy citations; added 'be brief' guidance; new instruction to hand off ambiguous returns to a
colleague." A week later the review looked at the A/B dashboard, saw success and CSAT inside their intervals and
policy findings down (1/21 reviews against 4/20), kept v15 at 50% and started an effort staircase on its arm. On
2026-09-09 a fast path on Claude Haiku 4.5 took 30% of order-status and product-inquiry tickets as a canary.

**What went wrong.** The hand-off instruction had no tool behind it. The model read "ambiguous" generously and told
customers "I have escalated your return request to a colleague, who will review it and reply within two business
days" - without calling `escalate_to_human`. Nobody was assigned; an RMA had been opened that the customer was
never told about. Every signal was in the traces from the first day: a sequential test on hand-off language crosses
its boundary on 2026-08-31, reviewers filed the new finding type from 2026-09-04 with passing scores, and the
return-request slice sat at p = 0.037 on the generic metric. None of them was on the dashboard, which showed
averages over all categories and a score, not a finding count. On 2026-09-14 - three weeks after the merge, two
weeks into the rollout - a customer complained. In the same fortnight the fast path sent a customer another
order's status (its guardrail would have fired on 2026-09-13), and its cost on the dashboard included cache reads
its model cannot produce.

**What changed.**
* A derived-metric library: claims in the reply checked against actions in the trace (exercise 12), run on every
  reply as guardrails and in every gate as critical checks.
* The release gate of lab 04: per-slice non-inferiority on the difference, minimum slice sizes, zero-tolerance
  checks, Holm across gates, BH across monitors.
* Review findings counted by type, with an alert when a type is new for a version.
* Canaries with a concurrent control, a pre-registered SPRT and guardrails; drift monitors with measured noise
  floors joined to the change log (lab 07).
* One change per arm: v16 (v15 without the hand-off sentence, plus a line tying any hand-off to the escalation tool)
  is tested on Claude Opus 5 against v15 on Claude Opus 5; the model decision is a separate staircase (lab 06).
* The runbook: returns routed back to v14 within minutes, the fast path stopped, and the four customers behind six
  affected replies contacted from the traces.

---

## Lab walkthrough

Run each lab from the repository root. Excerpts are mock-mode output; labs 01, 02 and 04 make no API call, so their
numbers are the same live.

### Lab 01 - `01_mining_evals_from_traces.py`: from 480 traces to a stratified, labelled, split eval set

`python advanced/day6_eval_science_release/labs/01_mining_evals_from_traces.py` (`--size 160` for a bigger set).
Pure Python: it inventories the traces, measures agreement between the label sources, de-duplicates, compares
random and stratified sampling, shows the leakage of a split by trace, and writes `eval_set.jsonl` and
`manifest.json` under `.runs/advanced/day6/evals/`.

```
  signals         traces with both  agreement  kappa  pass rate (1st)  pass rate (2nd)
  --------------  ----------------  ---------  -----  ---------------  ---------------
  review vs csat                69      82.6%   0.49            81.2%            75.4%
  review vs edit               154      87.7%   0.67            73.4%            76.6%
  csat vs edit                 220      76.8%   0.45            67.3%            72.3%
...
  Empty (category, version) cells: random 4, stratified 0. A regression that lives in one cell is invisible when that cell is empty.
...
  by trace : 80/20 at random -> 13 tickets appear on BOTH sides (17 of the 24 held-out items have a sibling in the training side)
  by ticket: train 72 / validation 27 / test 21 items, 0 tickets shared - a prompt tuned on the training side has never seen a held-out ticket.
...
  version sha256:c4f544a07799  pass rate 72.5%  splits {'test': 21, 'train': 72, 'validation': 27}  labels {'csat': 4, 'review': 116}
```

Observe: raw agreement looks fine and kappa does not; the stratified draw fills every (category, version) cell but
leaves `opus-v15-high` with one item, because arms were not a stratum; the manifest's hash is what a gate quotes.

### Lab 02 - `02_confidence_intervals_and_paired_tests.py`: how much an eval number is worth

`python advanced/day6_eval_science_release/labs/02_confidence_intervals_and_paired_tests.py` (`--judged-sample 30`
adds a judged sample through the API). Intervals per arm, the eyeball simulation, paired vs unpaired designs, power
and multiple comparisons, and pass^k on a scripted agentic task:

```
  arm             success  rate   95% Wilson   95% bootstrap  noise floor
  --------------  -------  -----  -----------  -------------  -----------
  baseline        265/359  73.8%  69.0%-78.1%    69.1%-78.3%  +/-4.5%
  opus-v15        52/74    70.3%  59.1%-79.5%    59.5%-81.1%  +/-10.2%
  opus-v15-high   18/21    85.7%  65.4%-95.0%   71.4%-100.0%  +/-14.8%
  opus-v15-low    10/15    66.7%  41.7%-84.8%    40.0%-86.7%  +/-21.6%
  haiku-fastpath  9/11     81.8%  52.3%-94.9%   54.5%-100.0%  +/-21.3%
...
  The trap: collapse each ticket to a majority verdict per arm and run McNemar: pass->fail 9, fail->pass 0, exact p = 0.004.
...
  drop to detect  traces per arm
  --------------  --------------
              3%           3,491
              5%           1,284
             10%             336
             15%             155
```

Observe: the canary's interval spans 43 points; the McNemar trap (a significant p-value from a test whose
assumption - both verdicts measured the same way - is violated); and that lab 01's 120-item set, used as an
aggregate gate, can only see a collapse.

### Lab 03 - `03_pairwise_judges_and_bradley_terry.py`: judges, swaps, rankings, drift

`python advanced/day6_eval_science_release/labs/03_pairwise_judges_and_bradley_terry.py` (`--limit 30` live).
It judges all 120 pairs in both orders with a tie-allowed rubric, a forced-choice variant and a second rubric (720
calls on Claude Sonnet 5; $0.81 of simulated usage at list prices in mock mode):

```
  tie allowed (v1)         survives the swap 119/120   changed to/from tie   0   followed the position   1   (A-first order said {'A': 19, 'B': 6, 'tie': 95})
  forced choice (v1)       survives the swap  24/120   changed to/from tie   0   followed the position  96   (A-first order said {'A': 114, 'B': 6})
...
  rubric v2 ('empathy counts, prefer concise'): 46/120 verdicts differ from rubric v1 on the SAME replies
  v1 vs H1 kappa 0.15   v2 vs H1 kappa 0.15   v1 vs v2 kappa 0.37
```

Observe: agreement with the humans read against their own ceiling (kappa 0.23); the forced-choice judge's
position bias and how the swap filter removes it; the Bradley-Terry intervals; and a rubric change that moves 46
of 120 verdicts and reorders the arms without moving the calibration number. `[mock]` The judge is a
transparent keyword heuristic that sees hand-offs, over-promises, order-id contradictions and tone; the forced
variant resolves every equal pair by position. Run live to measure Claude.

### Lab 04 - `04_finding_the_regression.py`: the regression the average hid

`python advanced/day6_eval_science_release/labs/04_finding_the_regression.py`. The A/B dashboard, a Simpson's
reversal, slices with corrections, the derived metric, the reviewers' findings, the gate and the sequential test:

```
  category         haiku-fastpath  rate    opus-v15  rate    canary - opus
  ---------------  --------------  ------  --------  ------  -------------
  order_status     6/6             100.0%  5/5       100.0%          +0.0%
  product_inquiry  3/5              60.0%  3/4        75.0%         -15.0%
  ALL traffic      9/11             81.8%  52/74      70.3%         +11.5%
...
Gate on the concurrent A/B up to 2026-09-06: 62 baseline traces, 54 candidate traces
  slice              n v14  n v15  v15 - v14  95% CI of difference  critical  status
  -----------------  -----  -----  ---------  --------------------  --------  ------------
  order_status          13      4     +23.1%            -28%..+50%         0  INSUFFICIENT
  shipping_delay         9      4      -2.8%            -50%..+36%         0  INSUFFICIENT
  return_request         7     10     -31.4%            -62%..+14%         3  BLOCK
...
  release gate: BLOCK   (BLOCK 1, INSUFFICIENT 9)
...
  2026-08-31  TR-00233  HAND-OFF  cumulative  2  LLR   4.61 <- crosses the upper boundary
```

Observe: the dashboard that said "keep going", with its 2.7x cost column caused by the model, not the prompt; the
reversal; the hand-off metric surviving Holm and BH while the generic metric survives nothing; the reviewers'
finding table; and that one week of data is INSUFFICIENT everywhere except the check that needs one trace.

### Lab 05 - `05_prompt_hill_climbing.py`: train, validate, and only then test

`python advanced/day6_eval_science_release/labs/05_prompt_hill_climbing.py` (add your own round with
`--rule 'two things|two issues=>technical_support'`). Four rounds of a triage prompt on the 62 tickets (248 calls):

```
  round  change                                       train  validation  test
  -----  -------------------------------------------  -----  ----------  -----
  v0     baseline prompt, no rules                    30/37  8/12        10/13
  v1     rules generalised from train failures        35/37  8/12        11/13
  v2     train failures patched with their own words  37/37  7/12        10/13
  v3     definitions from the triage guidelines       35/37  9/12        12/13
  Skip the holdout and choose by train, and you ship v2 with a reported 100.0% - its real test score is 10/13, no better than
  not tuning at all. That is overfitting to the eval: the score measures memorised tickets, not triage.
  T-1203 ('Return one KC-2', a return_request) became warranty_claim on the test split: it contains the patch
  phrase "SO-10257" copied from a different ticket.
```

Observe: v1 lifts train by five tickets and leaves validation flat; v2 is reverted by the rule written before round
one; the sealed test split is opened once, for v3. `[mock]` The triager obeys the prompt's `<rules>` block
literally (first match wins) and cannot read the Spanish and German tickets; Claude reads the rules as prose, reads
Spanish and starts from a much stronger v0 - measure the headroom live before planning rounds.

### Lab 06 - `06_model_effort_staircase.py`: choose an operating point you can defend

`python advanced/day6_eval_science_release/labs/06_model_effort_staircase.py` (`--sample 12` live: 60 calls). The
cells in the traces, the telemetry schema check, caching health, a live-capable sample of one prompt on five
cells, and the decision:

```
  arm             model             effort  n   success  O/E (95% CI)      cost/trace  cost/success  p50    p95     thinking tok
  --------------  ----------------  ------  --  -------  ----------------  ----------  ------------  -----  ------  ------------
  baseline        claude-sonnet-5   medium  65  50/65    1.04 (0.89-1.16)     $0.0196       $0.0255  16.2s  24.4s            497
  opus-v15-low    claude-opus-5     low     15  10/15    0.93 (0.58-1.18)     $0.0426       $0.0640  8.9s   15.3s            121
  opus-v15        claude-opus-5     medium  20  13/20    0.88 (0.59-1.11)     $0.0484       $0.0745  24.3s  33.1s            482
  opus-v15-high   claude-opus-5     high    21  18/21    1.16 (0.88-1.29)     $0.0792       $0.0924  71.6s  119.0s         1,564
  haiku-fastpath  claude-haiku-4-5  (none)  11  9/11     1.05 (0.67-1.22)     $0.0044       $0.0054  4.8s   6.3s               0
...
  haiku-fastpath             48.0%        1,742          4,096  IMPOSSIBLE           $0.0044           $0.0064
...
  claude-haiku-4-5  (none)  4/8      21.5%-78.5%                           33                       0    $0.0018  -
...
  Decision - the operating point stays at claude-sonnet-5, effort medium (the incumbent):
```

Observe: the within-Opus effort staircase (13x thinking tokens, 7.8x p95 from low to high); the 15% cost
overstatement from reading the telemetry with the API's convention; the fast path's impossible cache reads; the
sample's cost axis separating the cells after eight calls while accuracy separates nothing; and the staircase walk's
next probes (cells cheaper than the incumbent only). `[mock]` Each sample cell starts with an empty cache, answers
come from one heuristic, and latency is local compute; token counts follow the effort level and the per-model cache
minimum is enforced as the API documents.

### Lab 07 - `07_canary_drift_rollback.py`: canary, drift, rollback, migration audit

`python advanced/day6_eval_science_release/labs/07_canary_drift_rollback.py`. Pure Python for steps 1-4; step 5
sends eight probe requests:

```
  monitor                                false alarm (no regression)  detection (regression)  mean traces to stop (H0 / H1)
  -------------------------------------  ---------------------------  ----------------------  -----------------------------
  fixed test, one look at n = 200                               4.7%                   99.9%                            200
  same test after EVERY trace (n >= 10)                        17.0%                  100.0%  -
  SPRT (alpha 0.05, power 0.8)                                  3.5%                   80.4%  29 / 34
...
  2026-09-13  TR-00453  order_status     resolved  -     yes     +2.83 <- boundary  GUARDRAIL: order id SO-10999 != customer's SO-10312
...
  category mix            input             0.082             0.053             0.014         0.324
...
  latency                 output  0.182  D=0.09     0.306  D=0.22     0.636* D=0.24           0.308
...
  A2   BLOCKS FAIL   extractor: thinking disabled
...
  A4   BLOCKS FAIL   fast path as recorded (effort=medium)
                     claude-haiku-4-5 today: 400: output_config.effort: effort is not supported for claude-haiku-4-5
...
  A7   TUNE   ACTION roll back mid-conversation
                     claude-opus-5-5 -> claude-opus-5: input_transformations: thinking_dropped (model_binding_mismatch)
```

Observe: peeking's false alarms; the canary's guardrails and its SPRT stopping on the same day, and how the verdict
depends on the pre-registered H0; inputs stable and outputs drifting as the change log predicts; the runbook and the
decision record written to `.runs/advanced/day6/release/`; and an audit that finds two 400s waiting for the
switch-over, a config error already in production, a rollback that drops thinking, and a prompt hotfix that breaks
in-flight conversations. `[mock]` The probes run against the mock's request validation, which mirrors the
documented rules; `claude-opus-5-5` behaves as an account created before 2026-08-31, so the hotfix probe opts into
enforcement with `prefix_mismatch_behavior: "error"`.

---

## Key takeaways

1. Traces are the best eval source you have; their labels are measurements with errors you can quantify. Record
   each label's source, adjudicate conflicts, split by ticket, and version the set.
2. Every eval number needs an interval, and every eval needs a size chosen before it runs: 120 items see a 17-point
   drop, not a 5-point one.
3. Pair when both versions answer the same items - and check a test's assumptions before believing its p-value.
4. With many slices, something is always red. Pre-register gates and monitors, correct with Holm and BH, and turn a
   flag into a finding only with a mechanism.
5. A judge is an instrument: read its agreement against the human ceiling, offer "tie", judge both orders, pin its
   version, and re-calibrate it against its previous version, not just against humans.
6. Aggregates hide regressions and can reverse them. Slice, standardise, and derive metrics from claims in the reply
   vs actions in the trace.
7. A release gate says INSUFFICIENT when it cannot conclude, REVIEW when the interval straddles the margin, and
   BLOCK on a critical check - which needs one trace, not a sample.
8. Hill-climb on train, choose on validation, report test once; the overfit signature is train up, validation down.
9. Model and effort are one surface: walk the staircase against pre-registered gates, and remember that quality
   needs hundreds of samples while cost and latency need dozens.
10. Cache health is a release metric: check prompt size against each model's minimum, compare cache-read share
    with the incumbent, and verify your telemetry's schema before trusting any cost.
11. Watch canaries with sequential tests and guardrails, measure drift against a noise floor, roll back the smallest
    thing that removes the harm, and remediate what was already sent.
12. Audit a migration by probing the real request shapes on the target model - including a rollback and a prompt
    hotfix on in-flight conversations.

## Further reading

* The first course's evals day: [`../../day6_evals_guardrails_production/README.md`](../../day6_evals_guardrails_production/README.md)
  (eval pyramid, golden sets, judge calibration, canaries); preserved thinking and binding in this course's
  [Day 3](../day3_long_horizon_context/README.md).
* Effort: <https://platform.claude.com/docs/en/build-with-claude/effort.md> · adaptive thinking
  <https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking.md>
* Prompt caching (minimum prefix per model, usage fields): <https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md>
* Model migration guide (Claude Opus 5 -> Claude Opus 5.5 and the preserved-thinking checks):
  <https://platform.claude.com/docs/en/about-claude/models/migration-guide.md>
* Pricing: <https://platform.claude.com/docs/en/about-claude/pricing.md> · cost and intelligence
  <https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md>
* Structured outputs (judge and triage verdicts): <https://platform.claude.com/docs/en/build-with-claude/structured-outputs.md>
* Token counting: <https://platform.claude.com/docs/en/build-with-claude/token-counting.md> · usage and cost
  reporting <https://platform.claude.com/docs/en/manage-claude/usage-cost-api.md>
* Refusals and server-side fallbacks: <https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback>
* The methods, by their classic sources: Wilson (1927) score interval; Newcombe (1998) intervals for a difference of
  proportions; McNemar (1947) and Connor (1987) for paired designs; Wald (1945) sequential probability ratio test;
  Holm (1979) and Benjamini & Hochberg (1995) for multiple comparisons; Cohen (1960) kappa; Bradley & Terry (1952)
  and Hunter (2004) for paired-comparison rankings.
