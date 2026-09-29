# Day 1 - Durable agents

The first course ended with Kestrel's Service Desk Copilot going live: a support agent behind deterministic gates,
with evals, guardrails and a go-live memo. Its agent loop is the one you built on Day 2 of that course - a
`messages` list in a Python variable, a `while` loop, tools called inline. That loop is correct, and in production
it is fragile. The process that holds the list is killed by every deploy, by out-of-memory kills and node
failures; queues deliver the same ticket twice; a retry repeats a refund; a manager's approval takes two days, and
no process should hold a thread for two days.

Today the agent run stops being a loop and becomes a **state machine persisted as an event log**. Every model
response and every tool result is written down before the next step; any worker can rebuild the run from the log
and resume it; every side effect carries an idempotency key that the system of record honours; actions that span
several systems run as sagas that can undo themselves; approvals park the run durably; leases and heartbeats keep
two workers off one run; and the log doubles as the flight recorder you replay to debug, fork to explore and
export to a tracing backend. The runtime is `advanced/lib/durable.py` - about 500 lines on SQLite. You will use
it, break it on purpose, and extend it without editing it.

None of the patterns is new: write-ahead logs come from databases, idempotency keys from payment APIs, sagas from
long-lived database transactions, leases from distributed locking, and Temporal, AWS Step Functions, Azure Durable
Functions and Managed Agents sessions package them as products. What an agent run adds is a messages array that
must be rebuilt **byte for byte** (prompt caching and preserved thinking both depend on it), side effects chosen by
a model at run time, and waits for people that last days. Day 4's coordinator and the capstone run on this runtime.

## Learning objectives

By the end of the day you can:

1. Explain how an in-memory agent loop fails after go-live - deploys, crashes, retries, duplicate workers, long
   waits - and model a run as a state machine whose every transition is written to an append-only log first.
2. Compare event sourcing, snapshots and checkpoints, and say what Temporal, AWS Step Functions, Azure Durable
   Functions and Managed Agents sessions give you and what you still own.
3. Make side effects at-most-once with idempotency keys, an effects table and a system of record that honours the
   key; explain why "exactly-once" is at-least-once delivery plus an idempotent receiver; size a dedup window.
4. Run multi-system actions as sagas with compensations and a durable rollback, and say when two-phase commit or
   "just retry" is the right answer instead.
5. Park a run on a human approval for days, decide it from another process, resume it correctly, and run an SLA
   sweeper; compare this with stop-and-ask and `tool_choice` patterns.
6. Choose lease TTLs and heartbeat and sweep intervals; detect stuck runs; take them over; fence zombie workers.
7. Reproduce a production bug offline from the log, fork a run at an event, decide what to log (PII, thinking
   signatures, cost), and export the log as an OpenTelemetry-style trace.
8. Decide where run state should live for a given system: SQLite, Postgres, a workflow engine, or hosted sessions.

## Agenda (about 7 hours)

| Time | Block | Lab |
|---|---|---|
| 0:00 - 0:45 | Why in-memory loops fail; the run as a state machine; the event log | 01 |
| 0:45 - 1:45 | Crash and resume; byte-exact rebuilds; event sourcing, snapshots, engines | 02 |
| 1:45 - 2:00 | Break | |
| 2:00 - 3:00 | Idempotency and "exactly-once" | 03 |
| 3:00 - 3:45 | Sagas and compensation | 04 |
| 3:45 - 4:30 | Lunch | |
| 4:30 - 5:20 | Approvals that wait | 05 |
| 5:20 - 6:05 | Leases, heartbeats, stuck runs | 06 |
| 6:05 - 6:45 | Replay, forks, what to log, traces | 07 |
| 6:45 - 7:00 | Where run state should live; the case study review | |

Exercises (`exercises/README.md`, twelve of them, worked solutions in `solutions/README.md`) are for the evening or
the next morning; four are hands-on extensions of the runtime (cancel, a concurrent-safe sweeper, fencing,
snapshots).

**Running the labs.** `python advanced/day1_durable_agents/labs/01_event_sourced_run.py` from the repository root
(activate the venv first). Without an API key everything runs against labkit's offline mock: the model is a
rule-based stand-in, while the stores, the logs, the leases, the effects, the sagas and the approvals are the real
code - crashes are simulated with `crash_at` and a `BaseException`, so they leave exactly what a dead process
leaves. Each lab keeps its runs in SQLite files under `.runs/advanced/day1/`; open them with any SQLite client
and read the `events` table while you work. With `ANTHROPIC_API_KEY` set, the same scripts call Claude; the
simulated total of the seven labs is about $1.82, of which lab 02 (about twenty runs of the same ticket, some on
Claude Fable 5.1) is $0.77. The runner sends the first course's support-agent request shape through
`client.beta.messages.create`: automatic caching plus server-side refusal fallbacks (`fallbacks="default"`, beta
`server-side-fallback-2026-07-01`); when a beta graduates, expect the header to stop being required and the
parameter to be accepted by `client.messages.create` - check the release notes then. Excerpts below are *mock
mode*: live wording, token counts and tool_use ids differ; the mechanics do not.

---

## 1. Why in-memory agent loops fail after go-live

**The idea.** An in-memory loop keeps a run's state - the messages array, which tool calls have been answered,
where in the loop it is - in the variables of one process. After go-live, processes die routinely, requests are
delivered more than once, and some steps wait for hours. Each of those turns an in-memory loop into lost work,
repeated side effects, or both.

| event | how often | what the in-memory loop loses or repeats | what a durable run does |
|---|---|---|---|
| rolling deploy (SIGTERM, a grace period) | every deploy: 6.3 in-flight runs a month at Kestrel, and practically every approval wait (exercise 4) | the messages array and the position in the loop; the customer never hears back | the next worker rebuilds the run from the log and continues (lab 02) |
| crash, out-of-memory kill, lost node (`kill -9`) | rare per process, routine per fleet | the same, plus the lock it held | its lease expires; a sweeper resumes the run (lab 06) |
| a retry of the whole request (queue redelivery, client retry, a "retry failed jobs" script) | whenever something times out | nothing: it *repeats* everything - model calls, and side effects that already happened | the run id is the dedup key, the retry resumes the same run, effects are keyed (lab 03) |
| two workers on one run (duplicate delivery, a takeover while the first worker is alive) | whenever leases are wrong | double everything | leases, heartbeats, fencing (lab 06, exercise 11) |
| a wait for a person | 12% of Kestrel's runs, 18 business hours on average | a thread for hours, killed by the next deploy | the run parks as a row; any process decides; any worker resumes (lab 05) |
| "why did it do that?" two weeks later | weekly | the transcript, unless someone happened to log it | the log is the transcript, the bill and the audit trail (lab 07) |

