# Day 6 solutions - worked answers

Runnable solutions: `ex01_label_quality.py` (exercise 1e), `ex02_sample_size.py`, `ex03_pass_k.py`,
`ex04_bradley_terry.py`, `ex06_multiple_comparisons.py`, `ex10_kappa_calculator.py`, `ex11_drift_monitor.py`,
`ex12_contradiction_detectors.py`. They are pure Python over the dataset (no API calls), so their numbers are exact
and identical in mock and live mode. Two of them (`ex01`, `ex12`) read the hidden `_truth` field to grade a method
after the fact - something a lab must never do, and a solution may.

---

## 1. Labels, duplicates and splits

**a. Conflicting signals.** Lab 01's policy is "review > CSAT > edit", so the trace is labelled *pass* with source
*review*. The CSAT of 2 contradicts it, and the policy silently discards that. Before the item enters a set that
gates releases, send it to adjudication: a person reads the reply against the ticket and the policy and writes the
label, and the eval set records both the adjudicated label and the fact that the sources disagreed. Conflicts are
cheap to find (a query) and they are exactly the items where the weak label is most likely wrong. `ex01` shows how
wrong each source is: the review is the best one and still passes 2 of 36 flawed replies (5.6%), CSAT errs about 14%
in both directions, and the edit signal passes 24% of flawed replies because nobody touched them.

**b. The de-duplication key.** Text alone merges replies that are identical across arms - templated categories
produce the same sentence from v14 and v15 - so the version comparison loses items exactly where the arms behave
alike, and you cannot tell "no difference" from "no data". The ticket alone collapses every answer to one per
ticket (62 items): no variety of answers, no arm coverage. (ticket, arm, reply text) keeps one item per distinct
answer of each arm, and lab 01 keeps the copy with the strongest label.

**c. Random split by trace.** Two independent ways the held-out score flatters:

* *Leakage through siblings.* Replies to one ticket share its facts and phrasing. Lab 01's random 80/20 split put
  13 tickets on both sides, and 17 of the 24 held-out items had a sibling in training - a prompt, a judge or a
  classifier tuned on the training side has already seen the answer.
* *Leakage through time.* Traces from later days sit in training while earlier ones are held out, but production
  always asks about the future: a time-based split (train on the past, validate on the most recent week) is the
  honest one for anything that drifts.

A third, subtler one: once the held-out part has been looked at for a few decisions, it is a validation set, and
the next number reported from it is optimistic too. Keep a sealed test split.

**d. Stratify on the question.** The next question is about effort, so effort (arm) must be a stratum:
(category x arm) with a floor per cell, or - because the staircase arms are small (56 traces between them in the
concurrent week) - take them all (a census of the rare arms) and sample only the large baseline arm. When a
stratified set is used to estimate an overall rate, weight each item by the inverse of its sampling fraction.

**e. Agreement is not accuracy.** Two sources that agree could share a mistake; kappa between them is symmetric and
says nothing about which one is right. `ex01_label_quality.py` grades them against the truth:

```
  source                      traces  accuracy  kappa  false pass (flawed -> pass)  false fail (clean -> fail)
  review                         154     94.2%   0.84  2/36 = 5.6%                  7/118 = 5.9%
  csat                           220     85.9%   0.66  8/57 = 14.0%                 23/163 = 14.1%
  edit                           480     87.7%   0.68  31/129 = 24.0%               28/351 = 8.0%
  weak label (lab 01 policy)     480     87.7%   0.70  24/129 = 18.6%               35/351 = 10.0%
```

The reviewers' recall per issue type ranges from 60% (missing citations) to 100% (tone, over-promises), and 7
reviews list an issue on a clean reply. The ranking most people predict (review first) holds, but the weak label's
18.6% false-pass rate is the number that matters for a gate: good enough to *find* candidates, not to gate on
without a hand-verified sample.

## 2. How many traces to see a 5-point drop?

`ex02_sample_size.py` prints every step. With z(0.975) = 1.960 and z(0.80) = 0.842:

a. Unpaired, 74% -> 69%, pooled p = 0.715:
   n = [1.960 x sqrt(2 x 0.715 x 0.285) + 0.842 x sqrt(0.74 x 0.26 + 0.69 x 0.31)]^2 / 0.05^2
     = (1.2512 + 0.5365)^2 / 0.0025 = **1,279 traces per arm**. A simulation at that size gives 81.8% power.
   (Lab 02 prints 1,284 because it uses the measured baseline, 73.8%.)

b. With 120 per arm the minimum detectable drop is **17.1 points** (the quick approximation
   (z_a + z_b) x sqrt(2p(1-p)/n) gives 15.9). A 120-item aggregate gate sees a collapse, nothing else.

c. Paired, discordant share psi = 0.07, net effect delta = 0.05 (Connor's formula):
   n = [1.960 x sqrt(0.07) + 0.842 x sqrt(0.07 - 0.0025)]^2 / 0.0025 = **218 scenarios** - 5.9x fewer, because only
   the scenarios that flip carry information. The saving depends on churn, not on the baseline rate: a change that
   flips many scenarios both ways (a model migration) needs far more pairs.

d. Bonferroni over 10 slices, alpha = 0.005 per slice: **2,169 traces per arm per slice**, 21,690 per arm in all.
   Statistical per-slice gates on a 5-point effect are out of reach for Kestrel's volume; slices get monitors,
   mechanisms and critical checks (lab 04).

e. One-sided non-inferiority, margin 5 points, true difference 0:
   n = (1.645 + 0.842)^2 x 2 x 0.74 x 0.26 / 0.05^2 = **952 per arm**. Proving "no worse than" costs the same order
   as detecting a drop of the same size; the margin, not the direction, drives the sample size.

## 3. pass@k and pass^k

From `ex03_pass_k.py`:

```
  scenario                   passed  pass@3  pass^3 (unbiased)                   naive p^3
  S1 order status            10/10   100.0%  C(10,3)/C(10,3) = 120/120 = 100.0%     100.0%
  S2 return, eligible        9/10    100.0%  C(9,3)/C(10,3) = 84/120 = 70.0%         72.9%
  S3 warranty with photos    8/10    100.0%  C(8,3)/C(10,3) = 56/120 = 46.7%         51.2%
  S4 two-issue email         6/10     96.7%  C(6,3)/C(10,3) = 20/120 = 16.7%         21.6%
  S5 return past the window  3/10     70.8%  C(3,3)/C(10,3) = 1/120 = 0.8%            2.7%
  mean pass@3 93.5%   mean pass^3 46.8%   mean naive 49.7%   pooled rate 72.0% -> pooled^3 = 37.3%
```

a. pass@3 = 1 - C(n-c, 3)/C(n, 3); pass^3 = C(c, 3)/C(n, 3). The naive (c/n)^3 overstates pass^3 on every
   imperfect scenario because it treats the observed rate as exact.
b. The mean of per-scenario pass^3 (46.8%) and the pooled rate cubed (37.3%) differ because a power of an average
   is not the average of the powers: failures cluster on S4 and S5, and pooling spreads them evenly over all five.
   A support agent's customers see every reply, so report **pass^k per scenario** (S5 alone is 0.8%) with the mean
   as a summary; pass@k belongs to draft-and-verify workflows where a checker keeps the best attempt.
c. From 5 runs pass^3 can only be 0%, 10%, 40% or 100%. If S3 truly passes 80% of its runs (pass^3 = 51.2%), five
   runs report 0% with probability 6%, 10% with 20%, 40% with 41% and 100% with 33%: a number, not an estimate.

## 4. Reading a Bradley-Terry table

`ex04_bradley_terry.py` refits the table and prints the full matrix.

a. P(haiku-fastpath beats opus-v15) = 2.17 / (2.17 + 1.19) = **64.5%**; P(opus-v15-low beats baseline) =
   0.75 / (0.75 + 1.00) = **42.9%** - probabilities of winning a decided comparison (ties counted half-half).

b. Reasons to doubt "the canary is the best arm":
   * 15 comparisons and an interval from 1.08 to 4.88 - compatible with "slightly better than baseline" and with
     "five times stronger";
   * it only ever answered order_status and product_inquiry tickets (6 and 9 of its pairs): Bradley-Terry puts all
     arms on one quality scale, but an arm that never faces warranty or safety tickets is measured on an easier one -
     the Simpson's reversal of lab 04 in another form;
   * the labels come from one annotator whose agreement with a second is kappa 0.23.

c. Raw win rate depends on who an arm happened to face (strength of schedule); Bradley-Terry estimates strengths
   jointly, so beating a strong arm counts more than beating a weak one - the idea behind chess ratings. It is not
   enough when quality differs by category (one scale is wrong), when preferences are intransitive, when the
   comparison graph is sparse or disconnected, and when the verdicts themselves are noisy or position-biased.

d. Spend the 60 judgments where the decision is: canary vs baseline on the same order_status and product_inquiry
   tickets, in both presentation orders, with a second annotator on a fifth of them. The parametric bootstrap in
   `ex04` shows what that buys:

```
  design                                 canary comparisons  95% range of its strength  hi / lo
  today's graph                                          15                  0.73-3.98     5.4x
  + 60 spread over all 10 arm pairs                      39                  1.03-3.06     3.0x
  + 60 canary vs baseline, same tickets                  75                  1.13-2.56     2.3x
```

## 5. The judge changed under you

a. Nothing about v15. Two parts of the instrument changed at once (model and rubric), so the jump could be either
   change, both, or v15; a rubric that "prefers concise replies" can move win rates on identical replies. Lab 03
   shows the mechanism: rubric v2 changed 46 of 120 verdicts on the same replies.

b. The re-calibration protocol:
   1. Pin (judge model, rubric id) on every stored verdict; a change of either is a new instrument.
   2. Keep a fixed calibration set with human labels (lab 03's 120 pairs, both orders).
   3. On any change, run old and new instruments on the calibration set and compare the new one with the humans
      AND with the old one. Lab 03 is the warning: v1 and v2 both have kappa 0.15 against H1, yet agree with each
      other at kappa 0.37 and rank the arms differently - a flat calibration number does not prove a stable
      instrument.
   4. Re-judge the historical overlap with the new instrument before splicing a trend line; otherwise start a new
      chart.
   5. Change one thing at a time.

c. In one order the forced-choice judge says "A" on 114 of 120 pairs (every pair it finds equal goes to the reply it
   read first), so its verdicts barely co-vary with the human's and kappa sits at chance (-0.00). The swap filter
   turns the 96 verdicts that followed the position into ties; what remains is exactly the tie-allowed judge's
   verdicts (120/120 match), whose kappa is 0.17 - still low, because the judge cannot see policy and citation
   errors, but no longer an artifact of presentation order.

d. Cheaper schemes, each with a price:
   * *Swap only decided verdicts:* judge once; call again in the swapped order only when the verdict picks a side.
     With lab 03's tie-allowed judge that is 120 + 25 calls instead of 240. It gives up detecting a tie that turns
     into a side when swapped (0 cases in lab 03; measure it on the calibration set).
   * *Randomise the order, one call per pair:* unbiased on average over many pairs, but no individual verdict is
     protected, so it suits aggregate win rates, not per-ticket decisions.
   * *A cheaper judge model* after it passes the same calibration - often the largest saving.

## 6. Ten slices, three corrections

From `ex06_multiple_comparisons.py`:

a. Raw alpha 0.05 flags return_request (p = 0.0369). Bonferroni (threshold 0.005), Holm (0.005 for the smallest p,
   then 0.0056, ...) and Benjamini-Hochberg at q = 0.05 or 0.10 (i x q / 10 for the i-th smallest; 0.0369 > 0.01)
   flag nothing.

b. With 10 independent tests and no real effect, P(at least one flag) = 1 - 0.95^10 = **40%**; with 50 tests (10
   slices x 5 metrics) **92%**. A team that looks at enough slices can always find a reason to block or to ship.

c. With the hand-off metric added (p = 0.0013), every rule flags it: a real effect with a mechanism survives any
   correction. That is the practical lesson - corrections cost you the marginal findings, not the real ones.

d. A hard gate that blocks a release controls the family-wise error: use Holm (the same guarantee as Bonferroni,
   never less power). A monitor that routes slices to a person can control the false discovery rate with BH, which
   keeps more power. Written before the rollout:
   * gates - return_request and safety_incident success (Holm across the gates), plus the zero-tolerance checks;
   * monitors - every other category x metric, BH at q = 0.10; a flag means "a person looks this week";
   * a flagged slice becomes a finding only with a mechanism (a reply text, a tool pattern, a release note).

## 7. The release gate for v16

**Recommended design.**

*Offline, before any traffic.*
* Unit: the ticket, re-run through the agent - not stored replies. Run the 62 tickets with 5 reps each for v15
  and v16 on the same model (the prompt is the only change; lab 04's "two changes in one arm" is how v15 hid its
  model effect), plus the first course's 30 golden scenarios for their critical checks.
* Metrics: (1) zero-tolerance contradiction checks - a hand-off with no `escalate_to_human` call; (2) relative
  checks where the incumbent already has a rate - refund promises with no refund tool, order ids that contradict the
  customer (exercise 12); (3) per-category success from code graders; (4) swap-filtered pairwise judge verdicts
  v16 vs v15 on return_request; (5) cost per resolved ticket, p95 latency, cache-read share.
* Thresholds: zero hand-off contradictions in 8 return tickets x 5 reps; return_request success non-inferior to
  v14 with a 10-point margin on a Newcombe interval; no category's critical-check rate above the incumbent's.

*Online canary.*
* Route return_request only to v16 at 25%, with v14 on the other 75% as the concurrent control (lab 07: a control
  of 6 traces cannot be compared with; a historical one needs a drift check).
* Guardrail, no statistics: one hand-off contradiction stops the canary.
* Decision metric: severe defects with a pre-registered H0 (v14's rate) and H1, watched with an SPRT; its average
  sample number sets the planned duration, and every expansion (25% -> 50% -> 100%) repeats the gate.
* Slices: return_request and safety_incident are gates; the rest are monitors with BH (exercise 6).

*Decisions and records.* REVIEW is decided by the release owner and the support lead within one business day, in
writing. Every gate run records the eval-set version (lab 01's manifest hash), the judge's model and rubric id, the
thresholds, and the outcome.

**Rejected alternatives.** A 50% A/B on all categories (exposes every customer and is underpowered anyway, lab 02);
offline-only (v15 passed the offline golden set because its return scenarios were unambiguous); a fixed-horizon
test inspected daily (alpha inflation, lab 07 step 1); zero tolerance on order-id contradictions (the incumbent
already produces them, so the gate would block every release).

## 8. A hill-climbing protocol for the reply prompt

* **Unit and splits.** The ticket, split by ticket (splits.json: 37 / 12 / 13); the test split is sealed until the
  end and scored once.
* **What is scored every round.** Every split except test, with 5 reps per ticket (the reply agent is
  stochastic), graded by code checks (the contradiction detectors, required facts) plus the swap-filtered pairwise
  judge against the incumbent. Price one round before starting: tickets x reps x (agent + two judge calls).
* **Keep/revert rule, written first.** Keep a round only if validation does not drop beyond its noise floor
  (paired per ticket against the incumbent) and at least one split improves, AND the round names a mechanism that
  generalises. Stop after two consecutive rounds without a gain beyond the noise floor, or when the remaining
  failures are not the prompt's to fix, or at a round cap.
* **What to change first.** Definitions and policy statements, then rules that describe a behaviour seen across
  several failures, then examples - never the words, ids or codes of one ticket (lab 05's `SO-10257` patch broke a
  test ticket sharing the order).
* **Not overfitting the judge.** Calibrate it first (lab 03), keep a human-labelled subset out of the loop for the
  final report, and keep grader vocabulary out of the prompt ("the judge prefers..." is a prompt that learned the
  grader).
* **Twelve validation tickets.** Their interval is ~45 points wide. Use reps and paired comparisons (the
  per-ticket difference against the incumbent is far less noisy than two rates), treat validation as a veto rather
  than a ranking, and grow the set: mine new tickets from the trace stream every week (lab 01) so the next round
  has more than 12.
* **Report.** The test-split delta against the starting point with its interval, the list of rounds kept and
  reverted, and the cost - not the train score.

## 9. Choosing the operating point

a. All cells meet the latency SLA. The cost bar is 0.9 x $0.0255 = $0.02295 per success.

| cell | quality (lower bound >= 0.95) | cost (<= $0.02295) | verdict |
|---|---|---|---|
| Sonnet 5, low | 0.94 - fails by 0.01 | $0.0199 (0.78x) - passes | re-test |
| Opus 5, low | 0.99 - passes | $0.0640 (2.51x) - fails | no |
| Opus 5.5, low | 1.01 - passes (better than the incumbent) | $0.0470 (1.84x) - fails | no, unless the objective changes |
| Haiku 4.5 | 0.90 - fails | $0.0081 (0.32x) - passes | no (and only two categories) |

The incumbent stays today. Sonnet 5 at low effort fails the quality floor by 0.01 - inside the noise of a
480-ticket comparison - with a 22% saving at stake: re-test it before rejecting it (a fail that close to the floor
earns a second run; doubling to ~960 paired tickets narrows the interval by about 1/sqrt(2)). Opus 5.5 at low
effort is the only cell that shows higher quality; buying it is a business decision (what is a point of success
worth?) that the pre-registered gates deliberately do not make.

b. The fast path: 3 x 1,900 = 5,700 input tokens per trace, every request below Haiku 4.5's 4,096-token minimum, so
nothing is cached: input 5,700 x $1/M = $0.0057, output 330 x $5/M = $0.00165, **$0.0074 per trace**. A dashboard
that assumes ~48% cache reads (as the traces report) shows about $0.0050. The uncached number is the real one - the
model cannot cache that prompt.

c. Two ways to cache, per trace:
* *Grow the stable prefix past 4,096 tokens with content that earns its place* (say 4,200 stable + 600 variable
  tokens per request): with a warm cache 3 x (4,200 x $0.1/M + 600 x $1/M) + $0.00165 = **$0.0047**; but if every
  request arrives after the 5-minute entry expired, each one pays the 1.25x write: **$0.0192**, 2.6x worse than not
  caching. Only a route with a request every few minutes benefits.
* *Move the route to Claude Sonnet 5* (1,024-token minimum; ~1,500 of the 1,900 tokens stable): warm
  3 x (1,500 x $0.2/M + 400 x $2/M) + 330 x $10/M = **$0.0066**; cold $0.0170. Similar to uncached Haiku, with
  Sonnet's quality.

For this route caching is a minor lever; the decision is quality and the correctness guardrails of lab 07.

## 10. A kappa calculator

`ex10_kappa_calculator.py` implements the three functions (Cohen's kappa matches the labs' helper to three decimals):

```
  rater pair                      items  agreement  kappa (ours)  kappa (helper)  95% bootstrap
  H1 vs H2 (pairwise preference)     39      51.3%         0.233           0.233  0.01 to 0.46
  reviewer score vs CSAT (1-5)       69      31.9%        -0.042          -0.042  -0.17 to 0.09
  ...
  unweighted kappa -0.04   linear weighted kappa 0.07   quadratic weighted kappa 0.22 (95% 0.06 to 0.35)
```

* **Raw agreement flatters** when one label dominates or labels are few: on a 90/10 label two raters who agree on
  90% of items get kappa 0.44, and a judge that always says "tie" has kappa exactly 0 whatever its agreement.
* **Unweighted kappa under-credits** ordinal raters: it counts a 4-vs-5 like a 1-vs-5. Weighted kappa (quadratic
  weights penalise large gaps most) credits near-misses; for the reviewer vs CSAT it rises from -0.04 to 0.22 and
  still says the two scales measure different things (policy correctness vs the customer's experience).
* **Report the interval**: over 39 items H1 vs H2 spans 0.01-0.46, from "barely above chance" to "moderate".

## 11. A drift monitor

`ex11_drift_monitor.py` subclasses the starter's `DriftMonitor`; the noise floor is computed once per feature at
the typical window size (115-116 traces), which keeps the whole run near a second:

```
  feature                 family  noise floor  first drift  days flagged  longest run  PSI on 2026-09-14  verdict (change-log dates)
  category mix            input         0.319  -            0/15                    0              0.020  stable
  priority mix            input         0.114  -            0/15                    0              0.004  stable
  model mix               system        0.000   2026-08-31  15/15                  15              4.217  explained by 08-31
  tool-call mix           output        0.076  -            0/15                    0              0.008  stable
  reply length            output        0.298  -            0/15                    0              0.137  stable
  latency                 output        0.288   2026-09-06  9/15                    9              0.574  explained by 08-31
  output+thinking tokens  output        0.308   2026-09-12  3/15                    3              0.456  explained by 08-31, 09-07, 09-09
  cache-read share        system        0.268   2026-09-14  1/15                    1              0.296  UNEXPLAINED
```

* The inputs never drift. The model mix flags the day the Opus arm starts; latency and tokens lag because a
  trailing 7-day window needs enough of the new arms' traffic - the price of a stable comparison.
* One flag is unexplained: the cache-read share, for one day. A 95% floor raises about one false alarm per twenty
  feature-days, so alert only after two consecutive days above the floor (here: model mix, latency, tokens) - and
  have a person read every alert, explained or not.
* Several changes can explain one drift (tokens: the Opus arm, the effort staircase and the canary); list them all.
* What a drift monitor can never tell you: that a change failed to do what it promised. v15's "be brief" left reply
  length where it was - check each change-log entry's *expected* effect explicitly.

## 12. Contradiction detectors

From `ex12_contradiction_detectors.py`:

```
  detector                             v14     v15    baseline  opus-v15  opus-v15-high  opus-v15-low  haiku-fastpath
  hand-off without escalation          0/359   5/121  0/359     5/74      0/21           0/15          0/11
  refund promise without refund tool   15/359  4/121  15/359    4/74      0/21           0/15          0/11
  order id contradicts the customer's  7/359   4/121  7/359     2/74      1/21           0/15          1/11
```

* **A zero-tolerance gate needs a zero baseline.** Only the hand-off detector qualifies: the incumbent never does
  it, so one occurrence is a regression.
* Refund promises and order-id contradictions already happen on the incumbent (15 and 7 of 359). A zero-tolerance
  rule would block every release, the incumbent included; gate on "not worse than the incumbent" with an interval,
  and open defects for the existing rates - Kestrel has been promising refunds outside policy all along.
* All three are cheap enough to run on every reply before it is sent, as guardrails that hold the reply for a
  person.

Graded against the hidden truth (solution only):

```
  detector                             planted issue           fired  precision       recall
  hand-off without escalation          unnecessary_escalation      5  5/5 = 100.0%    5/7 = 71.4%
  refund promise without refund tool   over_promised              19  19/19 = 100.0%  19/19 = 100.0%
  order id contradicts the customer's  hallucinated_id            11  11/11 = 100.0%  11/23 = 47.8%
```

Precise, not complete: the hand-off detector misses the two hand-offs on tickets that needed a human (their traces
do call `escalate_to_human`), and the order-id check sees a hallucinated id only when both the email and the reply
name an order. A detector that never cries wolf is the right kind for a gate; its blind spots go on the monitor
list, or to a check against the tool results.
