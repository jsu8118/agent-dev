# Day 6 exercises - Evaluation science and release engineering

Twelve exercises: concept checks (1, 5), calculations (2, 3, 6), interpretation (4), design scenarios (7, 8, 9)
and hands-on coding (10, 11, 12, with starter files in this folder that run and print what is left to do).
Worked answers are in [`../solutions/README.md`](../solutions/README.md); runnable solutions are in
`../solutions/ex*.py`. Numbers quoted from the labs are their mock-mode output.

Prices used in the calculations (USD per million tokens, input / output): `claude-opus-5` $5 / $25,
`claude-opus-5-5` $4 / $20, `claude-sonnet-5` $2 / $10, `claude-haiku-4-5` $1 / $5. Cache writes cost 1.25x the
input price (5-minute TTL), cache reads 0.1x (0.05x on Claude Opus 5.5). Minimum cacheable prefix: 512 tokens on
Claude Opus 5 and 5.5, 1,024 on Claude Sonnet 5, 4,096 on Claude Haiku 4.5.

---

## 1. Concept check - labels, duplicates and splits

Lab 01 turned 480 traces into a 120-item eval set.

a. A trace has a QA review with no findings, a CSAT of 2 and no human edit. What label does lab 01's policy give it,
   and what would you do with this item before it enters a set that gates releases?
b. Why does lab 01 de-duplicate on (ticket, arm, reply text) rather than on the reply text alone, or on the ticket?
c. A colleague splits the eval set 80/20 at random by trace. Name two different ways the held-out score is
   optimistic.
d. The stratified draw left `opus-v15-high` with one item. The next question the team will ask is "which effort
   level?". Change the sampling design.
e. The three label sources agree with each other only moderately (kappa 0.45-0.67). Does that tell you which one is
   right? The solution script grades them against the hidden truth - predict the ranking before you run it.

## 2. Calculation - how many traces to see a 5-point drop?

The baseline succeeds on 74% of traces. Two-sided alpha = 0.05, power = 80%. Show the arithmetic.

a. Traces per arm for an unpaired comparison to detect 74% -> 69%.
b. With 120 items per arm, what is the minimum detectable drop?
c. A paired design (same scenarios before and after): the change flips 6% of scenarios from pass to fail and 1%
   from fail to pass. How many scenarios?
d. You want the 5-point detection in EACH of the 10 category slices, Bonferroni-corrected. Traces per arm per slice?
   What does that say about per-slice statistical gates?
e. A one-sided non-inferiority test: "v16 is at most 5 points worse than v14" when the two are truly equal.
   Traces per arm?

## 3. Calculation - pass@k and pass^k

Five agentic scenarios were each run 10 times: S1 10/10, S2 9/10, S3 8/10, S4 6/10, S5 3/10.

a. Compute pass@3 and pass^3 per scenario with the unbiased estimators, and the naive (c/n)^3.
b. Compute the set's mean pass^3 and the pooled rate cubed. Why do they differ, and which one goes in the release
   report of a support agent?
c. With 5 runs per scenario instead of 10, which values can a pass^3 estimate take? If S3 truly passes 80% of its
   runs, how likely is each reading?

## 4. Interpretation - a Bradley-Terry table

Lab 03 fitted this table from the 120 human (H1) verdicts (strength relative to baseline = 1.00):

```
  arm             strength  95% bootstrap  P(> baseline)  comparisons
  haiku-fastpath      2.17      1.08-4.88            68%           15
  opus-v15            1.19      0.74-1.83            54%           69
  opus-v15-high       1.17      0.71-1.97            54%           43
  baseline            1.00      1.00-1.00            50%           81
  opus-v15-low        0.75      0.42-1.29            43%           32
```

a. What is the probability that `haiku-fastpath` beats `opus-v15` on a decided comparison? That `opus-v15-low`
   beats the baseline?
b. A product manager reads "the canary is the best arm". Give three reasons to doubt it.
c. Why is a Bradley-Terry fit better than ranking arms by their raw win rate against everyone, and when is it still
   not enough?
d. You have budget for 60 more human judgments. Design them so the next table answers "is the fast path as good as
   the baseline on the tickets it serves?".

## 5. Concept check - the judge changed under you

In the same week the platform team moves the pairwise judge to a newer model and a teammate edits the rubric to
"prefer concise replies". The weekly chart of v15's win rate jumps from 48% to 61%.

a. What can you conclude about v15?
b. What should have happened before the chart was drawn? Write the re-calibration protocol.
c. In lab 03 the forced-choice judge has kappa -0.00 against H1 when read in one order and 0.17 after the swap
   filter. Explain both numbers.