**The run as a state machine.** `durable.py` gives a run six statuses, and every transition is an event in the
log:

| from | on | to |
|---|---|---|
| - | `create(run_id)` (idempotent: the same id returns the same run) | `pending` |
| `pending` | a worker acquires the run's lease | `running` |
| `running` | the model ends its turn | `completed` |
| `running` | the turn limit, or a model call the API rejects (a 4xx no retry fixes, logged as `model.error`); any other exception leaves the run `running`, for a sweeper | `failed` |
| `running` | a tool raises `ApprovalRequired` | `waiting_approval` |
| `waiting_approval` | `decide()` or `expire()`, from any process | `pending` |
| `running`, lease expired (its worker died) | a sweeper acquires the lease | `running`, on another worker |
| any non-final status | `cancel()` (exercise 9) | `cancelled` |

Inside `running` the loop has its own states, and each transition is written *before* the next action - the
write-ahead rule of database logs: model call, then `model.response` logged, then `tool.started` logged, then the
tool runs, then `tool.result` logged, then the next model call. Each write is a commit point. A crash between two
of them loses only the work since the last one, and the log says precisely which: lab 02 crashes the run at every
point and counts what the next worker replays and re-executes.

**When not to.** A single model call with no side effects (classification, extraction) is retried whole. A batch
whose unit of work is idempotent (a results table keyed by item, or the Message Batches API) is redone per unit.
Durability costs a database write per step and discipline about effects; it pays off when runs take several
steps, touch systems of record, or wait.

**Kestrel example.** Lab 02, step 6, kills the first course's loop right after `issue_refund` returned and runs it
again from scratch: `model calls in total: 6 (durable: 4), refunds=1`. There was no second refund only because
`get_rma` re-read the world and the refunds table's primary key would have refused one - two safety nets in the
tools, not in the loop. Lab 03 removes them.

---

## 2. Event sourcing, snapshots and checkpoints - and the engines that do it for you

**Event sourcing** stores the facts and derives the state by folding them in order. It comes from accounting
ledgers (you never erase a line; you post a correcting entry) and from database write-ahead logs (the log is the
truth, tables are a cache of it). For an agent run the facts are:

| event | written | carries | used for |
|---|---|---|---|
| `run.created` | at intake | kind, input (message, channel-verified sender, ticket) | the first user message |
| `run.status` | every transition | status, error | dashboards, stuck-run detection |
| `model.response` | right after the call, before any tool runs | turn, the full content (thinking blocks with signatures, text, tool_use), stop_reason, usage | the assistant turns; the bill |
| `model.error` | when the API rejects a model call (a 4xx no retry fixes) | turn, HTTP status, message | why the run failed |
| `tool.started` | before a tool runs | tool_use_id, name, input | "was it attempted?" - the in-flight question |
| `tool.result` | when the tool returns | tool_use_id, the exact string sent back, is_error | the tool_result blocks |
| `approval.requested` / `.decided` | when a tool asks; when a person decides or a sweeper expires it | the action; the outcome, who, the note | the approvals UI, the audit |
| yours: `saga.*`, `approval.escalated`, `run.cancelled`, ... | when your code needs them | anything | sagas, sweepers, cancellation |

`rebuild()` folds the log into `(messages, results by tool_use_id, turns, results replayed)`. Two properties
matter for agents in particular.

**Byte-exactness.** Prompt caching reads a prefix only if it is byte-identical to one an earlier request wrote.
Preserved thinking binds each thinking block's signature to everything before it - the system prompt, the tool set,
every earlier message: Claude Fable 5.1 enforces the check, Claude Opus 5.5 enforces it for accounts created on or
after 2026-08-31, and other accounts can opt in with `thinking.block_binding.prefix_mismatch_behavior` under the
beta header `thinking-binding-controls-2026-08-01` (lab 02 sets it, so it behaves the same on any account; while
the controls are in beta they need that header and `client.beta.messages`, and a request that sends `block_binding`
without the header is a 400). A rebuild that is merely *equivalent* breaks both. Lab 02 resumes the same crashed
run three ways:

```
resume variant                       claude-opus-5: first resumed request         claude-fable-5-1
faithful (the log's bytes)           read 2,828 write   105 -> completed          completed
tool results re-serialised (JSONB)   read 2,747 write   186 -> completed          400 messages.3.content.0: The block is bound to a different conversation. Run failed.
redeploy: new system prompt (v2)     read     0 write 2,949 -> completed          400 messages.1.content.0: The block is bound to a different conversation. Run failed.
```

The second row is a storage bug you can ship without noticing: Postgres `jsonb` (like any column that normalises
JSON) re-orders object keys, so the tool results come back as the same data in different bytes. Store what you
will send back to the model as exact text (`text` or `json`). The third row is a deploy: new code picked up an
in-flight run and sent it the new system prompt. No retry can fix either 400, so the runner logs a `model.error`
event and fails the run instead of leaving it `running` for a sweeper to retry forever.

**Versioning.** The log outlives the code that wrote it. Pin what a run started with - the prompt version, the
tool set, the model - on the run, keep old versions loadable until their runs drain, and give new versions to new
runs only. When an event's schema changes, read old events into the new shape ("upcasting") instead of rewriting
history. Managed Agents does the prompt half for you: a session pins the agent version it was created with.

**Snapshots and checkpoints** are the alternatives, and databases combine them:

