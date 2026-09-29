# Day 4 exercises - Multi-agent orchestration at scale

Twelve exercises: concept checks (1, 2, 12), calculations (3, 4, 5), design scenarios (6, 7, 8) and hands-on
coding (9, 10, 11, with starter files in this folder). Worked answers are in
[`../solutions/README.md`](../solutions/README.md); runnable solutions are in `../solutions/*.py`.

Prices used throughout (list prices, as in `labkit.get_spec`): `claude-opus-5` $5 / $25 per million input / output
tokens; cache writes 1.25x (5-minute TTL), cache reads 0.1x. Managed Agents list cost adds $0.08 per session-hour of
running time. The recall data is `advanced/data/recall/*`; the labs' numbers are quoted in the lesson.

---

## 1. Concept check - who de-duplicates what?

The swarm of labs 01-02 delivers work at least once. Five layers can stop a duplicate: the queue's primary key on
`task_id`, the atomic claim (one `UPDATE ... RETURNING`), the lease and the supervisor's `reap()`, the durable run id
(`unit:<task_id>`, whose log is replayed on resume), and the effect key (`ctx.effect()`, plus the desk's idempotency
per serial). For each incident, say which layer prevents the duplicate - or that none does - and what residual risk
remains:

a. The coordinator process dies inside `dispatch_units`; the resumed process calls it again with the same 7 batches.
b. A worker dies after the desk accepted `book_slot` but before `tool.result` reached the run log.
c. Two worker threads call `claim()` in the same millisecond on a queue holding one task.
d. A worker is not dead, only slow: a 40-second pause makes its 30-second lease expire; a second worker claims the
   task and starts the same unit. Then the first worker wakes up.
e. A customer replies twice to the same email (two identical inbound messages, 3 seconds apart).

## 2. Concept check - delegation, handoff or broadcast?

For each situation, choose delegation, handoff, broadcast - or no agent-to-agent message at all - and name the
envelope type(s) of lab 04 you would send, who owns the customer conversation afterwards, and one field the typed
payload must require:

a. The coordinator must check with the quality lead whether a draft reply changes the hazard wording.
b. RPL-019: Lumen Data Centers' purchasing manager writes that all further communication goes through their counsel.
c. The last MS-100-R kit is reserved; the campaign rule says to pause scheduling for that remedy.
d. A worker needs the field-service lead's approval to send an engineer outside the unit's region.
e. RPL-003: an out-of-office auto-reply from Harbor Foods' purchasing contact.
f. The quality lead clears lot PS-2608-B after the investigation and wants the campaign to continue.

## 3. Calculation - when does splitting the work pay?

Model the recall task with these assumptions (calibrated on lab 07: the single agent's last prompt was 8,223 tokens
after 11 units):

* one agent: 3 turns per unit; every turn re-reads a prefix P = 1,800 tokens plus the history, which grows by
  h = 600 tokens per unit completed - so the history re-read during unit j (j = 0 .. n-1) is j x h;
* a swarm with one worker per unit: each worker makes 3 turns, re-reading P plus a brief of b tokens (its own history is
  negligible); each worker writes a report of r output tokens; the coordinator processes K = 8,000 tokens plus c
  tokens per unit (reading the reports or digests);
* ignore every other output token.

a. Write the tokens processed by each architecture as a function of n, and find the break-even n (the smallest n at
   which the swarm processes fewer tokens) for a *tuned* swarm (b = 100, r = 150, c = 150) and a *naive* one
   (b = 1,000, r = 600, c = 600).
b. Now price it with caching, in input-token equivalents: a token written to the prompt for the first time costs 1.25,
   every later re-read 0.1, an output token 5 ($25 vs $5). The swarm's prefix P is written once and read by every
   worker; count the coordinator's K + c·n as written once. Find both break-evens again, and the cost ratio at n = 11.
c. Lab 07 measured: one agent 186,860 tokens processed and $0.3386 as run (cached); the tuned swarm 93,764 tokens and
   $0.3548; the naive swarm $0.7267. Are those consistent with (a) and (b)? What, then, is the argument for splitting
   11 units?

## 4. Calculation - a budget for the swarm

The campaign's model-spend cap is $25.00 (`campaign.json`). In lab 03 and exercise 10 a single-unit task costs between
$0.027 (warm cache) and $0.039 (the first, cold one); plan with $0.036 as the worst cost per unit once the cache is warm.
The coordinator of a tuned swarm costs about $0.05 per campaign.

a. How many units fit under the cap, with the worst per-unit cost?
b. Eight workers run concurrently behind the naive gate (`spent < cap`, checked before a claim). What is the worst-case
   overshoot, and how does it change with 25 workers?
c. With admission control (`spent + reserved + estimate <= cap`) and an estimate of $0.045 per unit, how many units are
   admitted, how much of the cap is left unspent when dispatch stops, and why is that the right trade?
