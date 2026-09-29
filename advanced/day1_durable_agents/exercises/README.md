# Day 1 exercises - Durable agents

Twelve exercises: concept checks (1, 2), calculations (3, 4), design scenarios (5-8) and hands-on coding (9-12,
with starter files in this folder that run and print what is left to do). Worked answers are in
[`../solutions/README.md`](../solutions/README.md); runnable solutions are in `../solutions/*.py`. None of the
coding exercises may edit `advanced/lib/durable.py`: subclass it or wrap it, as a team that consumes a shared
runtime would.

Prices: `claude-opus-5` $5 / $25 per million input / output tokens; cache writes 1.25x (5-minute TTL), cache
reads 0.1x. `claude-fable-5-1` $10 / $50, cache reads 0.025x.

---

## 1. Concept check - read the log tail

A worker died. For each log tail below (the run's last events, oldest first), say what `DurableRunner.run()`
on another worker does next: which tool results it replays, which tools it executes, whether it calls the
model, and what - if anything - can now happen twice. Assume keyed effects where the question says so.

a. `... tool.result(get_rma)`, `model.response(turn 3, tool_use issue_refund)`
b. `... model.response(turn 3, tool_use issue_refund)`, `tool.started(issue_refund)`; the effects table has the
   refund's key in state `started`, and the refund API honours idempotency keys.
c. `... tool.started(issue_refund)`, `tool.result(issue_refund)`
d. `... tool.result(issue_refund)`, `model.response(turn 4, stop_reason end_turn)`; the run's status is `running`.
e. `... approval.requested`, `run.status(waiting_approval)`, `approval.decided(approved)`, `run.status(pending)`,
   and the queue consumer that picks up pending runs calls `run()`.
f. `run.created`, `run.status(running)` and nothing else; the worker's SDK had retried the first model call
   twice (a 529, then a timeout) before the process was killed.

## 2. Concept check - "exactly-once" claims

Each statement was made in a design review for the Service Desk Copilot. Say what is true, what is false, and
what the speaker should do instead.

a. "Our queue has exactly-once delivery, so a tool can't run twice."
b. "Step Functions Standard workflows are exactly-once, so the refund Lambda they call runs once."
c. "We send an idempotency key to the payment API: `uuid4()`, generated when we call it."
d. "The effects table makes our credits exactly-once."
e. "Temporal replays workflow code on recovery, so activities run at most once."
f. "The front door dedupes on the email's Message-ID, so a redelivered email cannot cause a second refund."

## 3. Calculation - re-run, resume or snapshot: a 40-turn run that crashed at turn 38

Kestrel's stock-audit agent checks 40 locations, one tool round per turn. Its first request carries P = 3,000
tokens (tools and system 2,600, the planner's message 400); every turn adds D = 450 tokens to the history (the
assistant turn with its tool call, and the tool result) and bills 250 output tokens (thinking included). The
worker dies right after turn 38's tool result is logged. For storage, count 4 bytes per token of JSON and 300
bytes of metadata per logged event (three events per turn).

a. The in-memory way: the whole run is re-run from scratch. Without prompt caching, what did the lost attempt
   cost, what does the re-run cost, and what is the bill against an uninterrupted run?
b. The same with automatic caching on every request (each request reads the previous request's prompt from the
   cache and writes the new part).
c. The durable way: the run is resumed from its event log. What does the resumer pay if the cache is still warm
   (resumed within 5 minutes of the last request), and if it has expired?
d. Storage for the full run: the event log, a snapshot of the messages array after every turn (system and tools
   are not in the array), and snapshots every 10 turns plus the log.
e. How many rows does a new worker read to rebuild the run at the crash, from the full log and from the latest
   of the every-10-turns snapshots plus the tail? When are snapshots worth building?

## 4. Calculation - Little's law, deploys and lease numbers

The copilot handles 1,900 tickets a month over 21 business days of 10 hours. A worker is busy with a run for 40
seconds on average; 12% of runs wait for an approval, 18 business hours on average. The team deploys three times
a business day (rolling, 30-second grace period). Leases: heartbeat every 10 s, TTL 30 s, a sweeper every 60 s.
The ERP behind `get_order` answers in more than 30 s for 0.2% of calls; 60% of runs call it; the tool does not
heartbeat.

a. With Little's law, how many runs is a worker busy with at an average instant, and how many runs are parked
   on an approval?
b. Before the runtime was durable, how many in-flight runs did the deploys kill per month? What happened to
   approvals waited on in worker memory?
c. With leases, how long after a `kill -9` is its run resumed, at best and at worst? And after a graceful SIGTERM?
d. How many false takeovers per month does the slow ERP cause, and what does each one cost - in tokens, and in
   risk once the model runs live?
e. Product asks for two things: a dead worker's run resumes within 2 minutes, and no false takeover from ERP
   calls of up to 60 seconds even though the tool does not heartbeat. Choose the TTL, the heartbeat interval
   and the sweep interval, or explain what you would change instead.

## 5. Design - an idempotency key scheme for Kestrel's tools

For every tool the copilot can call - `get_customer_profile`, `get_order`, `list_customer_orders`,
`get_invoice`, `get_rma`, `check_return_eligibility`, `check_warranty`, `create_rma`, `issue_refund`,
`escalate_to_human`, `search_knowledge_base`, lab 03's `post_credit` and lab 04's `arrange_replacement` (RMA,
stock reservation, carrier pickup, confirmation email) - decide:

* read or write, and whether a retry of it is harmless;
* the idempotency key: the runtime's `run_id:tool_use_id`, a business key (which fields?), or both;
* where the key goes downstream, and how an in-flight effect is looked up after a crash;
* the dedup window you need from the downstream system, and what you do when it has no key support at all.

Then answer: when the model calls `issue_refund` twice in one run with the same arguments (two tool_use ids),
should the second call be deduplicated? Who decides?

## 6. Design - a saga for advance warranty replacements

Strategic customers with a failed pump under warranty get an **advance replacement**: Kestrel ships the new unit
before the failed one comes back. The steps: open a warranty RMA; reserve a replacement unit; place a credit
hold for the unit's value on the customer's account; book the outbound shipment (once the carrier collects it,
it cannot be recalled for free); book the collection of the failed unit; email the customer. If the failed unit
has not arrived 30 days later, the hold is invoiced.

Design the saga: order the steps, give each its compensation (or say why it has none), identify the pivot step
after which the saga can only go forward, decide which steps the model plans and which the saga executes, and
say how the 30-day deadline is implemented durably. What must the customer be told, and when?

## 7. Design - approvals for goodwill credits

Policy: goodwill credits up to $500 are the agent's call; up to $2,500 a support manager approves; above that,
finance. Managers answer within minutes to days; finance works business hours. Design the approval flow: what
the approval record contains; who may approve (can the requester approve their own case?); what happens when a
manager wants a different amount; the SLA ladder and what the customer is told at each rung; expiry vs
rejection; how a decision made in the approvals UI reaches the run. Compare your design with (i) blocking the
worker until someone answers, (ii) a stop-and-ask turn ("a colleague will confirm"), (iii) forcing a
`request_approval` tool with `tool_choice`, and (iv) a Managed Agents session whose custom tool is answered
only after approval.

## 8. Design - where should the run state live?

Choose where the run state lives for each system, and justify it against the alternatives: SQLite, Postgres,
a workflow engine (Temporal, AWS Step Functions, Azure Durable Functions), Managed Agents sessions, or no
durability at all.

a. The Service Desk Copilot: 1,900 tickets a month, runs of seconds to minutes, approvals that wait up to five
   business days, tools that live inside Kestrel's network, three workers, one Postgres the team already runs.
b. The recall campaign orchestrator (Day 7): runs for weeks, many timers and reminders, several agents, a
   budget, an operations team that must pause, resume and audit it.
c. A nightly job that triages 2,000 warranty claims with read-only tools and writes a report nobody reads
   before 9:00.
d. A research assistant for Kestrel's engineers that works in a sandbox (code, files) for up to an hour per
   question and has no tools that change business systems.

## 9. Hands-on - `cancel(run_id)` (`ex09_cancel_run.py`)

Add a durable, idempotent cancel that works from any process: before a worker picks the run up, while it waits
for an approval (close the approval), and while a worker is inside a tool (stop at the next checkpoint, record
what already happened). A cancel must never cause the model to be called again or an approved action to run.
Decide what a cancel does NOT do, and say where that belongs.

## 10. Hands-on - an approval sweeper two hosts can run (`ex10_approval_sweeper.py`)

The starter runs lab 05's sweeper on two hosts that read before either writes, with a manager deciding in the
middle. It pages seven times where three pages are right, and it records the expiry as a refusal, so the customer
is told the refund "could not be approved" when nobody decided. Make each action at-most-once, key the page,
re-check the approval right before acting, and record expiry as expiry (`store.expire()`). What would still go
wrong if a host died between its expiry and its page, and how does your code handle it?

## 11. Hands-on - fencing a zombie worker (`ex11_fencing.py`)

Reproduce lab 06's false takeover. The runtime stops the zombie at its next heartbeat (`LeaseLost`), but by then
it has logged a second answer to the same tool call. Make that write fail: a `FencedStore` whose writes to a run
are conditional on holding its lease (checked and written in one statement). Then answer: what can fencing NOT
prevent, and what covers that gap?

## 12. Hands-on - snapshots and a tail rebuild (`ex12_snapshots.py`)

Add snapshots to the runtime for long runs: store the folded state every 10 turns in a table next to the log,
rebuild from the latest snapshot plus the events after it, and prove the result is byte-identical to the full
rebuild - at the crash and after the resumed run. Measure rows read and bytes stored, and say when you would
turn snapshots on.