| approach | stores | rebuild | storage | replay, fork, audit | watch out for |
|---|---|---|---|---|---|
| event log | every event, once | fold every event | linear in the turns | yes - the whole history | long logs rebuild slowly; schema evolution |
| snapshot per step (agent frameworks' "checkpointers" commonly save the state after every step of a thread) | the whole state after each step | load the latest | grows with the square of the turns: every snapshot repeats the history | only if you keep every snapshot | the latest snapshot must be complete and exact |
| checkpoint + log tail | the log, plus a periodic snapshot of its fold | latest snapshot + the events after it | linear, plus the snapshots | yes - the log stays complete | snapshot and log must agree byte for byte |

At four turns the difference is small (lab 01: a 4,661-byte log against 7,388 bytes of per-turn snapshots); at 40
turns it is 109,900 bytes against 1,540,000 (exercise 3). Exercise 12 builds the checkpoint-plus-tail version and
measures a rebuild that reads 27 rows instead of 116 - milliseconds either way, which is why agent runs of tens of
turns need no snapshots, while a coordinator that runs for weeks does.

**The engines.** Durable execution is a product category; each one packages this day's patterns:

| | Temporal | AWS Step Functions (Standard) | Azure Durable Functions | Managed Agents sessions | `durable.py` in your database |
|---|---|---|---|---|---|
| state | event history; workflow code replayed deterministically | a state machine (Amazon States Language) and its execution history | orchestrator code replayed from history | a session: event history, a container, checkpoints | your event log |
| the agent loop is | a workflow; every model call and tool call an activity (they are not deterministic) | a loop of Task states; a Lambda calls the model | an orchestrator calling activities | Anthropic's, server-side | your runner |
| side effects | activities: at-least-once with retry policies | tasks retried by each state's `Retry` | activities: at-least-once | hosted tools run in the session's container; custom tools run on your side | `ctx.effect()` and keys |
| waiting for a person | signals and updates, durable timers | `.waitForTaskToken` callbacks with heartbeat and timeout | `WaitForExternalEvent` plus durable timers | the session idles with `requires_action` until `user.tool_confirmation` or `user.custom_tool_result` | `ApprovalRequired`, `decide()` / `expire()`, sweepers |
| limits you meet | history size (continue-as-new), payload size, determinism rules | 256 KB per state's payload, 25,000 history events, one-year executions | determinism rules, replay cost as history grows | the platform's quotas and session pricing | whatever your database handles |
| in-flight versioning | patching, worker versioning | versions and aliases | your job (side-by-side deployments) | sessions pin an agent version | your job (pin versions per run) |
| **you still own** | idempotent activities, byte-exact transcripts across activities, approval semantics | the same | the same | custom tools and their idempotency, approval records, reconnecting to the stream (history fetch + dedupe by event id), a ticket -> session table (session creation takes no idempotency key) | everything, including the sweeper |

Every column has the same last row in some form: the engine makes its own bookkeeping exactly-once; your side
effects stay at-least-once until you key them.

---

## 3. Idempotency and "exactly-once"

**The idea.** A network can deliver a message at most once (and lose some) or at least once (and duplicate some);
it cannot deliver exactly once. What products call "exactly-once" is **at-least-once delivery plus an idempotent
receiver**: a second copy of a request is recognised and answered with the first copy's result. The pattern comes
from payment APIs (an `Idempotency-Key` header: the same key within a window returns the same charge instead of a
second one), message brokers (deduplication ids) and HTTP (PUT is idempotent, POST is not unless you make it so).
For an agent the request is a tool call with a side effect: a refund, a credit, an RMA, a carrier booking, an
email.

**Three pieces, and why you need all three.**

1. **A stable key.** `ctx.idempotency_key` is `run_id:tool_use_id`: unique per decision the model made, and
   stable across resumes because the tool_use id comes from the logged model response. A key minted at call time
   (`uuid4()`) is useless - a retry gets a new one. Business keys ("one refund per RMA") deduplicate *across*
   runs and belong to the tool, not the runtime (exercise 5).
2. **A local record: the effects table.** `effect_begin` claims the key (`started`, in one `INSERT OR IGNORE`, so
   two claimants cannot both win), `commit` stores the result (`done`). On resume, `done` replays the stored
   result; `started` means **in flight**: a worker died between the claim and the commit, and the outcome is
   unknown.
3. **The key pushed to the system of record.** Only the system that received the call knows whether it happened.
   With the key downstream, "in flight" becomes a lookup: found - commit it; not found - do it now, same key.

This is the approved-refund path of lab 05 (`labs/_day1.py`), where the refunds table is the system of record:

```python
        with ctx.effect() as eff:                                # key: <run>:<tool_use>:approved
            if eff.done:
                return eff.stored
            if eff.in_flight:
                found = find_refund(db, rma["rma_id"])
                if found:
                    return eff.commit(found)
            return eff.commit(approved_refund(db, rma, round(float(tool_input["amount_usd"]), 2),
                                              tool_input["reason"], approved_by))
```

**Transport retries vs business retries.** A transport retry - the SDK's `max_retries` (two by default, for 408,
409, 429, 5xx and connection errors), your HTTP client's retry - repeats the *same* request and must carry the same
key. A business retry is the model deciding to call the tool again: a new tool_use, a new key, a new decision, and
whether it is allowed is a business rule for the tool (exercise 5). Lab 03 prices every combination against a fake
accounts-receivable ledger that honours keys for one day:

```
  approach                           crash after post       lost response + retry  late resume (3 days)
  naive (no table, no key)           2 credits              2 credits              2 credits
  effects table, no key downstream   1 credit + reconcile   2 credits              1 credit + reconcile
  effects table + key downstream     1 credit               1 credit               2 credits
  ... + business-key fallback        1 credit               1 credit               1 credit
```

Read the second row twice: the effects table stops the *resumer*, but a transport retry inside one attempt goes
straight past it. And read the third column: **dedup windows** are the weak link. A run parked three days on an
approval, a backlog drained after an outage, a replay from backup - all resume after a one-day window has closed,
and the key is unknown downstream. Prefer receivers that keep keys as long as the business record exists; keep a
business-key check (order and amount within 30 days) as the fallback; reconcile.

**Model calls** have no side effect but the bill. The SDK retries them, and a response lost before it was logged is
simply generated again (lab 02, step 3: five model calls for a four-turn run). Log the response immediately; do
not post-process first.

**When the downstream system supports no keys:** look it up by business key before acting (safe while a lease
keeps one worker per run), put an outbox or a proxy you control in front of it, reconcile daily, and fail closed on
in-flight money - a person decides (lab 03, step 2).