d. Judging every pair twice doubles the bill. Propose a cheaper scheme and say what it gives up.

## 6. Calculation - ten slices, three corrections

Lab 04's Fisher p-values for v15 vs v14 on the generic success metric: return_request 0.0369, technical_support
0.1197, billing 0.1441, order_status 0.2645, shipping_delay 0.4771, warranty_claim 0.5364, safety_incident 0.6478,
product_inquiry 0.6922, account_access 1.0000, other 1.0000. The derived hand-off metric on return_request:
p = 0.0013.

a. Which slices does each rule flag: raw alpha 0.05, Bonferroni, Holm, Benjamini-Hochberg at q = 0.05 and 0.10?
b. With no real effect anywhere, what is the chance of at least one flag among 10 slices at 0.05? Among 10 slices
   x 5 metrics?
c. Add the hand-off p-value to the family. What changes?
d. Which correction for a release gate and which for a monitor? Write the pre-registered rule.

## 7. Design - the release gate for v16

v16 = v15 without the "hand off ambiguous returns" sentence, plus one line tying any hand-off to the
`escalate_to_human` tool. It goes to a canary on Monday. Design the gate end to end: the offline part (eval set,
paired design, metrics, thresholds, intervals), the online part (share, duration from power or a sequential test,
control, guardrails), which slices are gates and which are monitors, the zero-tolerance checks, who decides on
REVIEW, and what is recorded. Justify each choice and list the alternatives you rejected.

## 8. Design - a hill-climbing protocol for the reply prompt

The support team has two weeks to improve the reply agent's prompt. They have the 62 tickets, lab 01's eval set,
and lab 03's judge. Write the protocol: the unit of evaluation, the splits, what is scored every round, budget and
stopping rule, the keep/revert rule, what to change first, how to avoid overfitting to the judge as well as to the
tickets, and how to report the result. What do you do about a 12-ticket validation split?

## 9. Design and calculation - choose the operating point

After lab 06 the team ran a paired OFFLINE comparison on the eval set (these numbers are exercise data):

| cell | O/E vs incumbent (95% CI) | cost per success | p95 latency | paired tickets |
|---|---|---|---|---|
| `claude-sonnet-5`, medium (incumbent) | 1.00 | $0.0255 | 24 s | 480 |
| `claude-sonnet-5`, low | 0.97 (0.94-1.00) | $0.0199 | 17 s | 480 |
| `claude-opus-5`, low | 1.02 (0.99-1.05) | $0.0640 | 15 s | 480 |
| `claude-opus-5-5`, low | 1.04 (1.01-1.07) | $0.0470 | 16 s | 480 |
| `claude-haiku-4-5` (fast-path categories only) | 0.96 (0.90-1.02) | $0.0081 | 6 s | 240 |

Gates: p95 <= 30 s; O/E lower bound >= 0.95; a challenger must be at least 10% cheaper per success.

a. Apply the gates. Which cell is the operating point? What do you do with `claude-sonnet-5` at low effort?
b. The fast path's prompt is about 1,900 tokens per turn over 3 turns, with 330 output tokens per trace. Compute its
   input and output cost per trace on Claude Haiku 4.5 with and without caching, and explain which one is real.
c. Give two ways to make the fast path cache, with their cost per trace.

## 10. Hands-on - a kappa calculator (`ex10_kappa_calculator.py`)

Implement Cohen's kappa, weighted kappa (linear and quadratic) for ordinal scores, and a bootstrap interval.
Apply them to the two human annotators of the pairwise data and to the reviewer's score vs the customer's CSAT.
Check your unweighted kappa against `d6.cohens_kappa`. When does raw agreement flatter a rater, and when does
unweighted kappa under-credit one?

## 11. Hands-on - a drift monitor (`ex11_drift_monitor.py`)

Build a monitor that, every day from 2026-08-31 to 2026-09-14, compares the trailing 7 days with a reference week
on eight features, flags a feature when its PSI exceeds a noise floor measured by resampling the reference, and
reports the first day each feature drifted and whether the change log explains it. Which drifts are explained,
which are not, and what can a drift monitor never tell you?

## 12. Hands-on - contradiction detectors (`ex12_contradiction_detectors.py`)

Implement three detectors - a hand-off with no escalation, a refund promise with no refund tool, an order id that
contradicts the customer's email - count them per prompt version and arm, and decide from the counts which can be
zero-tolerance release gates. (The solution then grades them against the hidden truth.)
