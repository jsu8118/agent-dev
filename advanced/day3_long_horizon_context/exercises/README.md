# Day 3 exercises - Long-horizon context

Twelve exercises: concept checks (1-3), calculations (4-7), design scenarios (8, 9) and hands-on coding (10-12, with
starter files in this folder that run and print what is left to do). Worked answers are in
[`../solutions/README.md`](../solutions/README.md); runnable solutions are in `../solutions/*.py`.

Prices used throughout (per million tokens, input / output): Claude Opus 5 $5 / $25, Claude Opus 5.5 $4 / $20, Claude
Fable 5.1 $10 / $50, Claude Sonnet 5 $2 / $10. Cache writes cost 1.25x the input price (5-minute TTL) or 2x (1-hour
TTL); cache reads cost 0.1x on Opus 5 and Sonnet 5, 0.05x on Opus 5.5 and 0.025x on Fable 5.1.

---

## 1. Concept check - which failure, which lever?

For each situation from Kestrel's field-agent logs, name the failure (context rot, cost curve, lost facts, budget
overrun, binding break), the primary lever, a second lever, and one thing you would *not* do.

a. At 16:00 the agent answers a KP-400 question with the KP-250's table. The context is 60,000 tokens, mostly raw
   historian exports read in the morning.
b. The monthly bill tripled after technicians started keeping one session open all day; output tokens per session are
   flat.
c. The end-of-day report leaves out the first site of the day.
d. A few turns a day take minutes; one "log it" turn produced 1,400 thinking tokens.
e. After the move to Claude Opus 5.5 with `drop_block`, `input_transformations` lists dropped thinking blocks on most
   turns.
f. Every answer after the technician left the plant floor is still three bullet points.
g. At 07:00 eight technicians' first requests each write 6,000 tokens to the cache and read none.
h. Next-visit notes dictated on Monday are gone on Tuesday.

## 2. Concept check - what will the API do?

A three-turn conversation has thinking blocks at `messages.1.content.0`, `messages.3.content.0` and
`messages.5.content.0`; `messages[2]` is a user message. For each next request, predict: a 400, a 200 with which
`input_transformations` entries, or a 200 with `[]`. Assume Claude Fable 5.1 enforces the prefix check and Claude
Opus 5.5 runs on an account created before 2026-08-31.

a. Fable 5.1, no beta header: the harness removed the `<system-reminder>` text block it had injected into `messages[2]`.
b. The same request with `thinking-binding-controls-2026-08-01` and `prefix_mismatch_behavior: "drop_block"`.
c. Opus 5.5, beta header on, field unset: the system prompt now ends with "Local time: 11:05."
d. The Opus 5.5 conversation, unchanged, sent to Claude Opus 5 with the header and `drop_block`.
e. A conversation produced on Claude Opus 5, `messages[0]` reworded, sent to Fable 5.1 with the header and `"error"`.
f. Fable 5.1: a turn-scoped reminder from turn 1 left in place (cleared); a new one appended after the latest user
   message.
g. Fable 5.1: tools reordered and `max_tokens` raised.
h. Fable 5.1, no header: keep-tail compaction - a summary message, the last turn verbatim with its thinking, the new
   user message.

## 3. Concept check - ceilings, dials and plans

a. What does the model see of `max_tokens`, of `output_config.effort`, and of a task budget?
b. What counts toward a task budget, and why is the history you re-send on every request not counted?
c. When must you pass `task_budget.remaining`? What goes wrong if you pass it in an ordinary loop?
d. What happens to a request with a task budget of 15,000 tokens?
e. Why is changing the top-level `effort` on every turn worse than a per-message effort change?
f. On which models do `thinking: {"type": "adaptive", "display": "updates"}` progress notes exist, what does Claude Opus
   5 do instead, and how should a UI treat thinking blocks under `"updates"`?
g. Lab 01's 64k-budget arm produced a one-line end-of-day report. Was the budget wrong, the harness, or neither? What
   would you change?

## 4. Calculation - lookback misses in a 40-turn session

The field agent's session on Claude Opus 5: a static prefix of P = 5,000 tokens (system + tools) with its own
breakpoint; every turn adds D = 1,500 tokens; model it as one request per turn, so turn t reads the conversation after
turn t-1, C(t-1) = P + D(t-1), and writes D. Six turns (5, 10, 15, 20, 25, 30) are look-ups with eight sequential tool
rounds, and the harness keeps its one message breakpoint on the newest technician message - so the request after each
look-up turn misses the 20-position lookback: it reads only P and writes everything after it.

a. What does turn 21 cost (input side) normally, and when it misses?
b. What do the six misses cost in total?
c. What is the session's input cost with and without the misses?
d. Name three fixes, and say which you would ship.

## 5. Calculation - deciding the compaction cadence from a budget