---

## 4. Sagas and compensation vs two-phase commit vs "just retry"

**The idea.** Replacing a wrong item touches four systems: the ERP (an RMA), the warehouse (a stock reservation),
the carrier (a collection) and the mail gateway (a confirmation). No transaction spans them. A **saga** - the name
comes from Garcia-Molina and Salem's 1987 paper on long-lived database transactions - runs each step as its own
local commit and pairs it with a **compensation** that semantically undoes it; when a step fails, the completed
steps are compensated in reverse order. Between steps the world is visibly half-done; that is the price of not
locking four systems.

**Compensation is semantic, not a rollback.** Lab 04 withdraws the RMA (status `rejected`, with a note) instead of
deleting it, because the audit trail must keep it; an email cannot be unsent, only corrected. Compensations are
effects too (keyed `...:undo`), so a compensation that already ran is skipped on resume. Steps fall into three
classes: *compensatable* (an undo exists), the *pivot* (the point of no return - a unit handed to a carrier), and
*retriable* (after the pivot: must eventually succeed, so they must be idempotent). Order them in that sequence
(exercise 6).

**The saga's direction must be durable too.** The subtle bug in home-grown sagas: the decision "we are rolling
back" lives in a Python variable. Lab 04, step 5, kills the worker right after its rollback, before the tool
result was logged:

```
  scenario                                       reserved  RMA-7023  pickups  emails  customer told
  direction in memory, crash after the rollback  44 -> 44  rejected  1        1       collection booked, replacement reserved
  direction logged, crash after the rollback     44 -> 44  rejected  0        0       escalated to the order desk
  direction logged, crash in the middle of it    44 -> 44  rejected  0        0       escalated to the order desk
```

In the first row the resumer re-ran the tool call and the saga went *forward* over its own undo: the undone steps
"replayed" from the effects table, the failed carrier step (whose claim an ordinary failure releases) executed and
succeeded, and the customer was promised a replacement that is not reserved, against a withdrawn RMA. Logging
`saga.rolling_back` before the first compensation, and reading it first on resume, fixes it.

| option | guarantee | needs | fails as | fits |
|---|---|---|---|---|
| "just retry" | none by itself - each step's own idempotency | every step idempotent, order irrelevant | silent duplicates (lab 04, step 6: 4 impellers reserved for a 2-impeller replacement) | reads, idempotent writes |
| saga | every step done or compensated | a compensation per step before the pivot, a durable log, a durable direction | visible intermediate states; a failed compensation needs a person | an ERP, a carrier and a mail gateway |
| two-phase commit (XA) | atomic across participants | every participant holds a prepared transaction until a coordinator decides | a coordinator that dies leaves locks everywhere | databases and queues you control |

**The agent plans, the saga executes.** The model decides *whether* to replace and *what* (look up the order,
check eligibility); one tool call - `arrange_replacement` - runs the saga. If the model sequenced the four steps as
four tool calls, the happy path would work and the failure path would ask a probabilistic planner to undo half a
transaction. Compensation logic belongs in code.

---

## 5. Human approvals as durable waits

**The mechanics.** A tool that needs a person raises `ApprovalRequired({"summary": ...})`. The runner records an
approval (`approval.requested`), sets the run to `waiting_approval` and returns - no thread, no lease, no model call
while it waits. Any process that can open the store settles it - `decide()` for a person, `expire()` for a sweeper -
and the run goes back to `pending`. Settling is a conditional update (`... WHERE status = 'pending'`), so the first
decision wins even when two people click at once, and a second click changes nothing. The next worker's `run()`
answers the settled approval before the loop continues: an approved action executes once, under its own key
`<run>:<tool_use>:approved`, and a rejection or an expiry becomes a tool error the model must explain.

**Why the runtime answers the decision, not the consumer.** Lab 05, step 3, first runs a copy of the case on a
runner with that step switched off - the runtime as it was before it answered decisions itself:

```
before the runtime answered decisions itself, a worker calling run() on the decided run (a copy):
  status=waiting_approval turns=3 replayed_tools=2 executed_tools=0 approval=apr_... | approvals now: ['approved', 'pending'] | refunds: []
```

`run()` found `issue_refund` without a result and executed it again without the approval, so the gate asked
again: the manager's decision was ignored and a second request waited in their queue. As long as a decision is
answered only by a special resume call, some queue consumer eventually makes the ordinary one. So the rule lives
where every path passes, in `run()`. On the real run the same plain call answers the approval first - the refund's
`tool.result` lands before turn 4 is generated:

```
    13  approval.decided    approved by ops.manager: inspection report checked
    14  run.status          pending
    15  run.status          running
    16  tool.result         issue_refund ok 174 chars: {"refund_id": "RF-7001", "amount_usd": 9188.5, "status": ...
    17  model.response      turn 4  stop=end_turn  blocks=['thinking', 'text']  tools=[]  in=0 cache_w=103 cache_r=2944 out=254
    18  run.status          completed
```

**Timeouts and escalation** are policy, applied by a sweeper on a schedule: escalate an approval nobody picked up
(lab 05: after 4 hours), expire one nobody decided (after 2 days). **Expiry is not a refusal** - nobody said no -
so record it as expiry: `store.expire()` gives the approval the status `expired`, and the run's tool error says
"Not decided in time". Make sure what the customer is told has a record behind it: in lab 05 that tool error makes
the model open a finance escalation (`ESC-4101`, P2) before replying "overdue". Two cron hosts running the same
sweeper page twice unless each action is claimed first (exercise 10).

**The alternatives.**

| pattern | where the wait lives | survives a deploy | the customer | use it for |
|---|---|---|---|---|
| block the worker until someone answers | a thread | no | waits in silence | nothing that waits for people |
| stop and ask ("a colleague will confirm") | nowhere: the run ends | nothing to survive | a new conversation later, with none of this context | cases a person takes over completely - backed by an escalation record |
| force a `request_approval` tool with `tool_choice` | nowhere: it shapes the model's next action | - | - | not an approval mechanism; forced tool choice is a 400 on Claude Opus 5.5 and Claude Fable 5.1 anyway - put the gate in the executor |
| durable approval (this day) | a row: `waiting_approval` + a pending approval | yes | "pending approval", then the outcome | anything a person must decide |
| Managed Agents: `always_ask` / a custom tool | the hosted session, idle with `requires_action` | yes (server-side) | as you design it | hosted tools; custom tools whose answer you hold until approved - the approval record, roles and SLA are still yours |

