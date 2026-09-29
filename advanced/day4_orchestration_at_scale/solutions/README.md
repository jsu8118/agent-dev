# Day 4 solutions - worked answers

Runnable solutions: `ex03_ex04_ex05_calculations.py` (exercises 3-5), `ex06_lease_simulation.py`,
`ex09_dead_letter_queue.py`, `ex10_budget_aware_scheduler.py`, `ex11_failure_attribution.py`. The numbers below are
printed by those scripts (mock mode for the ones that run agents; the calculations are exact).

---

## 1. Who de-duplicates what?

The principle: at-least-once delivery is safe only if every layer that can receive a duplicate either rejects it or
turns it into a no-op, and the layer that matters is the one closest to the side effect.

| Incident | Layer that stops the duplicate | Residual risk |
|---|---|---|
| a. dispatch_units re-run after the coordinator died | the run log first: the resumed coordinator does not regenerate turn 2, it replays the logged `model.response`, so the tool input (the 7 batches) is byte-identical; the effect is `in_flight`, the queue is its system of record, and `enqueue` returns False for every known `task_id`; done tasks are never claimed again (lab 01 step 3) | a producer outside the log - a cron job, a second coordinator - that batches differently creates different task ids for the same units. Key the de-duplication on the unit (a unit-to-task ledger, or one task per unit), not on the batch |
| b. worker died after `book_slot` succeeded, before `tool.result` was logged | the desk: the effect record says `started`, so the resumed worker calls `book_slot` again, and the desk answers "already booked for this serial (idempotent)" with the original booking | only because the desk is idempotent per serial. A downstream API that is not (exercise 6's calendar) needs the idempotency key passed down and looked up when the effect is `in_flight` - Day 1's pattern |
| c. two claims in the same millisecond | the atomic claim: one `UPDATE ... WHERE task_id = (SELECT ... LIMIT 1) RETURNING *` under `BEGIN IMMEDIATE`; SQLite serializes the writers, one gets the row, the other the next task or nothing | none at this layer. On Postgres the equivalent is `SELECT ... FOR UPDATE SKIP LOCKED` |
| d. slow worker loses its lease, a second worker takes the task, the first wakes up | the lease makes the takeover possible; `complete()` refuses the first worker (no longer the owner - lab 02 step 3); the desk's per-serial idempotency means whichever of the two books first wins and the other gets the same booking back | the zombie still spent money, and against a non-idempotent system it could commit something different. Abort on a failed heartbeat, and fence side effects with the claim's token (exercise 6) |
| e. the customer's reply arrives twice | **none of the five**: they de-duplicate work the swarm creates, not messages the world sends | de-duplicate at the edge: key the "handle reply" task on the email's Message-ID (`reply:<message-id>`), so the second copy is a duplicate enqueue. If the customer really sent it twice (two ids), the handler's effects are idempotent per unit, but a second reply to the customer is not: key outbound messages on (conversation, in_reply_to) |

The tempting wrong answer to (b) is "the durable runner replays the result" - it cannot: the result never reached the
log. Replay covers what was logged; the effect table and the downstream system cover what was not.

## 2. Delegation, handoff or broadcast?

| Situation | Pattern and envelopes | Owner afterwards | A field the payload must require |
|---|---|---|---|
| a. hazard wording check | delegation: `delegation` (kind `hazard_wording_review`) -> `result` -> the coordinator sends the `outbound` | coordinator | the draft text and its template id in the delegation; an explicit verdict in the result; `hazard_wording_changed: false` on the outbound (the lab's schema pins it) |
| b. RPL-019, counsel only | handoff: `handoff` to the account manager or legal liaison, `handoff_accept` back; plus a `broadcast` to the workers holding C-1012 units ("no contact with the site") | the account manager (a human owner is a legitimate entry in the ownership ledger) | `state.contact_restrictions` ("all communication through counsel@lumen-dc.example") and `state.open_actions` |
| c. last MS-100-R reserved | broadcast: `broadcast` (topic `remedy_pause`, scope campaign) -> `ack` from each worker | unchanged | `kit_sku` and the affected serials; the acks name what each worker paused. Enforce it in the tool as well (`reserve_kit` refuses), so an agent that missed the broadcast still cannot plan around it |
| d. visit outside the region | delegation that is an approval: `delegation` (kind `approval_request`) to the field-service lead; the worker's durable run pauses (`ApprovalRequired`, Day 1) until the `result` | unchanged | the exact action (slot, engineer, travel), its cost, and an expiry |
| e. out-of-office auto-reply | none. Classify it in code (`Auto-Submitted` header, subject), record the return date on the contact in the system of record, route to the site contact until then. At most a `notification` to the owner | unchanged | - |
| f. lot cleared | broadcast `lot_resume` to the workers, and a `handoff` of each paused customer conversation from the quality lead back to the coordinator | coordinator | `state.investigation_outcome` and `state.customer_informed` (what each customer was told) |

The pattern follows from one question: after this message, who must answer the customer? Nobody new -> delegation;
someone else -> handoff (with the state they need); nobody, but many must know -> broadcast.

## 3. When does splitting the work pay?

**a. Tokens.** One agent re-reads its history on every turn, so its tokens grow with n^2:
T1(n) = 3nP + 1.5·h·n(n-1) = 5,400n + 900n(n-1). The swarm pays a constant coordinator and a linear overhead:
Ts(n) = 3n(P + b) + K + (c + r)n.

* tuned: Ts = 6,000n + 8,000, so the swarm wins when 900n^2 - 1,500n - 8,000 >= 0, i.e. n >= 3.93: **n = 4**;
* naive: Ts = 9,600n + 8,000, so 900n^2 - 5,100n - 8,000 >= 0, n >= 6.95: **n = 7**.

At n = 11: one agent 158,400 tokens, tuned swarm 74,000, naive 113,600.

**b. Dollars with caching** (input-token equivalents: first write 1.25, re-read 0.1, output 5):
C1(n) = 1.25(P + hn) + 0.1(T1(n) - P - hn) = 90n^2 + 1,140n + 2,070, and
Cs(n) = 1.25(P + nb + K + cn) + 0.1(3n(P + b) - P - nb) + 5rn:

* tuned: Cs = 12,070 + 1,622.5n; break-even at 90n^2 - 482.5n - 10,000 >= 0, n >= 13.56: **n = 14**;
* naive: Cs = 12,070 + 5,740n; 90n^2 - 4,600n - 10,000 >= 0, n >= 53.2: **n = 54**.

At n = 11 the tuned swarm costs **1.17x** one agent and the naive one **2.95x**. Caching prices exactly the term that
made splitting pay - the single agent's quadratic re-reading - at a tenth, while the swarm's overhead is mostly
first-time writes (briefs, the coordinator) and output tokens (reports, 5x). The break-even moves from 4 to 14 units
for the tuned swarm and from 7 to 54 for the naive one.

**c. The lab agrees.** Lab 07's one agent processed 1.99x the tokens of the tuned swarm (the model says 2.14x); in
dollars, as run with caching, the tuned swarm cost 1.05x one agent (model: 1.17x, below its break-even of 14) and the
naive swarm 2.15x (model: 2.95x). So at 11 units splitting does not pay in dollars. It pays in what the dollar column
does not show: the two safety units one agent booked after their deadline (context contamination, lab 07 step 3), a
largest prompt of 3,266 tokens instead of 8,223, a modelled critical path of 79 s instead of 215 s, and per-unit runs
that can be retried, resumed and prioritized independently. If the investigation widens the recall to 80 units, the
cost argument joins them: 669,270 vs 141,870 token-equivalents, 4.7x in the swarm's favour.

## 4. A budget for the swarm

a. (25.00 - 0.05) / 0.036 = 693.1: **693 units** at the worst per-unit cost.

b. The naive gate is checked before a claim, so every worker that claimed while `spent < cap` finishes: with 8 workers
the overshoot is below 8 x $0.036 = **$0.288**; with 25 workers below **$0.90**. The overshoot grows with concurrency,
not with the size of the campaign.

c. One worker: 1 + floor((25.00 - 0.05 - 0.045) / 0.036) = **692 units**, **$0.038** left unspent (less than one
estimate). With 8 workers the in-flight reservations hold up to 8 x ($0.045 - $0.036) = $0.072 more when dispatch
stops, about 690 units; with 25 workers about 686 - and never a cent over the cap. It is the right trade because the
campaign's rule is a contract ("model spend reaches the cap: pause and report"): stranding 0.2-0.5% of the cap is
invisible, overshooting breaks the rule. Exercise 10 measures the same thing on the lab's real per-unit costs.

d. A session budget is a gate before every model request, and the request that crosses the cap completes: at most
one request per running thread, so **25 x $0.02 = $0.50**. What it does not cover: a cap per agent or thread (one
runaway thread can spend the shared budget - lab 03's per-agent guard), loops and tool error rates (the cap stops them
late; a loop detector and a breaker stop them early), wall-clock budgets, a campaign that spans several sessions (the
campaign-level cap is yours), and spend outside the session (your custom tools' downstream costs). Note also that it
caps *list* cost, not your negotiated price.

## 5. Fanning out against a cold cache

The prefix costs $0.01875 to write (3,000 x 1.25 x $5/MTok) and $0.0015 to read (0.1x).

a. Eleven concurrent first requests all write (an entry is readable only once its writer has started responding):
11 x $0.01875 = **$0.2063**.

b. Pre-warm with one `max_tokens: 0` request (a write, no output), then fan out: $0.01875 + 11 x $0.0015 = **$0.0353**,
83% less.

c. Pre-warming pays when 1.25N > 1.25 + 0.1N, i.e. N > 1.09: **from two concurrent workers** - the price is one extra
round trip before the fan-out. With 7 workers (batching per customer): $0.1313 cold vs $0.0293 pre-warmed. Batching
cuts both terms, because fewer workers means fewer first requests and fewer copies of the prefix read on every later
turn. An alternative to the explicit pre-warm is to start the first worker and release the rest a moment later, once it
has begun responding (advanced Day 3).

## 6. A claim and lease scheme for three workers

**Recommended design** (checked by `ex06_lease_simulation.py`):

* **Lease 60 s, heartbeat every 20 s.** Size the lease to the heartbeat, never to the task: a 20-minute task with a
  20-minute lease means a 20-minute outage when its worker dies. Renewing every third of the lease tolerates two missed
  beats (a slow network, a short pause). The heartbeat renews both the queue lease and the durable run's lease.
* **A failed heartbeat stops the worker before its next side effect.** The executor checks a "lease lost" flag before
  every tool call; a live-but-late worker abandons instead of racing its successor. The Day 1 runtime does this for the
  run's own lease: a failed `heartbeat()` makes `DurableRunner` raise `LeaseLost` before its next model call.
* **Fencing tokens.** Every claim increments a token (the task's version); the worker passes it with each side effect,
  and the desk, wrapping the non-idempotent calendar API, keeps the highest token seen per unit and refuses lower ones.
  In the simulation worker-A pauses for 90 s holding T1 (token 1), the supervisor re-queues T1 at t=65 s, worker-B
  books it with token 5, and when A wakes at t=100 s its booking is refused (`worker-A token 1 < 5`) and its
  `complete()` returns False.
* **Idempotency keys** per (campaign, serial, action) on every effect, passed downstream where the API supports them;
  Day 1's `in_flight` lookup where it does not.
* **Retries by error class.** Transient (timeouts, 429, 5xx, expired lease): up to 3 attempts with exponential backoff
  and jitter. Permanent (validation, unknown serial): straight to the dead letters (the simulation's T4). Poison
  detection: the same error on every attempt (exercise 9).
* **The supervisor** sweeps expired leases every 5 s and watches queue depth per status, the age of the oldest queued
  and claimed task, the reap rate (a spike means workers are dying), attempts per task, and dead-letter growth (a page).

**Rejected alternatives.** No lease (a claim is forever): a crashed worker strands its task. Long leases without
heartbeats: slow recovery, and still unsafe against a pause longer than the lease. A distributed lock (Redis) per
unit: a second system with the same failure modes - clock drift, pauses - that still needs fencing tokens to be safe.
Pessimistic database locks held for the task's duration: 20-minute transactions block the queue and deadlock.
"Exactly-once delivery": not achievable end to end across processes; at-least-once delivery plus idempotent, fenced
effects gives the same observable result.

## 7. One calendar, many planners

**Recommended:** make conflicts rare by design and impossible to commit by construction.

1. **Partition ownership:** one planner per region, and each planner books only its region's engineers - disjoint
   calendars, no overlap between agents (lab 06's conflicts were all between two planners on the same US-EAST
   calendar).
2. **Tentative holds with expiry** for the humans: a planner places a hold (visible to dispatchers), confirms it at
   commit, and an unconfirmed hold expires - a lease on a slot, so a crashed agent cannot block the calendar.
3. **Commit-time arbitration always:** the desk checks the slot's version/status at commit (lab 06's `record_plan`
   refused 4 of 11 plans); the loser re-plans from fresh data.

**Why not the others.** Pessimistic locks held across model turns (seconds to minutes each) block the dispatchers and
deadlock across agents. A single planner that owns the calendar is conflict-free among agents and simple - it is the
`--planners 1` fix of lab 06 - but it serializes planning and does nothing about the humans. Optimistic concurrency
alone is correct but, with overlapping planners, pays for every collision: in lab 06 two planners on one snapshot lost
36% of their plans (4 of 11) at the first commit, and the run needed 22 model requests and $0.7450 against 16 requests
and $0.4396 with one planner (`--planners 1`, lab 07's last row) - six more requests (the second planner, a snapshot
refresh, a re-plan turn in the first planner's thread, a second commit), 1.7x the cost, and a modelled critical path
of 236 s instead of 154 s. The parallelism bought nothing: planning is cheap, the shared resource is the bottleneck.

## 8. One agent, a self-hosted swarm or a hosted swarm?

a. **400 warranty claims a night: no coordinator agent at all.** The items are independent, there is no shared state
and no side effect before a human approves: the "coordination" is a loop in code. Run one small agent (or a single call)
per claim through the Message Batches API (half price; a night is plenty of time) or a plain queue with workers for the
7 a.m. deadline. A coordinator would add tokens and nothing it could decide.

b. **The recall campaign: the self-hosted durable swarm** (labs 01-03; the capstone builds it out). It spans two weeks,
waits a day for approvals, must stop on conditions, prioritize by risk, never double-book, and stay inside a campaign
budget across days - a queue, leases, dead letters, effects and approvals you own. At 80 units exercise 3 says the
swarm is also the cheaper architecture. Hosted sessions can still do bounded pieces of it (an investigation per
customer), but the spine stays yours.

c. **The engineering investigation: a hosted swarm.** It needs a sandbox to run analysis code (building and hardening
one yourself is Day 5's subject), 3,000 pages mounted as files (too much for one context: reader threads on a cheaper
model, each with its own window, the lead keeping only their reports), a few hours of work and a hard cost cap. It has
no multi-day durability requirement and no side effects on customer systems.

## 9. A dead-letter queue

`ex09_dead_letter_queue.py` adds a `task_errors` history and a `dead_letters` table to the lab queue. What the
on-call engineer gets, for the same traffic as the starter:

```
  unit:KP250-2608-0099: 1 attempt(s), class permanent, poison=False
    attempt 1: [permanent] not found: KP250-2608-0099
  unit:KP100-2608-0002: 3 attempt(s), class transient, poison=True
    attempt 1: [transient] scheduling service timeout
    attempt 2: [transient] scheduling service timeout
    attempt 3: [transient] scheduling service timeout
```

Design points: the error *class* decides whether to retry (a permanent failure is dead-lettered at once, attempt 1);
the *poison* flag (the same error on every attempt) says that retrying without a fix only burns attempts; the history
survives a `requeue`, which records who and why and resets the attempts; the `on_dead` callback pages someone; and a
late failure report from a worker that lost its claim is ignored (`fail_classified` checks the owner - fencing again).
Two things not to do: requeue dead letters automatically on a timer (poison tasks then loop forever at a slower pace),
and delete them (the history is the diagnosis).

## 10. A budget-aware scheduler

`ex10_budget_aware_scheduler.py` measures each unit's real cost once ($0.0311 - $0.0393 in mock mode) and replays the
swarm on a simulated clock, so the comparison is exact:

```
  naive gate, 3 worker(s)     9 units done, $0.2903 spent (over by $0.0903), 2 left queued
  budget-aware, 3 worker(s)   5 units done, $0.1645 spent (within the cap), 6 left queued
    first denial: w1: $0.1017 spent + $0.0800 reserved + $0.0393 > cap
```

The naive gate's overshoot grows with the number of workers ($0.0278 with one, $0.0903 with three, $0.1530 with
eight); the budget-aware scheduler stays within the cap for every worker count because in-flight work is counted as
reserved. Why p90 rather than the mean or the max: the mean under-reserves for half the tasks (overshoot again), the max
of a heavy-tailed distribution strands too much; p90 of observed costs, with a prior until there are samples, is a
bound that learns. The first unit here is the most expensive one (it writes the cache), which is why the scheduler is
conservative early. A per-worker cap ($0.08) shows up as a different stopping reason (`w2: its own $0.0624 + $0.0393 >
per-worker cap`). What the solution leaves for production: releasing a crashed worker's reservation when its lease
expires, and per-kind estimates when tasks differ (a batch of three units is not one unit).

## 11. Failure attribution from traces

`ex11_failure_attribution.py` walks back from each unit's outcome, one question per hop - did the unit reach a
worker, did the worker's tools work for it, did the booking come from a tool result that belongs to it:

```
  KP100-2608-0001 (pending_schedule)
    -> tool outage [right] at unit:batch:C-1025, turn 2
       evidence: all 1 find_engineer_slots call(s) for EU/seal_replacement failed: EU scheduling
       service unavailable (HTTP 503)
  KP250-2608-0007 (visit 2026-09-24 after remedy_by 2026-09-23)
    -> context contamination [right] at unit:batch:us-east, turn 26
       evidence: book_slot(FSE-01-2026-09-24-13) took a slot from the turn-17 search with
       not_after=2026-09-30, made for another unit; this unit's remedy_by is 2026-09-23
  KP250-2608-0008 (no plan produced)
    -> dropped at dispatch [right] at coordinator, turn None
       evidence: no task in the dispatch record lists KP250-2608-0008; batch:us-east carried 9
       serials
  3/3 attributed correctly
```

The question's answer: an end-to-end "did the campaign succeed?" check that reads the coordinator's summary catches
**none** of them. The dropped unit is in no plan, so the summary never mentions it; the outage surfaces as an honest
`pending_schedule` ("needs a human decision" - true, and silent about the cause); the contaminated booking is reported
as `scheduled`. Only a per-unit check against the campaign's list (every unit, not every plan returned) finds all
three, and only the traces say what to fix: the coordinator's batching, the EU scheduling dependency, the worker's
context. This is why lab 07 scores units, not runs.

## 12. What does the hosted runtime take off your hands?

a.

| Concern | Who |
|---|---|
| re-running a model call after a transient error | the platform (the session goes `rescheduling` and retries) |
| the event history of a conversation | the platform (events per session and per thread) |
| a sandbox for agent-written code | the platform (a container per session; self-hosted sandboxes if you need your own) |
| a hard dollar cap | the platform for one session (budget); per-agent and campaign-wide caps stay yours |
| retrying `book_slot` safely | **yours**: it is a custom tool; the platform may re-run a turn, so keys and fencing are your job |
| deciding whether a plan may be committed | **yours**: validation in the custom tool (lab 06's `record_plan`), approvals |
| knowing the assessor got worse after a prompt change | **yours**: evals, versions pinned per session, release gates (Day 6); outcomes can grade one session against a rubric, not your fleet over time |

b. The custom tool is the one place where an agent's intent enters your systems: it arrives as data (a name and an
input), is executed by your code with your credentials under your policy, can be validated, keyed, fenced and logged,
and comes back as a result the agent reads. Everything the agent does inside its sandbox is contained there;
everything that touches Kestrel passes through your executor. A rule in a prompt can be argued with; a rule in the
executor cannot.

c. One budget is shared by all 25 threads. The gate runs before each request and each thread's crossing request
completes, so the overshoot is at most one request per running thread; threads pause independently. A thread waiting
on a custom tool result outranks the cap at the session level - the session reports `requires_action`, and your
`user.custom_tool_result` (a settle event) is accepted even at the cap; the thread then makes no new model request until
the budget is changed or removed.

d. Nothing happens: the SSE stream has no replay, and the session sits idle with `requires_action`, waiting for a
result that your disconnected client will never send - a deadlock from the client's point of view. On every
(re)connect: open the stream, fetch the history with `events.list`, de-duplicate by event id, find the latest
`session.status_idle` with `requires_action` and answer its `event_ids`. `drive_session` in `labs/_day4.py` does this,
and lab 05 shows why it matters even without a drop: SDK 1.8's typed stream does not yield `session.usage` events, the
history does.

e. One level keeps the hosted orchestration bounded: no runaway recursion, one coordinator whose stream is the
session's primary thread, predictable thread limits (25 concurrent) and one budget. A three-level hierarchy is yours to
build: the campaign level is your durable coordinator (lab 01), each region is a hosted session whose lead has a roster
of unit workers (lab 06), and the handoff between them is your queue and your custom tools.