d. The hosted twin runs in one Managed Agents session with 25 concurrent threads and a $25 budget. Bound the overshoot
   if the most expensive single model request costs $0.02. What does the budget *not* cover that lab 03's guards did?

## 5. Calculation - fanning out against a cold cache

Eleven workers start at the same instant, each sending a first request whose cacheable prefix (system prompt, tool
definitions, campaign notice) is 3,000 tokens on `claude-opus-5`.

a. What does that prefix cost across the eleven first requests when they all start together (no request can read an
   entry another one is still writing)?
b. What does it cost if you pre-warm the cache with one `max_tokens: 0` request and fan out once it has completed?
c. From how many concurrent workers does pre-warming pay? How does batching per customer (7 workers) change the answer?

## 6. Design - a claim and lease scheme for three workers

Kestrel runs three worker processes on two hosts against one queue. A unit task takes 20 seconds to 20 minutes (a
worker may wait on a slow calendar API); processes get paused (GC, container migration) and hosts lose the network for
up to a minute. Delivery is at-least-once. The desk is idempotent per serial, but the calendar API behind `book_slot`
is not. Design the scheme: lease length and heartbeats, what a worker does when a heartbeat fails, how a late
("zombie") worker is prevented from committing after its lease was taken over, retry limits, error classes, dead
letters, and what the supervisor watches. Justify each choice and name the alternatives you rejected.

## 7. Design - one calendar, many planners

Lab 06's two planner threads booked from the same snapshot, and 4 of 11 plans were rejected at commit. Kestrel now
wants three planning agents (one per region group) plus human dispatchers editing the same engineer calendars during
the day. Choose how to keep the calendar consistent - pessimistic locks, optimistic concurrency on slots, a single
planner that owns the calendar, tentative holds with expiry, or commit-time arbitration with re-planning - and say
what each agent sees, what happens on a conflict, and what it costs in turns. Use lab 06's numbers.

## 8. Design - one agent, a self-hosted swarm or a hosted swarm?

Choose an architecture for each Kestrel workload and justify it with the dimensions of lesson section 8 (success per
unit of work, cost multiplication, coordination share, context size, latency, durability, what you must own):

a. Nightly triage of about 400 warranty claims: independent, one claim per item, no side effect before a human
   approves, results needed by 7 a.m.
b. The recall campaign itself: 11 units now, possibly 80 if the lot investigation widens, replies over two weeks,
   approvals that wait a day, stop conditions.
c. An engineering investigation into a new seal failure mode: read 3,000 pages of test logs, run analysis code in a
   sandbox, write a report within a few hours.

## 9. Hands-on - a dead-letter queue (`ex09_dead_letter_queue.py`)

The lab queue marks a task `dead` and keeps only its last error - fine for a lab, not for an on-call engineer. Extend
it: a `dead_letters` table with the error history and error class, transient vs permanent failures, poison detection
(the same error on every attempt), a `requeue(task_id, by, note)` for humans that keeps the history, and an `on_dead`
callback. The starter drives a flaky tool, a poison task and a worker crash through your queue and prints what is left
to do.

## 10. Hands-on - a budget-aware scheduler (`ex10_budget_aware_scheduler.py`)

Replace lab 03's single-threaded gate with a scheduler for three concurrent workers: admit tasks by priority only while
`spent + reserved + estimate <= cap`, estimate from the p90 of completed tasks' costs (a prior until you have three),
reserve before starting and settle to the actual cost afterwards, and enforce a per-worker cap. Compare the overshoot
and the number of units done with the naive gate at the same cap.

## 11. Hands-on - failure attribution from traces (`ex11_failure_attribution.py`)

The starter runs a small swarm with three planted problems - a brief that drops a unit, a scheduling outage for one
customer, and a worker whose batch mixes deadlines - and keeps every run's log. Write `attribute(serial)` that names,
for every unit that is not booked correctly, the agent, the turn, a category and the evidence, using the logs and the
dispatch record only (the answer key is for checking). Then answer: which of the three would a single end-to-end
"campaign succeeded?" metric have caught?

## 12. Concept check - what does the hosted runtime take off your hands?

a. For each concern, say whether Managed Agents handles it or it stays yours: re-running a model call after a
   transient error; the event history of a conversation; a sandbox for agent-written code; a hard dollar cap; retrying
   `book_slot` safely; deciding whether a plan may be committed; knowing that the assessor got worse after a prompt
   change.
b. Why is the custom-tool boundary (`agent.custom_tool_use` -> `user.custom_tool_result`) where your policy belongs?
c. A session budget is a pre-request gate. What does that imply for a session running 25 threads, and for a thread
   waiting on a custom tool result when the cap is reached?
d. Your client's SSE connection drops while a `agent.custom_tool_use` is pending. What happens, and what does your client
   do on reconnect?
e. Why does the platform refuse a coordinator in a coordinator's roster, and how would you build a three-level
   hierarchy (campaign -> region -> unit) anyway?