---

## 6. Leases, heartbeats, stuck runs and takeover

**The idea.** A lease is a lock with an expiry. `acquire()` is one atomic statement - `UPDATE runs SET lease_owner
= ?, lease_until = ? WHERE run_id = ? AND (lease_owner IS NULL OR lease_owner = ? OR lease_until < ?)` - so two
workers that pick the same run cannot both win it, and a winner that dies does not hold it forever. A worker
extends its lease with **heartbeats**; `DurableRunner` heartbeats before every model call (and stops with
`LeaseLost` when one fails), and anything slower than the TTL - a slow tool, a long model call - must heartbeat
from inside (`ctx.store.heartbeat(...)`). Leases come from distributed locking; the TTL is a bet that a worker
which has not heartbeated for that long is dead.

**The false takeover.** When a step outlives the TTL without heartbeats, another worker takes a run whose first
worker is still alive. Lab 06, step 2:

```
without heartbeats: worker-b at t=1.6 s ACQUIRED the lease (it had expired at t=1.0 s) and ran the run: status=completed turns=3 replayed_tools=1 executed_tools=1
  worker-a: LeaseLost: run lease-heartbeat-off: lease taken over by another worker; stopping
  log: model.response=3, tool.result=3, tool.started=3
  worker-a's heartbeat() answers, in order: True x2, False x1
  get_order results in the log: 2 - the same tool_use answered twice
```

Worker-a's heartbeat before its turn-3 model call answered False, and the runner stopped it with `LeaseLost` - no
second turn 3. But the `get_order` result it wrote on returning from the slow tool had already landed: the zombie
learns it is one at its next heartbeat, one step too late. Whatever it does in that step happens twice - the rest of
its tool round, or, when the takeover happens during a long model call, a whole turn; **live, that turn's tool_use
ids differ from the new owner's, so its writes carry different idempotency keys** and can happen twice. The remedy
is **fencing**: every write is conditional on still holding the lease, in the same statement, so a zombie's first
write after a takeover fails (exercise 11; Martin Kleppmann's "fencing tokens" are the version-number form of the
same idea). What fencing cannot stop - an effect the zombie already caused in another system - is covered by
idempotency keys and, where you own the downstream system, by a lease version it can check.

**Stuck-run detection.** `stuck(older_than_s)` is a query, not a judgement: status `running` and a lease that
expired more than `older_than_s` ago. Between a `kill -9` and the expiry, the run looks alive (lab 06, step 3:
`stuck(older_than_s=0) now: []`, then `['lease-killed']` after the TTL). A sweeper calls `run()` on what it
finds; the lease acquire is the only coordination needed. It must also pick up runs that are `pending` (settled
approvals, new work) - the same `run()` handles both.

**Choosing the numbers.** With a heartbeat every *h*, a TTL *T* (commonly 3*h*) and a sweep every *S*, a dead
worker's run is resumed between *T* and *T + S* after its last heartbeat: 30-90 s for *h* = 10 s, *T* = 30 s,
*S* = 60 s. A longer TTL is slower recovery; a shorter one is more false takeovers under load (GC pauses, a
saturated database, a slow model response). Exercise 4 puts Kestrel's numbers through it: 2.3 false takeovers a
month from one slow ERP call. Log every takeover - a rising count is the earliest sign of an overloaded fleet.
Graceful shutdown helps more than tuning: on SIGTERM, stop taking work, finish or abandon the current step, and
release the lease so the next worker takes the run at once.

---

## 7. Replay, forks and time-travel debugging; what to log