Same agent: P = 5,000 cached, D = 1,600 tokens per turn, 40 turns. Own summarisation every k turns: a fork reads the
current context from the cache (0.1x) and outputs a summary of S = 600 tokens ($25/MTok); the next conversation starts
from P + S, and its first request reads P and writes S + D (1.25x). A quality budget says no request may exceed
W = 30,000 tokens.

a. What is the largest k that respects W?
b. What does the day cost (input plus summary output) for k = 3, 5, 10, 15, and with no compaction?
c. Why is the cheapest k not the smallest one?
d. What else decides k in practice?

## 6. Calculation - pre-warming and keep-alive

a. Five depots of eight technicians start at 07:00; each depot's requests share a 6,000-token prefix on Claude Opus 5.
   What does a depot's morning fan-out cost cold, and pre-warmed with one `max_tokens: 0` request? What is the yearly
   difference over 250 days for all five depots?
b. On Claude Fable 5.1 a technician's conversation holds 30,000 cached tokens and then sits idle between sites for 20,
   30, 45 or 70 minutes. Compare the cost of bridging each gap with (i) the 5-minute TTL plus a `max_tokens: 0`
   keep-alive every 4.5 minutes, (ii) letting the entry expire and writing it again, (iii) the 1-hour TTL (the premium
   of writing the 30,000 tokens at 2x instead of 1.25x).
c. Which would you use, and when does the answer change?

## 7. Calculation - sizing a task budget

Lab 01 measured the day's spend per site (the 100k arm's "output + tool results" column): gbwd 19,169; harbor
18,188; riverbend 3,268; cedar 2,879; cobalt 19,037; westfield 3,079 - 65,620 tokens in all.

a. Choose a `task_budget.total` for a technician-day and justify it.
b. The harness resets the conversation at each departure (lab 02's scratchpad). What `remaining` does it pass after
   each site?
c. What would 64,000 do to this day? And 20,000?
d. Budget per day or per site visit? Argue both sides.

## 8. Design - the upgrade plan

Kestrel will move the field agent from Claude Opus 5 to Claude Opus 5.5 now, and the supervisors' route to Claude
Fable 5.1 later. Overload fallbacks go to Claude Opus 5. The harness is version 1 from the case study (clock in the
system prompt, injected-and-deleted reminders, server compaction only, `max_tokens` as the cost control). Design the
migration: what to capture and diff, the fixes in order, CI and production settings, fallback routing given model
binding, what to monitor, and the rollout. Which risks remain for customers whose accounts were created after
2026-08-31?

## 9. Design - memory for 60 technicians and 400 sites

Design the field agent's long-term memory for the whole service organisation: tiers and their keys (site, customer,
technician), write policies (who writes which tier, atomic notes, reads before writes), consolidation cadence and
conflict handling (two technicians disagree about a stop window), contamination controls (customer-supplied notes,
personal data, instructions aimed at the assistant), retention and erasure, and audit. Then choose between the memory
tool, your own store and a Managed Agents memory store for (a) the technicians' phone app and (b) a nightly Managed
Agents job that prepares next-day site briefs.

## 10. Hands-on - predict binding breaks before sending (`ex10_prefix_diff.py`)

Implement `divergence()` and `predict()` in the starter: given the previous request, the next one, the model it goes to
and which model produced each assistant turn, say for every thinking block whether the API will keep it, drop it (and
why), list it as allowed, or reject the request. The starter's bench sends thirteen cases - append-only, a reworded
turn, a trimmed tool result, a re-rendered system prompt, reordered tools, and model switches - and compares your
predictions with the API's `input_transformations` or 400. Aim for thirteen matches. Then: how would you use
`predict()` in CI, and in the request path in production?

## 11. Hands-on - a summary contract that keeps what the probes need (`ex11_summary_contract.py`)

Lab 02's own-summary and scratchpad strategies answered 7 of 8 probes: the heatsink temperature at Riverbend's latest
F05 trip lived only in a controller-log export. Without touching the harness, change the contract - the summary
instructions and the scratchpad schema (and its rendering) - so that both strategies answer all eight probes plus one of
your own, then report what the richer contract costs in summary tokens and dollars. Which facts would you *not* add, and
why?

## 12. Hands-on - a pre-warming scheduler and lookback-aware breakpoints (`ex12_cache_scheduler.py`)

Implement `schedule()` - group requests by shared prefix, pre-warm each group once with `max_tokens: 0`, wait until the
entry is readable, then fan out - and `place_breakpoints()` - mark intermediate blocks so that no request appends more
than 20 positions after the last cache entry, within the four-breakpoint limit, and refuse (with a message) when that is
impossible. The starter's bench fires twelve 07:00 requests from three depots and one request that appends thirty
checklist blocks. What do you do with a turn that appends sixty?
