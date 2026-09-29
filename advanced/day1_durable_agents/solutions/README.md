# Day 1 solutions - worked answers

Runnable solutions: `ex03_ex04_durability_math.py` (exercises 3-4), `ex09_cancel_run.py`,
`ex10_approval_sweeper.py`, `ex11_fencing.py`, `ex12_snapshots.py`. The coding solutions import their starter's
harness, so the scenario you ran against your own code is the one the solution passes. Numbers below come from
those scripts in mock mode; the arithmetic ones are exact, the run-based ones are the mock's deterministic
stand-in behaviour.

---

## 1. Read the log tail

The rule behind every answer: `rebuild()` turns the log into the messages array; every tool call with a logged
result is **replayed**, every tool call without one is **executed**, and the model is called for every turn the
log does not have yet.

| tail | what the resumer does | what can happen twice |
|---|---|---|
| a. `model.response(turn 3, issue_refund)` last | replays turns 1-2's results (already in the rebuilt messages), executes `issue_refund` (it never started), calls the model for turn 4 | nothing: the refund never started before |
| b. `tool.started(issue_refund)` last, effect `started` | executes the tool again; inside it `ctx.effect()` reports **in flight**, so it asks the refund API by key: found -> commits that result; not found -> issues it now, same key; then turn 4 | nothing - *because* the key went downstream and the API honours it within its window. Without the key: a second refund (lab 03, step 1) |
| c. `tool.result(issue_refund)` last | replays all three results, executes nothing, calls the model for turn 4 | nothing |
| d. `model.response(turn 4, end_turn)` last, status `running` | the answer is already in the log: the last logged turn has no tool call, so `run()` marks the run completed with the logged reply - **0 model calls** (lab 02, the last row of step 2). Calling the model instead would send a conversation that ends with the assistant's answer: a prefill, which current models reject with a 400 | the customer email, if sending it is a separate step without a key; put it in an outbox keyed by the run id |
| e. `approval.decided(approved)`, `run.status(pending)`, consumer calls `run()` | before the loop, `run()` answers the settled approval: `issue_refund` executes once with `_approved: True`, as an effect keyed `<run>:<tool_use>:approved`, and its result is logged; the model then writes the reply (lab 05, step 3) | nothing: a second `run()` finds the result in the log, and the key covers a crash inside the refund. (A runtime that did not answer decisions itself re-executed the gated call *without* the approval: the gate asked again and the manager's decision was ignored - lab 05 shows that too) |
| f. only `run.created`, `run.status(running)` | rebuilds `[user message]` and calls the model for turn 1 | the model call: the 529 was an overload refusal, but the request that timed out on the client may still have been processed - and billed - by the server (a timed-out request is not a cancelled one), and the resumer generates turn 1 again. Model calls are at-least-once; harmless except for cost, because no tool ran |

The pattern: tool execution is at-most-once *from the log's point of view* (a) - (c). Anything the log cannot see
needs its own guard: a downstream system that received the call (b), a step outside the loop (d), a decision made
outside the run, which `run()` must answer before the loop continues (e), a response that never reached the log (f).

## 2. "Exactly-once" claims

a. **False as stated.** Brokers that advertise exactly-once mean it inside their own boundary (transactional
   consume-produce, or deduplicating sends within a window). A consumer that calls a tool and dies before
   acknowledging gets the message again, and the tool runs again. Treat delivery as at-least-once and make the
   handler idempotent: effects table plus a key the downstream system honours.
b. **Half true.** Standard workflows record each state transition once in the execution history, but a Task
   state is retried by its `Retry` policy, and a Lambda that timed out after doing its work runs again. The
   refund Lambda must be idempotent (key it on the execution and state, or on the business key). The engine
   guarantees its own bookkeeping, not your side effects.
c. **Not an idempotency key.** A `uuid4()` minted at call time is new on every attempt, so the API sees each
   retry as a new request - the exact failure the key exists to prevent. Derive the key from something that
   survives the crash and is written down before the call: `run_id:tool_use_id` from the log, or a business key.
d. **Partly.** The effects table stops the *resumer* from repeating a done effect and flags an in-flight one.
   It cannot tell whether an in-flight call reached the ledger, and it does nothing for a transport retry inside
   one attempt - lab 03's matrix shows `1 credit + reconcile` after a crash but `2 credits` under a retry. Exactly-
   once *effects* are at-least-once attempts, a local record, and an idempotent receiver.
e. **False.** Replay re-runs *workflow* code against the recorded history; completed activities are not re-run
   during replay. But activities are at-least-once: one that timed out (its worker died after doing the work) is
   retried by policy. Idempotent activities are still your job.
f. **It protects against the wrong thing.** Deduping at the front door stops a second *run* for the same email.
   It does not protect effects inside a run that is retried or resumed (the case study's retry script
   double-credited a customer this way), nor two different emails asking for the same refund. Worse, with an
   in-memory loop it turns a lost run into a silent drop: the redelivery is discarded as a duplicate. Create the
   run idempotently from the Message-ID (`run_id` derived from it), **resume** it on redelivery, and key the
   effects inside.

## 3. Re-run, resume or snapshot: a 40-turn run that crashed at turn 38

Turn *t* sends P + (t-1)D = 3,000 + 450(t-1) input tokens. From `ex03_ex04_durability_math.py`:

```
--- Step 3a: Re-run from scratch, no caching -----------------------------------------------------
  lost attempt (turns 1-38): 430,350 input tokens + 9,500 output = $2.3893
  full re-run (turns 1-40):     471,000 input tokens + 10,000 output = $2.6050
  bill with the crash: $4.9943 against $2.6050 uninterrupted (+92%)
```

a. Lost attempt: 38 x 3,000 + 450 x (37 x 38 / 2) = 114,000 + 316,350 = 430,350 input tokens ($2.1518) plus
   9,500 output tokens ($0.2375) = **$2.3893**. Re-run: 40 x 3,000 + 450 x (39 x 40 / 2) = 471,000 tokens
   ($2.3550) + $0.25 output = **$2.6050**. The crash nearly doubles the bill (+92%).
b. With automatic caching every request reads the previous prompt at 0.1x and writes the new part at 1.25x. The
   uninterrupted run reads 39 x 3,000 + 450 x (38 x 39 / 2) = 450,450 tokens ($0.2252), writes 3,000 + 39 x 450
   = 20,550 tokens ($0.1284) and outputs $0.25: **$0.6037**. The lost attempt cost **$0.5657**, so the crash
   costs **$1.1693** in all (+94%): caching makes both runs cheaper, not the crash. The re-run cannot reuse the
   lost attempt's cache beyond the first 3,000 tokens, because its responses differ from the first attempt's.
c. Resuming generates only turns 39 and 40. Warm: turn 39 reads 19,650 tokens ($0.0098), writes 450 ($0.0028),
   outputs 250 ($0.0063); turn 40 the same one turn later - **$0.0380, 6% of the uninterrupted cached run**.
   Cold: turn 39 writes all 20,100 tokens at 1.25x ($0.1256) - **$0.1510, 25%**. Rebuilding the transcript from
   the log costs no tokens at all.

```
--- Step 3d: Storage: the log against snapshots of the messages array ----------------------------
  event log:                           109,900 bytes (grows linearly: 2,700 per turn)
  a snapshot after every turn:       1,540,000 bytes (14x the log; grows with T^2)
  snapshots every 10 turns + log:      296,300 bytes
```

d. Log: 1,600 + 300 bytes for the planner's message, then 450 x 4 + 3 x 300 = 2,700 bytes per turn: 109,900
   bytes. A snapshot after turn *t* holds (400 + 450t) x 4 bytes, so snapshots after every turn total
   40 x 1,600 + 1,800 x (1 + ... + 40) = 64,000 + 1,476,000 = **1,540,000 bytes** - 14 times the log, and the
   ratio grows with the number of turns. Four snapshots plus the log: 296,300 bytes. Exercise 12 measured the same
   shape on the real run: four snapshots of 83,018 bytes next to a log of 54,501.
e. Full log: 2 + 38 x 3 = **116 events**. Latest snapshot (taken after turn 30's response) plus the tail: 1 row +
   turn 30's tool pair + 8 x 3 events = **27 rows** - exercise 12 measures exactly these numbers. Both take
   milliseconds against seconds for one model call. Snapshots earn their keep when rebuilds read thousands of
   events (a coordinator that runs for weeks) or when events are large; for agent runs of tens of turns, the log
   alone is the right design - and the log must stay complete either way, because replay, forks and audits need it.

## 4. Little's law, deploys and lease numbers

```
--- Step 4a: Little's law: L = lambda x W --------------------------------------------------------
  arrivals: 1,900 / 210 business hours = 9.05 runs per hour
  runs a worker is busy with at any instant: 9.05/3600 x 40 s = 0.101
  runs parked on an approval at any instant: 9.05 x 12% x 18 h = 19.5
```

a. **0.1 runs** are being worked on at an average instant, but **19.5 runs** are parked on approvals. The
   durable part of the system is the waiting, not the working.
b. 63 deploys x 0.101 = **6.3 in-flight runs killed per month** (a given deploy hits at least one 10% of the
   time - rare enough that nobody noticed). Approvals were far worse: with a deploy every 3.3 business hours and
   waits of 18 hours, the chance that a wait sees no deploy is e^(-18/3.33) = 0.5% (treating deploys as random
   arrivals). **Practically all 228 approvals a month** were lost whenever they were held in worker memory.
c. After a `kill -9` the lease runs out 30 s after the last heartbeat and the sweeper sees it within the next
   60 s: **30 to 90 s**. A graceful SIGTERM (stop taking work, finish or abandon the current step, release the
   lease) lets the next poll take the run at once.
d. 1,900 x 60% x 0.2% = **2.3 false takeovers per month**. Each costs little in tokens: the new owner runs the
   in-flight `get_order` again (a read), and the zombie logs a duplicate result when its slow call returns, then
   stops with `LeaseLost` at its next heartbeat - before it calls the model again (lab 06, step 2). The risk is
   the rest of the zombie's tool round, which runs before that heartbeat: it answers the same tool_use ids as the
   new owner, so keyed writes collide on their key, but an unkeyed write happens twice. Key every write and fence
   the store (exercise 11). A runtime that ignores the heartbeat's answer is far worse: the zombie runs on, two
   workers pay for the same turns, and - live - their turns carry different tool_use ids, so every later write
   gets a different key and can happen twice.
e. The requirements force TTL > 60 s (no takeover during a 60 s step without heartbeats) and TTL + sweep <=
   120 s: **TTL 75 s, sweep every 30 s, heartbeat every 25 s** gives 75-105 s. The better answer changes the
   premise: heartbeat from inside the slow tool (`ctx.store.heartbeat(...)` every 10 s), keep TTL 30 s, and
   fence writes (exercise 11) for the tail latencies nobody measured.

## 5. An idempotency key scheme for Kestrel's tools

| tool | kind | key | downstream / in-flight lookup | window, fallback |
|---|---|---|---|---|
| `get_customer_profile`, `get_order`, `list_customer_orders`, `get_invoice`, `get_rma`, `check_return_eligibility`, `check_warranty`, `search_knowledge_base` | read | none | re-executing is harmless; the runtime *replays* the logged result anyway, so a resumed run sees what the crashed one saw | a read re-executed later may return something newer (drift, lab 07): never silently mix old and new answers in one run |
| `create_rma` | write | business key (order, SKU, reason, open) + `run:tool_use` in the effects table | the tool already returns the open RMA for the same line and reason; in flight -> query `rmas` by the business key | as long as the RMA is open; a *new* RMA after the old one closed is legitimate |
| `issue_refund` | money | **business key: one refund per RMA** (`RF-<rma>`), not the tool_use id | the refunds table's primary key; the payment gateway's `Idempotency-Key` = the refund id; in flight -> look the refund up by RMA | forever (the refund record persists); two different runs refunding the same RMA must collide |
| `escalate_to_human` | write (a ticket, a page) | `run_id` + queue | store the key on the escalation (the current table has no column for it: add one) and look it up in flight | the ticket's life; a second escalation to a *different* queue is a different effect |
| `post_credit` (lab 03) | money | the manager's approval id (one approval -> one credit), pushed as `Idempotency-Key`; `run:tool_use` locally | ledger lookup by key; business-key fallback (order + amount within 30 days) | the ledger forgets keys after 1 day: the fallback covers late resumes |
| `arrange_replacement` (lab 04) | saga | `run:tool_use:step` per step | reservation reference, carrier booking reference, RMA business key; the email's Message-ID derived from the key | email cannot be recalled or deduplicated reliably: send it through an **outbox** row written with the saga state, delivered once |

When a downstream system has no key support at all: look up by a business key before acting (racy, but a lease
keeps one worker per run), put a proxy or an outbox you control in front of it, reconcile daily (your effects
table against their records), and fail closed on in-flight effects of money (lab 03, step 2).

**Two `issue_refund` calls with the same arguments in one run** carry two tool_use ids: to the runtime they are two
decisions, and it will not merge them - that is correct, because the runtime cannot know the business rule. The
rule "one refund per RMA" belongs to the tool's owner and lives in the tool (the business key above). The runtime
guarantees at-most-once per decision; the tool guarantees the business invariant across decisions.

## 6. A saga for advance warranty replacements

Recommended order - compensable steps first, the pivot, then retriable steps (the classification comes from the
original sagas paper and its later practice: compensatable, pivot, retriable):

| # | step | compensation | class |
|---|---|---|---|
| 1 | open the warranty RMA (status `pending_replacement` - a semantic lock: a second run for the same unit sees it and stops) | withdraw it (`rejected`, with a note) | compensatable |
| 2 | credit hold for the unit's value | release the hold | compensatable |
| 3 | reserve the replacement unit | release the reservation | compensatable |
| 4 | book the collection of the failed unit | cancel the booking | compensatable |
| 5 | **book the outbound shipment** | none once collected - bringing the unit back is a new forward process | **pivot** |
| 6 | email the customer (outbox, keyed by the saga id) | none needed | retriable |
| 7 | 30 days later: unit received? release the hold : invoice it (keyed) | - | retriable, timed |

* **Model vs saga.** The model does the judgement: warranty check, eligibility, the customer's tier, the
  decision to offer an advance replacement. Then it calls *one* tool, `start_advance_replacement(...)`, and the
  saga executes the steps deterministically. If the model orchestrated the six steps itself, a crash between
  steps would leave a half-done world for a probabilistic planner to reason about.
* **The 30-day deadline is a durable timer**, not a sleeping thread: a row with a due time that a sweeper fires
  (the same machinery as lab 05's SLA sweeper), or the engine's timer (Temporal timers, a Step Functions Wait
  state, a Durable Functions `CreateTimer`). When it fires, it checks the RMA status and invoices or releases -
  both keyed by the saga id.
* **Rollback is durable** (lab 04, step 5): the decision to compensate is logged before the first
  compensation, and a resumed saga reads it first.
* **What the customer is told:** nothing definite before the pivot succeeded - at most "we are arranging a
  replacement". After it: the tracking number, the collection window, the 30-day rule and what happens after
  it. If a step before the pivot fails: compensations run, and the customer hears that a person will call
  (backed by an escalation record).

Rejected: two-phase commit (the ERP, the carrier and the billing system offer no prepare/commit); "just retry"
(the credit hold and the reservation are not idempotent by themselves, and there is no path backwards).

## 7. Approvals for goodwill credits

* **The record:** approval id; run, ticket and customer; the action exactly as it will run (tool, amount,
  order, reason) with the evidence the agent used; the amount tier and the role it needs; who requested and when;
  the SLA deadlines; status (`pending`, `approved`, `rejected`, `expired`, `cancelled`); who decided, when, and
  the note. The credit it unlocks is keyed by the approval id.
* **Who may approve:** a role, checked in code (support manager up to $2,500, finance above), authenticated in
  the approvals UI - never an email reply, which can be spoofed (Day 5). Dual control: the person whose case it is
  cannot approve their own request.
* **A different amount** is a new decision, not an edit: close the original with a note and approve a new request
  for the new amount (one click in the UI can do both). The run resumes with the approved amount, the model tells
  the customer that amount, and the audit shows both.
* **SLA ladder** (business hours): at 4 h page the approver's backup (keyed, once); at 1 business day send the
  customer a holding reply (once); at 3 business days *expire* (`store.expire()`) - the run resumes with a "not
  decided in time" tool error, the model escalates and tells the customer it is overdue, not refused (lab 05,
  step 5; exercise 10).
* **How the decision reaches the run:** the UI calls `decide()` (a conditional update: the first decision wins),
  the run returns to `pending`, the next worker's `run()` answers the decision before the loop continues, and the
  approved action runs as a keyed effect.

| option | verdict |
|---|---|
| (i) block the worker until someone answers | a thread held for hours or days, lost at every deploy (exercise 4: practically every wait); no audit trail |
| (ii) stop-and-ask ("a colleague will confirm") | ends the run; the follow-up starts from nothing; acceptable only when a person takes the whole case over - and the promise needs a record (an escalation) |
| (iii) force a `request_approval` tool with `tool_choice` | shapes what the model does next; parks nothing - the waiting still has to live somewhere - and a forced tool is a 400 on Claude Opus 5.5 and Claude Fable 5.1. The gate belongs in the executor (policy in code) |
| (iv) Managed Agents custom tool answered after approval | the session idles with `requires_action` and waits server-side as long as needed, so the waiting is hosted; the approval record, roles, SLA ladder and the keyed credit are still yours. For hosted tools, `always_ask` + `user.tool_confirmation` is a built-in gate without business semantics (roles, amounts) |
| **durable approval (this day)** | the run parks as a row; any process decides; any worker resumes; the approved action is keyed |

## 8. Where should the run state live?

a. **Copilot: Postgres**, the database the team already runs - event log, effects, approvals and leases as
   tables; `SELECT ... FOR UPDATE SKIP LOCKED` for the queue; payloads the model will see again stored as exact
   text (`text` or `json`, never `jsonb`, which re-orders keys - lab 02). Rejected: SQLite (three workers on
   separate hosts; SQLite over a network filesystem is unsafe); a workflow engine (a new system to operate for one
   workflow, and an agent loop maps awkwardly onto deterministic workflow code: every model call becomes an
   activity, transcripts exceed payload limits and move to external storage); Managed Agents (the tools live
   inside Kestrel's network and would round-trip through custom tools anyway; revisit if the sandbox becomes
   valuable).
b. **Recall orchestrator: a workflow engine is a strong fit** - timers, signals, pause/resume, and history
   views are what engines are built for, and the campaign has many of them over weeks. Each agent run can stay
   an event-sourced run in the database, started by an activity and awaited by id. The Day 7 reference shows the
   other defensible answer: the durable log plus a timer table and a coordinator in the same database, when the
   team would rather not run an engine.
c. **Nightly triage: no event log.** Read-only tools, nothing lost by redoing a claim but tokens: make the job
   restartable per claim (a results table keyed by claim id; skip done ones), and use the Message Batches API
   (half price, results within 24 hours) if one call per claim suffices.
d. **Research assistant: Managed Agents sessions.** Anthropic runs the loop and the sandbox, the session keeps
   its event history, and there are no business side effects to key. Yours: a question -> session table (the
   session create call takes no idempotency key), reconnecting to the stream with a history fetch and dedupe by
   event id, and a session budget.

## 9. `cancel(run_id)`

`solutions/ex09_cancel_run.py` passes the starter's four scenarios:

```
--- Step 3: c. cancelled while a worker is inside a slow tool ------------------------------------
  cancel() returned              running
  worker outcome                 cancelled
  tools run                      ['get_customer_profile', 'get_rma']
  refunds                        0
  check: OK
```

and prints the log of scenario c - the request appended by one process, honoured by the worker at its next
checkpoint:

```
     7  tool.started        get_rma {"rma_id": "RMA-7004"}
     8  run.cancel_requested {"by": "ops.lead", "reason": "customer asked to hold the refund"}
     9  tool.result         get_rma ok 284 chars: {"rma_id": "RMA-7004", "order_id": "SO-10263", "sku": "FL...
    10  run.cancelled       {"by": "ops.lead", "reason": "customer asked to hold the refund", "completed_...
    11  run.status          cancelled  error=cancelled by ops.lead: customer asked to hold the refund
```

* **An event first, a status second.** Any process may append `run.cancel_requested`; only the lease holder
  changes the status. `cancel()` takes the lease itself when nobody holds it (a pending run, a parked run, a dead
  worker's run) and finishes the cancel at once; otherwise it returns `running` and the worker stops at its next
  checkpoint.
* **Checkpoints without editing the runtime:** a wrapped client checks before every model call (no tokens are
  spent after a cancel), and the runner's own hook points (`_maybe_crash` at after_model, before_tool,
  after_tool) check around every tool. A tool already running finishes; the log records its result.
* **Parked runs:** pending approvals are closed as rejected, with the cancel in the note (the public API settles
  an approval as approved, rejected or expired). The runner also checks before `run()` answers a settled
  approval (`_answer_decided`, where an approved action executes), so a cancelled run never executes one.
* **What cancel does not do:** undo. `run.cancelled` lists the tools that completed; compensating them is a saga's
  job (lab 04) or a person's. Cancelling twice, or cancelling a finished run, changes nothing (scenario d: one
  request logged, a completed run stays completed). A cancel that arrives after the run's last checkpoint loses the
  race - the run completes, and the caller must read the status it gets back.

## 10. An approval sweeper two hosts can run

`solutions/ex10_approval_sweeper.py`:

```
  +4 h  host-1: escalate apr-A -> PG-001
  +4 h  host-1: escalate apr-B -> PG-002
  +4 h  host-2: escalate apr-A -> skipped: another host already did it
  +4 h  host-2: escalate apr-B -> skipped: another host already did it
  +2 d  host-1: expire apr-A -> PG-003
  +2 d  host-1: expire apr-B -> skipped: decided by ops.manager since the plan
  +2 d  host-2: expire apr-A -> skipped: another host already did it
```

Three pages instead of the naive sweeper's seven, apr-B keeps the manager's late approval (and is refunded), and
apr-A's customer is told it is overdue with an escalation reference; `5/5 checks passed` with the dead-host case
below.

* **Claim, then act.** One effects-table row per (approval, action). `RunStore.effect_begin()` is an `INSERT OR
  IGNORE`, so two hosts racing for the same key get exactly one first attempt; the other finds the claim, done or
  in flight.
* **Re-check right before acting, and read back after.** A plan is a snapshot of the past. The manager approved
  apr-B after host-1 planned its expiry; the re-check sees a decided approval and leaves it alone. `store.expire()`
  settles only a pending approval (a conditional update), so even a click that lands between the re-check and the
  expiry wins; the solution reads the approval back and pages only if it is now `expired`.
* **Key the side effect, and finish what a dead host started.** The page carries the claim's key, and the event
  that records it is written after the page went out. A host that dies between its expiry and its page leaves an
  expired approval nobody was told about - and `plan()` only lists *pending* approvals, so the next sweep would
  never see it. `plan_unfinished()` finds those (status `expired`, no `approval.expiry_paged` event);
  `apply_safely()` meets the claim in flight and pages with the same key, so the pager deduplicates if the first
  page did go out. Step 3 of the solution:

  ```
    host-1: host-1 died after expiring apr-C, before its page
    pending approvals the next plan() sees: 0; unfinished expiries: 1
    host-2: expire apr-C -> PG-001 (finished a dead host's claim)
  ```
* **Expiry is not a refusal.** `store.expire()` gives the approval its own status, and the run's tool result
  says "Not decided in time", so reports and the model can tell "nobody decided" from "a manager said no". The
  naive sweeper's `decide(approved=False)` made the model tell apr-A's customer the refund "could not be approved
  at this time (no decision within 2 days)" - a refusal nobody gave, and no follow-up.

## 11. Fencing a zombie worker

`solutions/ex11_fencing.py`:

```
--- Step 2: Fenced: the same takeover ------------------------------------------------------------
  worker-b           status=completed turns=3 replayed_tools=1 executed_tools=1
  worker-a           stopped: LeaseLost: worker-a may not write tool.result to fenced-takeover: the lease is free
  log                model.response=3, tool.started=3, tool.result=2
  get_order results  1
  final status       completed
```

Unfenced (step 1), the runtime's own check stops the zombie at its next heartbeat - `stopped: LeaseLost: run
fenced-takeover: lease taken over by another worker; stopping` - but only after it logged a second result for the
same `get_order` (`get_order results 2`). The fence is one SQL statement per write - `INSERT ... SELECT ... WHERE
EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND lease_owner = ?)` - so no other worker can take the lease between
the check and the write, and the zombie's first write after the takeover is refused; `set_status` is fenced the
same way. `tool.started=3` is expected: worker-a started the slow call legitimately, and worker-b started it
again after the takeover.

**What fencing cannot prevent:** anything the zombie did *outside* the store while it still believed it held the
lease - here a repeated read, in general a write to another system. That gap is covered by idempotency keys (the
zombie and the new owner answer the same tool_use, so they carry the same key) and, for downstream systems you
control, by accepting a fencing token (a lease version that only increases) and rejecting writes with an older
one. Worker identities must be unique per process incarnation (host, pid and a random suffix), or a restarted
worker with the same name would pass the fence.

## 12. Snapshots and a tail rebuild

`solutions/ex12_snapshots.py`:

```
--- Step 2: Snapshots every 10 turns: the latest snapshot plus the tail --------------------------
  events at the crash                        116
  rows read by rebuild()                     27
  resumed                                    status=completed turns=41 replayed_tools=38 executed_tools=2
  resumed request extends the crashed one    True
  snapshots stored: 4 (83,018 bytes) next to a log of 54,501 bytes
```

and both byte-identity checks - at the crash, and after the resumed run - print `True`.

* **Snapshot the fold, not the worker.** The snapshot is `rebuild()`'s own output at a known sequence number,
  taken from the log right after a response was logged. The worker's in-memory state can be ahead of the log (a
  response received but not yet written); a snapshot of it would contain something the log does not.
* **Rebuild = latest snapshot + the events after it + the same final step.** The solution reuses
  `DurableRunner._close_tool_round`, so a snapshot taken mid-round (tool call logged, result not yet) folds
  correctly when the result arrives.
* **The log stays complete.** Snapshots are a cache: a missing or corrupt one costs a full rebuild, never data.
* **When to turn them on:** four snapshots already weigh more than the whole log here, to save 89 row reads that
  take milliseconds. Snapshots pay off for logs of thousands of events - the week-long coordinators of Days 4
  and 7 - or when a rebuild must be fast at scale (many runs resumed at once after an outage).