**Replay without the model.** The log is the transcript. Lab 07 investigates a report ("the copilot tried to push a
$9,188.50 refund the customer asked us to confirm first") with `rebuild()` and zero model calls, and finds the
turn: the model called `issue_refund` at turn 3 although the ticket said *"please confirm the figure before you
issue it"*; only the tool's approval limit stopped the money. Nothing was refunded, so no dashboard flagged it -
only the log shows the intent.

**A replay harness classifies tools.** Reads are re-executed against the current world and diffed with the logged
result - a difference is *drift*: the world moved since the run (the RMA got refunded, the order shipped). Writes
are answered from the log, never re-executed: re-executing a write during an investigation is how a second
escalation, email or refund happens.

**Forks.** Copy a run's log up to an event, patch that event, and let the model continue in a sandbox: what would
the copilot have done with a different tool result? Lab 07 patches `refund_due_usd` to 2,400 - under the agent's
limit - and the continuation issues the refund without confirming the figure: the latent bug, reproduced before it
happened in production (a prompt-and-eval finding for Day 6). Forks edit history, so preserved thinking constrains
them: a fork at a *tool result* leaves every earlier thinking block's prefix intact and works on any model; a fork
that rewrites an earlier turn or the system prompt needs `prefix_mismatch_behavior: "drop_block"` (beta
`thinking-binding-controls-2026-08-01`) on models that enforce the check, and then continues without that
reasoning.

**What to log.**

| what | keep? | why |
|---|---|---|
| requester email, names | in the log yes; masked in exports | the run needs the channel identity to resume; log readers do not (PRV-004) |
| tool inputs and results | yes, exactly | the evidence, and the bytes a resume must re-send; cap sizes at the tool, never drop errors |
| thinking blocks | yes, verbatim | the signature is required to resume on the same model; the text is empty under the default `display: "omitted"`, and a `"summarized"` display can contain personal data - treat it like tool results |
| usage per turn | yes | the bill per run and per turn without a second system (lab 01: `this run cost $0.0436`) |
| model id, prompt and tool versions | yes | thinking is model-bound; a resume must use the versions the run started with |
| retention | a policy | as long as the business record (the refund, the RMA) plus the audit window; after that, the log is personal data you no longer need |

Redact at the boundary readers cross - exports, traces, dashboards - not in the log: the resuming worker needs the
exact bytes. Lab 07 masks the requester's email and name for export and keeps the signature verbatim.

**The log as a trace.** Project the log onto spans: one trace per run (the run id as trace id, so a resume
continues the same trace), an `llm.call` span per `model.response` (usage, stop reason, cost), a `tool.<name>` span
per tool call (error status from `is_error`), an `approval.wait` span per approval. `labkit.tracing` uses
OpenTelemetry-style `gen_ai.*` attribute names, so the mapping to a real exporter is one to one. Export from the
log, not from the worker's memory, and a crashed worker's spans still exist.

---

## 8. Where run state should live

| situation | put the state in | because | rejected |
|---|---|---|---|
| one host, a handful of workers (a department tool, a prototype) | **SQLite** (WAL mode) next to the workers | zero operations; `durable.py` as shipped | a network filesystem under SQLite (unsafe locking) |
| several hosts, a database you already run, runs of seconds to days | **Postgres**: events, effects, approvals, leases as tables; `SELECT ... FOR UPDATE SKIP LOCKED` for the queue; `text`/`json` columns for anything re-sent to the model | transactions, backups and on-call you already have | a new engine for one workflow |
| many long workflows with timers, signals and human steps, beyond agents | **a workflow engine** (Temporal; Step Functions on AWS; Durable Functions on Azure) | timers, retries, history and operations tooling out of the box | hand-rolled timers and sweepers at that scale |
| a sandboxed, tool-heavy agent (code, files), no business side effects, Anthropic running the loop acceptable | **Managed Agents sessions** | the loop, the container, event history and budgets are hosted | rebuilding the sandbox and the loop yourself |
| read-only, restartable work | **nothing beyond a results table** | redoing a unit costs only tokens | an event log per item |

Whatever you choose, four details decide whether it works: store the bytes you will re-send exactly (lab 02); give
every run a deterministic id derived from the request (a Message-ID, a ticket id), so a redelivery resumes instead of
duplicating; index the log by `(run_id, seq)`; and decide retention before the table is large. Exercise 8 applies
the table to four Kestrel systems.

---

## Case study: the Service Desk Copilot after the Tuesday deploy

**Where it started.** The copilot went live after the first course's go-live review: about 1,900 support emails a
month, the Day 2 support agent behind an input screen and an output guard, refunds up to $2,500 issued by the tool,
larger ones escalated. Requirement R8 said a redelivered email must not duplicate actions; the team met it at the
front door by deduplicating on the email's Message-ID. The agent loop itself ran in the worker's memory, and
approvals for goodwill credits were a thread per request, polling a chat channel for a manager's reaction.

**What went wrong.** A routine Tuesday deploy restarted the workers. Every run in flight died with its messages
array. The mail gateway redelivered the unacknowledged emails - and the front door discarded them as duplicates,
because it had seen their Message-IDs: the customers were never answered, and nobody knew until they wrote again.
Every approval thread died too; managers who approved afterwards approved into the void. On Thursday an engineer
ran a script that re-ran "failed" tickets from scratch. One of them was Harbor Foods' goodwill credit for the late
seal kits on SO-10283: the first attempt had posted the $150 credit to the accounts-receivable ledger before the
pod died, so the re-run posted it again - lab 03, step 1, is that incident: `ledger: credits=2 (CR-0001, CR-0002)`.

**Root causes.** (1) The run's state lived in a process. (2) Idempotency existed only at the front door, not at the
effects - so it turned lost runs into silent drops and did nothing for repeated side effects. (3) The ledger call
carried no idempotency key. (4) Approvals were threads. (5) Nothing could say what a run had done: no log.

**What changed.** The copilot moved onto the durable runtime, requirement by requirement:

| requirement | design | evidence |
|---|---|---|
| D1 a crash or deploy loses no run | run record created at intake with `run_id` derived from the ticket; every step logged first; any worker resumes | lab 02: every crash point ends with one refund; a resumed run reads the crashed worker's cache |
| D2 no side effect twice | effects table + keys pushed to the ledger and the carrier; business keys in the tools | lab 03's matrix; lab 04's saga |
| D3 approvals wait up to five business days | `ApprovalRequired`, an approvals UI on the same store, `run()` answering settled approvals, an SLA sweeper that escalates and then expires (`store.expire()`) | lab 05 |
| D4 two workers never run one run | leases, heartbeats inside slow tools, a stuck-run sweeper, fenced writes | lab 06, exercise 11 |
| D5 any run can be explained later | the log is the transcript, the bill and the audit; replay and forks offline; traces exported from the log | lab 07 |
| D6 durability costs no tokens | rebuilds are byte-exact, so a resume reads the prompt cache; prompts pinned per run across deploys | lab 02, steps 4-5 |

The front door kept its Message-ID check, but a redelivery now *resumes* the existing run instead of being
dropped. The retry script was deleted: nothing needs re-running from scratch when every run can be resumed.

**What is still open** (and where it is taught): cancellation from the approvals UI (exercise 9); two sweeper hosts
(exercise 10); snapshots for the week-long coordinators the recall campaign will need (exercise 12, Day 4, Day 7).

---

## Lab walkthrough

### Lab 01 - `01_event_sourced_run.py`: an event-sourced run

Runs the support agent on ticket T-1206 (Keystone Mechanical: "has RMA-7004 been processed, when is the refund
issued?") through `DurableRunner`, with a `SupportDesk` over a shared copy of the ops database as the executor.

1. **A ticket becomes a run record** - status `pending`, no lease, one `run.created` event. Nothing has been sent to
   the model: the record *is* the queue entry.
2. **The run** - four turns, three tools, one refund (`RF-7004`, $663.00).
3. **The log.** *Mock mode:*
   ```
        3  model.response      turn 1  stop=tool_use  blocks=['thinking', 'text', 'tool_use']  tools=['get_customer_profile']  in=0 cache_w=2747 cache_r=0 out=186
        4  tool.started        get_customer_profile {}
        5  tool.result         get_customer_profile ok 187 chars: {"verified": true, "customer_id": "C-1016", "name": "Keys...
   ```
   and so on to `13  run.status  completed`. Usage is stored per turn, so the log is also the bill (`this run cost
   $0.0436`), and it is small: `the log holds 4,661 bytes of payload` against 7,388 bytes of per-turn snapshots.
4. **`rebuild()` against the in-memory loop.** The rebuilt array equals what the worker held in memory
   (`identical: True`). The first course's loop, on the same ticket, holds the same conversation but not the same
   bytes:
   ```
     byte-identical? False - first difference: message 2, block 0 (tool_result): field 'is_error' = None vs False
     thinking signature of turn 1..4 equal in both runs: [True, False, False, False]
   ```
   The mock binds thinking signatures to the exact JSON before them, so everything after the first tool result
   differs. Whether the real API normalises that particular field is not something to rely on: re-send the bytes
   you sent.
5. **What survives the process.** A second `RunStore` on the same file sees the run, its reply and its status
   transitions; creating the run again with the same `run_id` returns the existing run and logs nothing new
   (`status=completed, run.created events: 1`): a ticket delivered twice is one run.

### Lab 02 - `02_crash_and_resume.py`: crash and resume

1. **Baseline:** `model calls=4  events=13  refunds=1`.
2. **The crash matrix** - every crash point on every turn, resumed by a second worker. *Mock mode:*
   ```
   crash point      events last event logged   status   replayed executed model calls refunds  resumed
   after_model  1        3 model.response      running         0        3           3 refunds=1  completed
   after_tool   1        5 tool.result         running         1        2           3 refunds=1  completed
   before_tool  2        6 model.response      running         1        2           2 refunds=1  completed
   after_tool   3       11 tool.result         running         3        0           1 refunds=1  completed
   after_model  4       12 model.response      running         3        0           0 refunds=1  completed
   ```
   (five of the ten rows). The resumer executes exactly the tools the log has no result for, calls the model for
   exactly the turns the log lacks, and there is always one refund. In the last row the final answer was logged but
   the run never marked completed; re-sending that conversation would be an assistant prefill (a 400 on current
   models), so the runner completes the run from the log with no model call.
3. **The response was lost** (the process died with the turn-2 response in memory): `model calls for the whole run:
   5 (4 turns + 1 repeated)`. The log makes tool execution at-most-once; it cannot make a model call exactly-once.
4. **Append-only.** With the mock's cache cleared so that worker-a is the only writer:
   ```
     worker-a turn 2: cache_read=2,747 cache_write=81  (its whole prompt: 2,828 tokens, now in the cache)
     worker-b turn 3: cache_read=2,828 cache_write=105 uncached=0
   ```
   worker-b's first request extends worker-a's last one byte for byte and reads exactly what it cached.
5. **What breaks it** - the table in section 2: re-serialised tool results and a new system prompt collapse the
   cache on Claude Opus 5 and are rejected on Claude Fable 5.1 (the lab enables the check explicitly, so live it
   behaves the same on any account); the rejected run ends `failed`, with a `model.error` event. Fix: exact bytes in
   storage, versions pinned per run.
6. **The naive alternative** - re-running the first course's loop: six model calls instead of four, a different
   reply, and one refund only thanks to the tool's own checks.

### Lab 03 - `03_idempotent_effects.py`: idempotent effects

A goodwill-credit agent (`get_order` -> `post_credit`) posts Harbor Foods' $150 credit to a fake AR ledger that
honours idempotency keys for one day. Step 1 reproduces the case study (`credits=2 (CR-0001, CR-0002)`); step 2 adds
the effects table alone - one credit, but the resumer can only tell the manager to have finance reconcile; step 3
pushes the key downstream, and a crash before *or* after the ledger call ends with one credit:

```
    worker-a: CRASH - worker killed after post
      effects table: in flight -> a previous attempt started and never finished
      ledger.find('b12f04b660c8') -> CR-0001: reuse it
```

Step 4 retries transport failures with and without the key; step 5 resumes three days later, after the ledger forgot
the key (two credits), and again with a business-key fallback (one); step 6 runs twelve more scenarios to fill the
matrix quoted in section 3.

### Lab 04 - `04_sagas_and_compensation.py`: sagas and compensation

Ticket T-1205 (Ironclad Steel received bronze IMP-250-A impellers instead of duplex IMP-250-D). The agent checks the
order and eligibility, then calls `arrange_replacement` once; the saga runs `create_rma -> reserve_stock ->
book_pickup -> notify_customer`, each step an effect keyed `run:tool_use:step`. A carrier failure, *mock mode:*

```
     13  saga.step         book_pickup      failed  error=SwiftParcel: no collection capacity in this postcode befo...
     14  saga.rolling_back book_pickup        undo: reserve_stock, create_rma
     15  saga.compensated  reserve_stock    
     16  saga.compensated  create_rma       
    world: reserved IMP-250-D 44 -> 44 | RMAs for SO-10292: [('RMA-7023', 'rejected')] | pickups booked: 0 | emails: 0
```

The model receives the error and what was undone, escalates to the order desk and tells the customer a person will
confirm. Step 3 crashes between steps (two replayed, two executed, stock reserved once); step 4 crashes inside
`book_pickup` after the carrier booked, and the step's `recover()` finds the booking by reference (`recovered`);
step 5 is the durable-direction table of section 4; step 6 is "just retry" (`reserved IMP-250-D 44 -> 48`).

### Lab 05 - `05_approvals_that_wait.py`: approvals that wait

Midland Oil confirms the $9,188.50 refund on RMA-7001 - above the agent's $2,500 limit.

1. **Parked:** `store: status=waiting_approval lease_owner=None refunds=[]`; another worker that picks the run up
   returns at once with `model calls made: 0`.
2. **Decided from another process:** a second `RunStore` on the same file (the approvals UI) lists the run, shows the
   action and approves; a second click changes nothing.
3. **Resumed with a plain `run()`** - a copy of the case on a runner without the runtime's decision step re-parks
   the run (section 5); today's runtime completes it:
   ```
     worker-b: status=completed turns=4 replayed_tools=3 executed_tools=0
     reply:
       Your refund of $9,188.50 for RMA-7001 has been issued (reference RF-7001, approved by
       ops.manager). It will reach your original payment method within 10 business days.
   ```
   (`executed_tools=0`: the approved refund ran before the loop, inside `run()`, as a keyed effect and was logged;
   the loop then replayed it with the other two results.)
4. **Rejected:** the tool result names who declined and why; the reply says so and promises nothing it cannot keep.
5. **The sweeper:** escalated at +4 hours, expired with `store.expire()` at +2 days (the log: `expired by
   sla-sweeper: no decision within 2 days`), and the resumed model escalates before replying:
   ```
     sweep at +4 hours    -> ['sd-T-1207-overdue: escalated to finance_director after 4 h']
     sweep at +1 day      -> ['nothing to do']
     sweep at +2 days     -> ['sd-T-1207-overdue: expired after 2 days -> run back in the queue']
   ```
   followed by `escalations in the system of record: [{'escalation_id': 'ESC-4101', 'queue': 'finance', 'priority':
   'P2'}]`.
6. **Stop and ask** with a read-only toolset: the run ends with a promise and no context for whoever picks it up.

### Lab 06 - `06_leases_heartbeats_stuck_runs.py`: leases, heartbeats, stuck runs

Four scenarios with worker threads on the same on-disk store (wall-clock timings are real; they take about ten
seconds). Step 1: `worker-b: RuntimeError: run lease-contention is leased by another worker`. Step 2: the
heartbeat comparison in section 6 (with heartbeats every 0.25 s the second worker is refused; without them the lease
lapses mid-tool, and worker-a stops with `LeaseLost` one duplicate result later). Step 3: a worker killed like
`kill -9` (its lease is not released); `stuck()` is empty until the TTL passes, then the takeover finishes the run
with the logged result replayed. Step 4, the sweeper's view:

```
run            status     lease_owner  lease    action
sweep-healthy  running    worker-live  live     leave alone (heartbeating)
sweep-dead     running    worker-x     expired  take over
sweep-done     completed  None         -        leave alone
```

Live, the model calls take seconds, so the takeover happens later in wall-clock terms, and a takeover during a long
model call lets the zombie log a whole turn - with tool_use ids of its own - before its next heartbeat stops it.

### Lab 07 - `07_replay_and_time_travel.py`: replay and time travel

1. **The original run** on T-1207: `get_customer_profile -> get_rma -> issue_refund -> escalate_to_human`.
2. **Offline reproduction:** `rebuild(): 10 messages, 5 turns, 4 tool results - model calls made: 0`, and the
   evidence:
   ```
   Evidence: at turn 3 the model called issue_refund with {'rma_id': 'RMA-7001', 'amount_usd': 9188.5, 'reason': 'Returned items received and inspected'}
             the ticket says: "It is a large amount, so please confirm the figure before you issue it."
   ```
   (The mock's stand-in issues refunds as soon as it sees a received RMA - that is the behaviour under
   investigation; live, Claude may ask first, and the log shows which.)
3. **Replay:** reads re-executed with `no drift`, writes stubbed from the log; still one escalation in the database.
4. **Fork** at `get_rma`'s result with `refund_due_usd` patched to 2,400: the continuation issues `RF-7001` for
   $2,400.00 in a sandbox; the original run is untouched.
5. **The trace**, projected from the log (durations are wall-clock and differ on every run):
   ```
   trace sdT1207000000000 (durable-support)
     - agent.run [145 ms]
       - llm.call [121 ms]  in=0 out=186 stop=tool_use $0.02195
       - tool.get_customer_profile [1 ms]
   ```
   ending `totals: llm_calls=5 tool_calls=4 in=0 out=1101 cost=$0.05316 errors=1`, with the mapping from log fields
   to span attributes.
6. **What to log:** the requester's email and name masked for export, the signature kept verbatim:
   ```
   run.created as logged : requester_email='travis.greer@midlandoil.example'  last line='Travis Greer, Midland Oil Services'
   run.created redacted  : requester_email='<email>'  last line='<name>, Midland Oil Services'
   ```

---

## Key takeaways

1. An agent run is a state machine; persist it as an append-only log written *before* each next step, and any
   worker can resume it from any crash point with only the lost step repeated.
2. Rebuild the messages array byte for byte. Prompt caching and preserved thinking both check bytes, not meaning:
   store what you re-send as exact text and pin prompt and tool versions per run across deploys.
3. The log gives you at-most-once tool *execution*; it cannot tell you what a downstream system did. Exactly-once
   effects are at-least-once attempts, a local effects table, and a key the system of record honours.
4. Keys must be stable across retries (`run_id:tool_use_id` from the log, or a business key), transport retries
   must reuse them, and dedup windows must outlive your longest resume - or have a business-key fallback.
5. Multi-system actions are sagas: compensatable steps, a pivot, retriable steps - and the saga's direction is
   logged, or a crash after its rollback sends it forward over its own undo.
6. A human approval is a durable wait: a row, not a thread. Decide from any process, let the runtime answer the
   decision on the next `run()` (a rule every consumer would otherwise have to remember), execute the approved
   action under its own key, and treat expiry as expiry.
7. Leases keep two workers off one run only while every slow step heartbeats. A zombie learns it is one at its next
   heartbeat (`LeaseLost`) - one step too late - so fence writes on the lease and key every effect.
8. Detection latency is TTL plus sweep interval; graceful shutdown that releases leases beats tuning both.
9. The log is the flight recorder: replay reads, stub writes, fork at a tool result, export traces from the log,
   and redact at the export boundary, never in the log.
10. Workflow engines and hosted sessions make their own bookkeeping durable; the idempotency of your side effects
    and the semantics of your approvals stay yours wherever the state lives.

## Further reading

* Tool use - https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
* Handling stop reasons - https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
* Errors and retries - https://platform.claude.com/docs/en/api/errors
* Prompt caching - https://platform.claude.com/docs/en/build-with-claude/prompt-caching
* Extended thinking (signatures, passing thinking blocks back) - https://platform.claude.com/docs/en/build-with-claude/extended-thinking
* Adaptive thinking - https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
* Model migration guide (preserved thinking: model and prefix binding) - https://platform.claude.com/docs/en/about-claude/models/migration-guide
* Refusals and fallback - https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback
* Managed Agents overview - https://platform.claude.com/docs/en/managed-agents/overview
* Managed Agents sessions - https://platform.claude.com/docs/en/managed-agents/sessions.md
* Managed Agents events and streaming - https://platform.claude.com/docs/en/managed-agents/events-and-streaming.md
* Managed Agents permission policies - https://platform.claude.com/docs/en/managed-agents/permission-policies.md
* Building effective agents - https://www.anthropic.com/engineering/building-effective-agents
* Effective harnesses for long-running agents - https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
