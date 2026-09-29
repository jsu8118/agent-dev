# Day 4 - Multi-agent orchestration at scale

The first course's Day 4 ended with a lead agent delegating to analyst subagents inside one Python process: twelve
pumps, a spawn cap, an assignment ledger, and a bill 1.65x that of a single agent for the same answer. That harness
was right for its job and would fail at the next one. After go-live a multi-agent system has a different job
description: its work runs for days and must survive crashes and redeploys; its agents book engineers and reserve
parts in systems other people also use; its bill has an owner who wants a cap, not an apology; and when a unit goes
wrong, someone must be able to say which agent, which turn and which tool call.

Today you build that system for Kestrel's recall campaign RC-2026-03 - eleven affected units at seven customers,
three remedy kits - and you build it twice. First self-hosted: a coordinator that is a durable run on Day 1's
runtime, a SQLite work queue with claims, leases and dead letters, worker agents that are durable runs themselves,
shared state guarded by versions and by the system of record, budgets, circuit breakers and loop detection, and typed
agent-to-agent envelopes. Then hosted: the same swarm on Claude Managed Agents, with a coordinator roster, threads, a
shared workspace and custom tools. Finally you evaluate both, and one agent, on the same eleven units, per unit of
work, with failures attributed from the traces - and decide.

The day builds on three earlier ones: the first course's Day 4 (which pattern, and when multi-agent wins or loses),
advanced Day 1 (the durable runtime every agent here runs on), and advanced Day 3 (cache engineering, which decides
what a fan-out costs). The capstone (`advanced/day7_capstone/`) is where the self-hosted branch of today goes next.

## Learning objectives

By the end of the day you can:

1. Build a coordinator that survives its own crash: a durable run whose dispatch is an idempotent effect over a work
   queue - and choose between in-process gather, queue and workers, a workflow engine and a hosted coordinator.
2. Design claims, leases, heartbeats, retries by error class and dead letters for at-least-once delivery, and keep
   side effects exactly-once with idempotency keys and fencing tokens.
3. Guard shared state with compare-and-set, atomic claims and commit-time checks in the system of record, choose a
   merge policy, and recompute aggregates from append-only results.
4. Put budgets (swarm and per agent; tokens, dollars, time), admission control, circuit breakers, loop detection and
   backpressure around a swarm, and bound what each one lets through.
5. Specify agent-to-agent messages as typed, versioned envelopes with owners and correlation ids; choose delegation,
   handoff or broadcast; say what MCP is and is not for between agents.
6. Run the swarm's hosted twin on Claude Managed Agents - versioned agents, environments, sessions, events,
   custom-tool round trips, mounted files, coordinator rosters, threads and session budgets - and say what stays yours.
7. Evaluate a multi-agent system per unit of work: success, cost per success, cost multiplication, coordination
   tokens, largest prompt, critical path, and failure attribution from traces.
8. Decide between one agent, a self-hosted swarm and a hosted swarm with numbers, and know when to build none of them.

## Agenda (about 7 hours)

| Block | Minutes | What |
|---|---|---|
| 1 | 50 | Coordinator patterns after go-live (§1), then Lab 01 - crash the coordinator, resume it |
| 2 | 50 | Shared state and conflicts (§2), then Lab 02 - two threads, one record, one calendar |
| - | 15 | Break |
| 3 | 55 | Swarm economics and safety (§3), then Lab 03 - trip every guard on purpose |
| 4 | 40 | Agent-to-agent protocols (§4), then Lab 04 - delegation, handoff, broadcast |
| - | 40 | Lunch |
| 5 | 50 | Managed Agents (§5), then Lab 05 - a session, a custom tool, a budget |
| 6 | 45 | Hosted coordinators (§6), then Lab 06 - the hosted twin |
| 7 | 45 | Evaluating multi-agent systems (§7) and the decision (§8), then Lab 07 |
| 8 | 30 | The case study as a design review; exercises start |

## Setup

```bash
cd agent-dev && . .venv/bin/activate
python advanced/day4_orchestration_at_scale/labs/01_durable_coordinator.py
python -m pytest tests/test_labs.py -q -k "advanced/day4"          # every lab and solution, in mock mode
```

Without `ANTHROPIC_API_KEY` every lab runs in **mock mode** against labkit's offline mock of the Messages API and of
Managed Agents. The mock validates requests like the real API and answers through rule-based policies in
`advanced/mock_scenarios/day4_orchestration_at_scale.py`: they read the tool results, envelopes and files a model
would see and never the answer key. The queue, the leases, the durable runtime, the desk (the campaign's system of
record), the guards, the bus and the scorer are real code. Three mock behaviours matter today and are flagged `[mock]`
in the output: the stand-ins do not reason (scripted misbehaviours are labelled as such); the hosted mock processes
events synchronously, runs `send_to_agent` to completion before returning, and replays history on the stream; and it
counts no session running time in list cost. Agent versions, mounted files, cached session history and budget
resumption are simulated with the platform's behaviour. Live, the same scripts run against Claude (`claude-opus-5`); the
hosted labs need a workspace with Managed Agents access. Every lab prints its cost - the Messages API ledger at exit,
and a separate line for Managed Agents sessions, whose usage the ledger does not see. The mock's simulated total for
all labs and solutions is about $6; expect the same order of magnitude live, more where Claude thinks longer.

---

## 1. Coordinator patterns after go-live

**The idea.** A coordinator decides what work exists and who does it. After go-live it must also survive its own
death, the death of any worker, work delivered twice, and days of waiting - so the decision "who does what" cannot live
in its memory. Make the coordinator a durable run (Day 1) whose `dispatch_units` tool writes tasks to a **work
queue**, and make every worker a durable run keyed by its task. The queue, not the coordinator, is the system of record
for work in flight; the run logs are the system of record for what each agent did.

**Where it comes from.** Nothing here is new to distributed systems: job queues with a visibility timeout (a claimed
message reappears if it is not deleted in time), worker pools, workflow engines that persist every step as event
history and replay it after a crash, supervision trees that restart what died, and dead-letter queues for messages
that cannot be processed. What is new is that the workers are agents: a task costs cents to dollars, runs for seconds
to minutes, and may do something different when retried.

| | In-process gather (first course Lab 06) | Queue + workers (Labs 01-03) | Workflow engine | Hosted coordinator (Labs 05-06) |
|---|---|---|---|---|
| a crash of the orchestrator | loses every task in flight | the coordinator's run resumes from its log; tasks stay in the queue | resumes by replaying event history | the session and its threads persist on the platform |
| a crash of a worker | its exception comes back to the caller (`return_exceptions=True`); nothing retries it | the lease expires, the task is re-queued, the worker's run resumes | activity retries with policies | the platform retries model calls; your custom tools are yours |
| retries and dead letters | none | by error class; permanent failures dead-lettered at once | declarative retry policies | none for your work items |
| priorities, backpressure | none | claim order by priority; admission control | task queues, rate limits | the lead's turn order is the schedule |
| visibility | stdout | queue statistics, run logs | a UI over history | event stream, threads, Console |
| what you run | nothing | a database and workers | the engine | nothing but your custom tools |
| fits | one-shot fan-out in seconds | campaigns over days, exactly-once effects | many heterogeneous workflows, a platform team | a fan-out that fits one session |

**How it works underneath.** The queue is one table: `task_id` (primary key - an at-least-once producer cannot create a
duplicate), `status` (`queued`, `claimed`, `done`, `dead`), `priority`, `owner`, `lease_until`, `attempts`,
`max_attempts`, `version`. A claim is one atomic statement - `UPDATE ... WHERE task_id = (SELECT ... ORDER BY priority
DESC ... LIMIT 1) RETURNING *` - so two workers can never take the same row. A worker holds a task for a lease and must
renew it; a supervisor's sweep (`reap()`) puts expired claims back. Failures are classified: **transient** (a dead
worker, a timeout, a 429) is retried until `max_attempts`; **permanent** (bad input - the same input will fail the same
way) goes to the dead letters at once, for a human. The coordinator's `dispatch_units` is a Day 1 effect: when the
coordinator dies inside it, the resumed run finds the effect *in flight*, and because the queue is idempotent it
simply runs dispatch again - known tasks are skipped, done tasks are not claimed, and a worker whose run died resumes
**its own** run (run id `unit:<task_id>`) with the tool results it already has replayed. Two levels of durability,
one mechanism.

**Kestrel.** In Lab 01 the coordinator dies after five unit tasks with `dispatch_units` in flight; one worker died
earlier, mid-unit. The second process resumes the same run id, replays `list_affected_units`, re-runs dispatch against
the queue, completes the last two customers, and dead-letters a stray task left in the queue by a dry run - an unknown
serial, which no retry can fix.

## 2. Shared state and conflicts

**The idea.** Agents in a swarm share three kinds of state, and each needs a guard that holds under *any*
interleaving - never a prompt. A **record** several agents update (the campaign's totals) needs a version and
compare-and-set. A **work item** several agents may take (a queue row) needs an atomic claim. A **resource** they
consume (an engineer's slot, the last KC-2 board) needs a uniqueness check at the system of record, at commit time.

**Where it comes from.** Databases: optimistic concurrency control (read a version, write only if it is unchanged),
the `If-Match`/ETag precondition of HTTP APIs, unique constraints, event sourcing (an append-only log and projections
recomputed from it). Managed Agents uses the same idea for its own objects: an agent update that supplies `version`
fails with 409 when someone else updated the agent first.

| Mechanism | Guards | Cost | Fails when | Kestrel use |
|---|---|---|---|---|
| pessimistic lock | anything | waiting; deadlocks; a lock held across a model turn blocks everyone for seconds | a holder dies (needs lease-based locks anyway) | none - agents are too slow to hold locks |
| optimistic concurrency (version, compare-and-set) | records | a retry per conflict; the update must be a pure function | contention is high (retries pile up) | the campaign record (Lab 02) |
| atomic claim | work items | nothing | never, for one row | the queue (Labs 01-02) |
| single writer / ownership | anything | a bottleneck | the owner is down | one planner owns the calendar (Lab 06 `--planners 1`) |
| check at the system of record | scarce resources | the loser re-plans | the checker is bypassed | `book_slot`, `reserve_kit`, `record_plan` (Labs 02, 06) |
| append-only results + recompute | aggregates | storage | never - it is the source of truth | the results table (Lab 02) |

**How it works underneath.** Compare-and-set is `UPDATE records SET data = ?, version = version + 1 WHERE key = ? AND
version = ?`; zero rows updated means someone wrote first, so the writer re-reads and re-applies **its change**, not its
stale copy - which is why the update is a pure function of the current record. The counters in the record are a cache:
the results table keeps every completed attempt, append-only, and the record can always be recomputed from it (the
merge policy: the latest completed attempt of a task wins; counters are never trusted on their own). Leases add a
subtle failure: a worker that was only *slow* wakes up after its task was re-delivered. The queue refuses its
`complete()` (it no longer owns the task), and exercise 6 adds **fencing tokens** so that the system of record refuses
its side effects too. The Day 1 runtime applies the same rule to a run's own lease: when `heartbeat()` finds that
another worker now holds the run, `DurableRunner` raises `LeaseLost` before its next model call instead of writing on.

**Kestrel.** Lab 02 runs two worker threads over eleven tasks and makes the first two record writes collide on purpose:
the invariants hold every time (eleven tasks claimed once, record version 12 = 1 + 11 writes, no slot booked twice),
while the counts vary - including how many bookings the desk refused because the other thread got there first. When
one unit's three attempts are all refused it is escalated as `pending_schedule`, never double-booked: contention costs
retries, and the guard is what keeps it from costing correctness. Lab 06 shows the hosted version of the same problem:
two planner threads read the same snapshot, and the commit refused 4 of their 11 plans.

## 3. Swarm economics and safety

**The idea.** A swarm multiplies tokens: every worker re-reads a prefix, every task needs a brief, every result
comes back as a report the coordinator reads. The first course measured 1.45x the input tokens and 1.65x the cost of a
single agent for twelve pumps. A swarm also multiplies failure modes: one agent looping, one dependency failing, one
burst of work hitting a rate limit. The controls are budgets (for the swarm and per agent; in tokens, dollars and
time), admission control, circuit breakers, loop detection and backpressure - each enforced in code, where the model
cannot talk its way around it.

**Where it comes from.** Error budgets and load shedding (SRE); the circuit breaker and bulkheads of *Release It!*;
token buckets and sliding windows (rate limiting); TCP's congestion control (back off when the network says so).

| Control | Bounds | Trips on | Then | Enforce in |
|---|---|---|---|---|
| swarm budget | dollars (or tokens) for the whole run | spent (+ reserved) reaches the cap | stop dispatching; queued work stays queued; report | the dispatcher, from `LEDGER` or run logs |
| admission control | the overshoot of the swarm budget | spent + reserved + estimate > cap | the next task does not start | the dispatcher (exercise 10 adds reservations) |
| per-agent cap | one agent's dollars / tokens / turns / wall-clock | the agent's own spend | warn as a tool error, then kill the run | the tool executor (`on_call`) |
| circuit breaker | calls to a failing dependency | failures in a window of recent calls | fail fast; after a cooldown one probe decides | the tool executor, per tool |
| loop detector | repeated work | the same call with the same input N times; A->B->A->B | a tool error, then kill | the executor; the message bus |
| backpressure | load on the API and on your systems | a token-rate window; 429 after the SDK's retries | admit in waves; reduce concurrency; re-queue | the dispatcher |

**How it works underneath.** A gate checked before each claim (`spent < cap`) lets every task already running
finish, so it overshoots by up to one task per concurrent worker - a tripwire, not a bound. Admission control spends the
money before the work starts: a task is admitted only if `spent + reserved + estimate <= cap`, and the estimate is
learned from completed tasks. A per-agent guard reads the agent's own run log before each tool call and answers with a
tool error first ("stop exploring and report"), so a cooperative model can land gracefully, then raises a stop the
runtime does not treat as a tool error. A breaker counts failures in a sliding window of recent calls (here 3 of 5),
opens and fails fast without touching the dependency, and after a cooldown lets one probe through (half-open) that
closes or re-opens it. A loop detector hashes (tool, input): the third identical call gets a warning, the fifth kills
the run; at the message level it looks for two agents exchanging the same two messages. Backpressure admits work
against a token-rate window and treats a 429 that survived the SDK's retries as a signal to lower concurrency - never
to spin. One more fan-out cost from Day 3: N workers that start at the same instant all *write* the shared prefix to
the cache (an entry becomes readable only once its writer has started responding); pre-warm it with one
`max_tokens: 0` request first (exercise 5 does the arithmetic).

**Kestrel.** The campaign itself states the rule: "model spend reaches the cap: pause and report" (`campaign.json`,
cap $25.00). Lab 03 trips every control once, and shows the naive gate crossing a $0.20 cap by $0.0278 while
admission control stops inside it.

## 4. Agent-to-agent protocols

**The idea.** When an agent sends work or a conversation to another agent, the message is an API between two
components, and it deserves what any API gets: a schema, a version, validation before delivery, routing, an owner, and
correlation between request and reply. Three patterns cover almost every case: **delegation** (ask for a result, keep
the conversation), **handoff** (give the conversation, and the state it needs, to another owner) and **broadcast**
(tell many; nobody new owns anything).

**Where it comes from.** Message envelopes, correlation identifiers and routing slips (enterprise integration
patterns); the actor model (an actor owns its state; others send it messages); and two protocols you will meet by
name. **MCP** connects *one* agent to tools and resources: a server offers tools, the agent calls them, and it is the
right way to put the desk, the calendar or the ERP in front of any agent in the swarm. It is not a protocol for tasks
between agents - there is no owner, no conversation, no task lifecycle. **A2A-style** agent-to-agent protocols add
exactly those: an agent card per agent (who it is, what it accepts), tasks with states, messages with typed parts. The
envelope in Lab 04 is a minimal version of that idea, in forty lines of JSON Schema.

| | Delegation | Handoff | Broadcast |
|---|---|---|---|
| after the message, the customer's conversation belongs to | the sender | the receiver | the sender |
| replies expected | one result | one acceptance | an ack per recipient |
| the payload must carry | the question and the context to answer it | the state the new owner needs: open actions, constraints, what the customer was told | the scope (lot, remedy, customer) and the affected items |
| cost over time | the sender stays in the middle of every later exchange | one transfer; later messages go straight to the owner | one call per recipient, once |
| use when | you need an answer and will keep the customer | someone else must own what happens next | many must know; nobody new must answer |

**How it works underneath.** The Lab 04 envelope has `schema_version`, `id`, `type`, `from`, `to`,
`conversation_id`, `conversation_owner`, `task_id`, `in_reply_to`, `payload`, `sent_at` (and an optional `ttl_s`),
and each message type has its own payload schema - a handoff without `state` is not a handoff. The bus enforces four
rules before any agent spends a token: the schemas; **ownership** (only the conversation's owner may delegate, hand off
or write to the customer, and the ledger changes owner when the receiver accepts a handoff); **capabilities** (the
recipient's agent card must accept the type); **correlation** (a result must answer an open delegation). Agents only
produce payloads; the harness writes routing, ownership and correlation fields, so a model cannot reroute a
conversation by writing a field. Messages are keyed by `id`, so a redelivered envelope is recognisably the same one.

**Kestrel.** RPL-018 - an operator sprayed with process fluid at Harbor Foods - is a campaign stop condition. Lab 04
handles it three ways and then rejects five bad messages: a worker answering the customer itself (with a promise of a
credit), the coordinator still answering after the handoff, a handoff with no state, a handoff to a unit worker, and a
result nobody asked for.

## 5. Managed Agents: the hosted runtime

**The idea.** Claude Managed Agents runs the agent loop and a per-session container for you. You create an **agent**
(model, system prompt, tools: persisted and versioned), an **environment** (a container template), and a **session**
per run; you send events and read an event stream. The SDK namespaces are `client.beta.agents`,
`client.beta.environments`, `client.beta.sessions` (with `.events` and `.threads`), and the SDK sends the beta header
`managed-agents-2026-04-01` for you. It is a beta surface: pin your SDK version, read the release notes, and expect the
header - and possibly some shapes - to change when the beta ends; the labs will then need the header dropped and any
renamed field updated.

**When to use it, and the neighbours.**

| | Messages API + your loop (Day 1 runtime) | SDK tool runner | Managed Agents | Claude Agent SDK |
|---|---|---|---|---|
| you write | the loop, persistence, retries | tool functions | agent config + your custom tools | a prompt and options |
| harness (loop, context) | yours | the SDK's | the platform's | the library's (Claude Code) |
| where tools run | your process | your process | a container per session (built-ins) and your process (custom tools) | your machine |
| state across crashes | your run log | none | the session's event history | local sessions |
| fits | exactly-once effects, days-long runs, full control | a custom-tool agent without hand-writing the loop | hosted, sandboxed, long-running agents; fan-out in one session; a hard cost cap | a coding or filesystem agent on your infrastructure |

**How it works underneath.**

* **Agents.** Create once and reference by id; an update (`POST /v1/agents/{id}`) creates a new immutable version, and
  supplying `version` makes the update conditional (409 if it changed). A session pins the version it starts with
  (`agent={"type": "agent", "id": ..., "version": ...}`), so running sessions are not affected by a prompt change.
  Never `agents.create()` on every run: look the agent up (the labs' `get_or_create_agent`) or manage it as a file.
* **Environments.** `config: {"type": "cloud", "networking": {"type": "unrestricted" | "limited", ...}}`; names are
  unique (a second create is a 409).
* **Sessions.** `sessions.create(agent=..., environment_id=..., title=, metadata=, resources=, initial_events=,
  budget=)`. Without `initial_events` a session is registered `idle`; with them it starts `running`. Statuses: `idle`,
  `running`, `rescheduling` (after a retryable error), `terminated`.
* **Events.** You send `user.message`, `user.custom_tool_result`, `user.interrupt`, `system.message`,
  `user.define_outcome`; you receive `agent.message`, `agent.thinking`, `agent.tool_use`/`agent.tool_result` (the
  built-in toolset), `agent.custom_tool_use`, `span.model_request_start`/`_end` (with `model_usage`), `session.usage`,
  `session.status_running`/`_idle` (with a `stop_reason`: `end_turn`, `requires_action`, `retries_exhausted`,
  `budget_reached`), and the thread events of §6.
* **Custom tools** are yours: `{"type": "custom", "name", "description", "input_schema"}`. The agent's call arrives as
  `agent.custom_tool_use`; the session goes idle with `stop_reason: {"type": "requires_action", "event_ids": [...]}`;
  you answer each id with `user.custom_tool_result` (`custom_tool_use_id`, `content`) - several in one send - and the
  session resumes.
* **The built-in toolset** `{"type": "agent_toolset_20260401"}` gives `bash`, `read`, `write`, `edit`, `glob`, `grep`,
  `web_fetch`, `web_search`, running in the session's container; `default_config` and per-tool `configs` enable tools
  individually and set permission policies (`always_allow`, `always_ask`, `auto`).
* **Streaming.** `events.stream(session_id)` is SSE **without replay**: open it before you send, and on every
  (re)connect also fetch `events.list()` and de-duplicate by event id - otherwise a dropped connection while a custom
  tool call is pending leaves the session waiting for an answer that never comes. The labs' `drive_session` does this.
  One more reason from this SDK version: the typed stream in SDK 1.8 does not yield `session.usage` events; the
  history does.
* **Usage and budgets.** Each `span.model_request_end` carries that request's tokens; `sessions.retrieve().usage`
  carries the totals and `list_cost` (cents, as an integer string, rounded; list prices, including $0.08 per
  session-hour of running time). The session caches its own history - nothing to configure - so after the first
  request most of a prompt is billed as cache reads. A **session budget** (`{"type": "limit", "max_list_cost":
  {"amount": "2500", "currency": "USD"}}`, create-only) is checked before every model request; the request that
  crosses the cap completes, so a session overshoots by at most one request per running thread. At the cap the session
  goes idle with `budget_reached` and accepts only settle events (tool results, tool confirmations, interrupts); a
  `user.message` is a 400; only a budget update (raised above the consumed cost, or removed with `budget: null`)
  resumes it.
* **Files.** Upload with the Files API and mount read-only at session creation (`resources=[{"type": "file",
  "file_id": ..., "mount_path": "/workspace/..."}]`); agents write outputs to `/mnt/session/outputs/`.

| Concern | Managed Agents gives you | Stays yours |
|---|---|---|
| the loop and retries of model calls | yes (`rescheduling`) | - |
| conversation state and history | yes (events per session and thread) | - |
| a sandbox for agent-written code | yes (a container per session) | hardening your own, if you self-host |
| cost visibility and a hard cap | per session and thread (`usage`, `list_cost`, `budget`) | per-agent and campaign-wide caps |
| side effects on your systems | - | custom tools: idempotency keys, fencing, validation |
| policy and approvals | - | the custom-tool executor, approvals (Day 1) |
| evaluation | outcomes can grade a session against a rubric | your eval sets, release gates (Day 6) |

**Live only.** Vaults (credentials stored by Anthropic and substituted at egress, never visible in the container),
outcomes (`user.define_outcome` with a rubric and a grader), scheduled deployments, MCP servers on agents, webhooks,
memory stores and self-hosted sandboxes are not simulated by the mock; read them in the Managed Agents docs listed
under Further reading before you rely on them.

**Kestrel.** Lab 05 moves the unit assessor onto the platform: two editors' conditional updates of the agent meet a
409, `get_unit` is a custom tool answered by Kestrel's desk, the assessment is written into the container with the
built-in `write` tool, and a second session with a 6-cent budget stops after four of six units, refuses new work at
the cap, and finishes the six once its budget is raised.

## 6. Hosted coordinators

**The idea.** A coordinator is an agent created with `multiagent={"type": "coordinator", "agents": [...]}`. Its roster
holds 1-20 entries - other agents by id (optionally pinned to a version), `{"type": "self"}` (copies of the
coordinator), and at most one advisor model. The coordinator's thread gets two tools, `list_agents` and
`send_to_agent`; every delegated task runs in a **thread**: its own context, its own agent config, persistent (a
follow-up with the same `thread_id` continues it), and the **same container filesystem** as every other thread. One
level of delegation only: an agent whose roster contains a coordinator is refused. At most 25 threads run
concurrently, and the session's one budget is shared by all of them.

**When to use it.** A fan-out that fits one session (hours, not weeks); reading-heavy sub-tasks that would fill one
context (give them to threads, keep only their reports); cheaper worker models for bulk reading; sandboxed code. Not
when you need a work queue across sessions and days, priorities and dead letters, custom retry semantics or a hierarchy
deeper than one level - that orchestration stays yours, and the hosted coordinator becomes one level of it.

| | Hosted coordinator (Lab 06) | Self-hosted coordinator (Lab 01) | One hosted agent with `self` copies |
|---|---|---|---|
| unit of isolation | a thread | a durable run | a thread |
| shared state | one container filesystem, no transactions | your database and desk | one filesystem |
| scheduling | the lead's turn order | the queue: priority, leases, retries | the lead's turn order |
| where your policy runs | custom tools on the lead (and on workers, cross-posted to the primary stream) | your tool executor | custom tools |
| cost cap | the session budget | your guards | the session budget |
| depth | one level | anything you build | one level |

**How it works underneath.** The session-level stream is the primary thread: the coordinator's own events plus
`session.thread_created`, `agent.thread_message_sent` / `_received` and thread status changes, not every tool call of
every worker (each thread has its own stream and event list). `threads.list()` shows each thread's agent, parent and
usage. On the platform `send_to_agent` returns at once and the worker's report arrives in a later coordinator turn;
custom tool calls made inside a worker thread are cross-posted to the primary stream with a `session_thread_id` that
your answer echoes. Two design rules follow from "one filesystem, no transactions". Pass bulk data **by reference**:
mount it as files at session creation instead of having the coordinator fetch it and write it out again as model
output (the most expensive tokens you buy). And treat anything two threads read from a shared snapshot as possibly
stale: validate at commit, in your code.

**Kestrel.** Lab 06's lead lists the units through a custom tool, sends three investigator tasks and two parallel
planner tasks, commits through `record_plan` (the desk validates every plan), and re-plans the four plans the commit
refused - in the first planner's existing thread, from a fresh snapshot. With one planner owning the calendar
(`--planners 1`) the same result takes 16 requests instead of 22.

## 7. Evaluating multi-agent systems

**The idea.** Evaluate a swarm **per unit of work**, not per run: a coordinator's upbeat summary is not evidence, and a
unit that is missing from every plan cannot fail a check that only reads plans. Then measure what the split costs and
buys, and when something goes wrong, attribute it - to an agent, a turn, a tool call or a message - from the traces.

**Where it comes from.** Evals from the first course's Day 6 and this course's Day 6 (a checker in code, never the
agent grading itself); distributed tracing (one trace per request, spans per step; OpenTelemetry's GenAI conventions);
blameless postmortems (find the step, not a culprit). Anthropic's account of its multi-agent research system adds two
observations that apply here: token usage explained most of the variance in quality there, and multi-agent systems
used about 15x the tokens of a chat.

| Metric | Definition here | Why it matters |
|---|---|---|
| success per unit of work | units booked correctly / units in the campaign (`check_plan` against the dataset, the rules and the desk's bookings) | an honest "pending" is escalated, not success; a missing unit is a failure |
| cost per success | cost as run (caching included) / successful units | cheap and wrong is not cheap |
| cost multiplication | cost / cost of one agent on the same task | the first course's 1.65x, made routine |
| coordination tokens | coordinator tokens + brief tokens x worker turns + report tokens | tokens that exist only because the work was split |
| largest prompt | the biggest single request | context isolation, and headroom |
| critical path | modelled: sequential turns + the slowest parallel branch | what a customer waits for |
| incidents repaired | conflicts rejected at commit and re-planned | failures that cost money even when the result is right |

**How it works underneath.** The scorer is code: expected properties per unit (customer, kit, contact, deadlines)
come from the dataset and the campaign rules, and a "scheduled" plan must match a real booking on the desk, in the
region, with the skill, on or before `remedy_by`. Attribution walks back from each unit's final artifact: to the
booking, to the tool result it came from, to the parameters of that call - and stops at the first step whose input does
not belong to this unit or this moment. The durable run logs (one per agent, one event per step) and the session's
event stream *are* the traces; debugging a swarm becomes a query. Two caveats keep the numbers honest: in mock mode
the agents are stand-ins, so success measures architecture, not model quality; and costs are compared as run, with
prompt caching on both sides, next to the same tokens priced uncached - how much of each bill the cache carries.

**Kestrel.** Lab 07 scores five configurations on the same eleven units and attributes six incidents: the two late
safety bookings of the single agent, and the four plans the hosted swarm's second planner took from a stale snapshot.

## 8. The decision: one agent, a self-hosted swarm or a hosted swarm

Start from the task, then the numbers, then ownership.

| | One agent | Self-hosted swarm (tuned) | Hosted swarm (one planner) |
|---|---|---|---|
| success (Lab 07) | 9/11 - two safety units booked late | 11/11 | 11/11 |
| $ per success, as run | $0.0376 | $0.0323 | $0.0278 |
| cost vs one agent, as run | 1.00x | 1.05x | 0.90x |
| coordination share | 0% | 13% | 67% |
| largest prompt | 8,223 | 3,266 | 7,591 |
| critical path (modelled) | 215 s | 79 s | 153 s |
| survives a crash / waits for days | only with Day 1's runtime | yes: queue + durable runs | within a session; days and approvals are yours |
| you operate | a runtime | a queue, workers, guards | custom tools and policy |
| choose it when | few, similar items that fit one context | work spans days, crashes, approvals; exactly-once effects; priorities | a session-sized fan-out; a sandbox; a hard cap; no agent infrastructure |

Three rules of thumb come out of the labs and exercise 3.

1. **Split the work when isolation or parallelism buys something you can measure** - here two safety units booked on
   time and a critical path cut from 215 s to 79 s. In dollars, with caching, eleven units do not justify a swarm:
   exercise 3's model puts the break-even at 14 units for the tuned swarm and 54 for the naive one, because caching
   prices exactly the single agent's quadratic re-reading at a tenth.
2. **Fix the coordination bill before you scale it.** The naive swarm spent 40% of its tokens on coordination, 4.7x
   what the tuned one needed for the same result; the hosted swarm's parallel planners needed six more requests and
   1.7x the cost of one planner.
3. **Choose where the orchestration runs by what you must own, and how the work is shaped by the numbers.** The
   hosted swarm is the cheapest row because of its work shape - workers read a mounted snapshot in bulk and one commit
   books everything - not because it is hosted; a self-hosted swarm can be shaped the same way. And for many
   workloads the right answer is no coordinator at all: 400 independent claims are a loop in code (exercise 8).

---

## Case study: Kestrel's recall investigation

**Requirements.** Recall RC-2026-03 covers eleven installed units at seven customers: nine pumps with seal cartridges
from lot PS-2608-B (a fluid-release hazard on safety duty) and two KC-2 controllers with boards from lot VD-2607-C.
Three remedy kits (MS-250-R, MS-100-R, KC-2-PSB) in three warehouses, six field engineers with calendars. Every unit
needs a kit reserved and an engineer booked in its region, with the right skill, before its deadline: 5 business days
for safety units, 10 for production, 15 for standard. Contact the site engineer when the purchasing contact is out of
office; never promise compensation; stop on an injury report; stay inside the model-spend cap.

**Attempt 1: one agent.** A single agent with the worker tools planned all eleven units in one conversation, 34 turns.
It was cheap as run ($0.3386), and its largest prompt (8,223 tokens) was nowhere near a context limit - the "blow-up"
was not size but interference. Eleven units with three different deadlines shared one context, and when it reached the
last safety units of Midland Oil it booked them from a slot search it had made for a production unit (Lab 07's trace
shows the turn-21 search with `not_after=2026-09-30` reused at turns 30 and 32): KP250-2608-0007 and -0008 were booked
for 24 September, a day after their safety deadline. In mock mode that shortcut is scripted and labelled; live, it is
the kind of cross-item contamination you measure rather than assume. The cost shape was the other warning sign: 186,860
tokens processed for eleven units, growing with the square of the campaign.

**Attempt 2: a swarm, built the obvious way.** One worker per unit (isolation fixed the contamination: 11/11), a
coordinator that pasted the campaign notice and the method into every brief, workers that wrote narrative reports, and
a coordinator that read them in full. It cost 2.15x the single agent as run, and 40% of everything it processed was
coordination: 58,772 tokens of coordinator work, briefs re-read on every worker turn, and reports - 4.7x what the job
needed. Exercise 3's model of exactly this configuration predicts about 3x the single agent's bill (2.95x).

**Attempt 3: fixed.** Batches per customer (one risk class, one deadline per worker context), a one-line brief (the
notice already sits in every worker's cached system prompt), compact JSON reports, a coordinator that reads a digest of
plans rather than the reports, priorities from the risk class in the queue, and every side effect idempotent per serial
at the desk. Same 11/11; coordination down to 13% of tokens; 1.05x the single agent's bill as run; largest prompt 3,266;
modelled critical path 79 s instead of 215 s. It also survives the things a campaign that runs for two weeks will
meet: Lab 01 kills it mid-dispatch and mid-unit and it resumes without repeating a call.

**The hosted twin.** On Managed Agents the same design needed less infrastructure and one new lesson. With two
planner threads working from one snapshot, the commit refused four plans (three slots and the last KC-2 board at
WH-EAST); the lead re-planned them from a fresh snapshot in an existing thread and reached 11/11 - at 22 requests
against 16 for a single planner. Threads share a filesystem, not transactions: the system of record, behind a custom
tool, is what kept the calendar consistent.

**What changed in the team's practice.** Units are scored one by one against the campaign's list, not by the
coordinator's summary; every failure is attributed from the logs before anyone touches a prompt; coordination tokens
are a dashboard line; budgets are admission control, not a tripwire. The capstone takes the tuned self-hosted design
into multi-day outreach with hostile inbound mail and approvals that wait.

---

## Lab walkthrough

All excerpts are from mock mode; live runs print the same structure with Claude's wording and real token counts.
Timings printed as wall-clock vary; the quoted numbers are the deterministic ones.

### Lab 01 - `01_durable_coordinator.py`: a durable coordinator, crashed and resumed

`python advanced/day4_orchestration_at_scale/labs/01_durable_coordinator.py [--crash-after 5] [--profile tuned|naive]`

1. Prints the campaign and the claim statement; a stray task from a dry run already sits in the queue.
2. The first coordinator process dispatches seven customer batches by priority; one worker dies after two tool calls
   and is retried after its lease expires; the coordinator dies after five completed tasks, inside `dispatch_units`.
3. A second process resumes the same run id: the dispatch effect is in flight, the queue makes re-running it safe, the
   stray task is dead-lettered as a permanent failure, and the coordinator writes its summary.
4. The coordinator's log, every unit run with attempts, replayed and executed tool calls, and the dead letter.

```
    !! worker coordinator-1/w1 died on batch:KP250-2608-0004 (attempt 1): worker died before check_parts (call #3)
    supervisor: lease expired -> re-queued ['batch:KP250-2608-0004']; queue={'done': 4, 'queued': 4}
    task batch:KP250-2608-0004 done by coordinator-1/w2: KP250-2608-0004 scheduled (resumed: 2 tool results replayed)

  !! coordinator process died after 5 unit tasks
  run status=running, events=7, last=tool.started (dispatch_units)
  queue after the crash: {'done': 5, 'queued': 3}
...
    dispatch_units: 0 new batch task(s) enqueued, 7 already known; queue={'done': 5, 'queued': 3}
    task batch:KP250-2608-0099 -> dead (dead letter): the desk has no record of KP250-2608-0099; retrying the same input cannot help
    task batch:KP100-2608-0001 done by coordinator-2/w1: KP100-2608-0001 scheduled
    task batch:KP250-2608-0005 done by coordinator-2/w1: KP250-2608-0005 scheduled

  resumed run: status=completed turns=4 replayed_tools=1 executed_tools=2; unit tasks completed in this process: 2
...
  KP250-2608-0004                                  done          2         completed   4      2         4
  KP250-2608-0099                                  dead          1         completed   -      -         -
```

What to look for: the resumed coordinator made model calls only for the turns it had not logged (turns 3 and 4); the
worker that died resumed its own run with two results replayed; the stray task was not retried three times, because
retrying bad input cannot help. `--profile naive` runs the case study's first swarm (one worker per unit, long briefs,
full reports). *Live, watch:* the batches Claude chooses (the queue's primary key protects you from a re-run only if
the resumed turn is replayed from the log, not regenerated), and the per-unit cost in the usage summary.

### Lab 02 - `02_work_queues_and_shared_state.py`: two threads, one record, one calendar

`python advanced/day4_orchestration_at_scale/labs/02_work_queues_and_shared_state.py`

1. Three scripted interleavings: a stale compare-and-set write, a lost claim race, and a double booking the desk refuses.
2. Two worker threads drain eleven unit tasks and update the campaign record; their first writes are made to collide.
3. At-least-once delivery: a producer's duplicate, a consumer re-running a completed run, a lease that expires while
   its worker sleeps - and the sleeper's late `complete()`.
4. Totals recomputed from the append-only results table against the record.

```
  A writes units_done=1 expecting version 1 -> ok (record now version 2)
  B writes units_done=1 expecting version 1 -> CONFLICT: 0 rows updated
  B re-reads and re-applies its change (a pure function) -> version 3, units_done=2 (a blind overwrite would have left 1: the lost update)
...
  A books FSE-01-2026-09-21-08 for KP250-2608-0006 -> BK-001
  B books FSE-01-2026-09-21-08 for KP250-2608-0007 -> refused: slot FSE-01-2026-09-21-08 is no longer free (status booked)
  B takes the next slot from its search, FSE-02-2026-09-21-08 -> BK-002
...
  Invariants (hold under ANY interleaving):
  * queue: {'done': 11}
  * 11 tasks claimed exactly once: True
  * record version 12 = 1 + 11 writes (one successful compare-and-set per task; retries do not add versions)
  * record: units_done=11, kits={'WH-EAST': 8, 'WH-EU': 1, 'WH-WEST': 2} - one reservation per unit
  * no slot booked twice: True
...
  consumer bypasses the queue and re-runs the unit's run id -> status=completed, model calls made: 0 (the durable run is complete; its reply is returned)
...
  A wakes up and tries to complete it -> False (A no longer owns it; only B's result will count)
...
  recomputed from results: units_done=11 kits={'WH-EAST': 8, 'WH-EU': 1, 'WH-WEST': 2} -> agrees with the record: True
```

The per-thread claim counts, the record conflicts (the first collision is forced, so there is always at least one)
and the line that starts "Varies with the interleaving" change from run to run - between none and a few refused
booking attempts, and now and then a unit escalated as `pending_schedule`; the invariants never change.
*Live, watch:* the threads spend real seconds in model calls, so collisions become rarer - which is exactly why the
guard must not depend on them being rare.

### Lab 03 - `03_budgets_circuit_breakers_loops.py`: every guard, tripped on purpose

`python advanced/day4_orchestration_at_scale/labs/03_budgets_circuit_breakers_loops.py`

1. A per-agent cap on a worker scripted to keep searching: warned by a tool error, then killed.
2. A $0.20 swarm budget, first behind the naive gate, then behind admission control.
3. A circuit breaker on `find_engineer_slots` during a scheduling outage: open, fail fast, half-open probe, closed.
4. Loop detection on a worker scripted to repeat one call, and ping-pong detection between two agents.
5. Backpressure: a 40,000 tokens/minute window, and a 429 that survives the SDK's retries.

```
  warning delivered as a tool error: {"error": "Budget: this agent has spent $0.0506 of its $0.05 cap. Stop exploring and report the plan from what...
  outcome: KILLED -> run status failed: agent budget exceeded ($0.0576 > $0.05) after the warning was ignored
...
  naive gate: spent < cap                      7 units done, $0.2278 spent (over the cap by $0.0278); stopped: $0.2278 spent >= $0.20; queue={'done': 7, 'queued': 4}
  admission control: spent + estimate <= cap   7 units done, $0.1869 spent (within the cap); stopped: next unit needs ~$0.0273, $0.0131 left; queue={'done': 7, 'queued': 4}
...
  3  KP100-2608-0001  1           open           pending_schedule
  4  KP100-2608-0002  0           open           pending_schedule
  5  KP250-2608-0002  0           open           pending_schedule
  6  KP250-2608-0003  1           closed         scheduled
  7  KP250-2608-0004  1           closed         scheduled
  transitions: closed->open | open->half_open | half_open->closed
...
  outcome: KILLED -> failed: loop detected: get_unit called 5 times with identical input
  ping-pong between agents (message-level): ping-pong: coordinator<->worker-1 exchanging the same two messages (f3f5e550/62b731cd)
```

Both budget passes finished seven units; the difference is where they stopped - one after crossing the cap, one before
(the second pass is also cheaper per unit because it reads the worker prefix the first pass cached). The misbehaving
workers are scripted `[mock]` stand-ins; the guards are real. *Live, watch:* whether Claude heeds the per-agent
warning and lands gracefully (the hard stop should then never fire), and how often real 429s reach the dispatcher at
your rate-limit tier.

### Lab 04 - `04_agent_to_agent_protocols.py`: delegation, handoff, broadcast

`python advanced/day4_orchestration_at_scale/labs/04_agent_to_agent_protocols.py`

1. The envelope schema, the payload schemas per type, the agent cards; a malformed envelope and its four problems.
2. The trigger: RPL-018, an injury report from Harbor Foods, and the customer's follow-up a day later.
3. Delegation: the coordinator asks the quality lead twice and relays both answers.
4. Handoff: the quality lead accepts the conversation with its state; the follow-up is routed to it directly.
5. Broadcast: a lot pause to three unit workers, three acks, no customer reply.
6. Five rejected messages and the comparison.

```
  worker-2 answers the customer itself: rejected - ownership: worker-2 may not send 'outbound' on RC-2026-03/C-1005 (quality-lead owns it)
  the coordinator keeps answering after the handoff: rejected - ownership: coordinator may not send 'outbound' on RC-2026-03/C-1005 (quality-lead owns it)
  a handoff with no state: rejected - payload: 'state' is a required property
  a handoff to a unit worker: rejected - capability: worker-1 does not accept 'handoff' (its card: delegation, broadcast)
  a result nobody asked for: rejected - correlation: 'result' must answer an open 'delegation' (in_reply_to)

  pattern     envelopes  model calls  input tok  output tok  cost     owner after   customer answered by
  ----------  ---------  -----------  ---------  ----------  -------  ------------  ---------------------------------
  delegation  8          6            1,544      1,188       $0.0374  coordinator   coordinator (relaying)
  handoff     7          3            704        621         $0.0190  quality-lead  quality-lead
  broadcast   5          4            1,017      726         $0.0232  coordinator   nobody - a broadcast owns nothing
```

The same event and the same follow-up cost half as much as a handoff than as a delegation, because after a handoff
the coordinator is no longer in the middle; the broadcast informed everyone and answered no one. In practice the
injury report needs a handoff *and* a broadcast. *Live, watch:* what Claude puts into the handoff's `state` when the
schema requires it - and what it would have left out without the schema.

### Lab 05 - `05_managed_agents_sessions.py`: a hosted agent, a custom tool, a budget

`python advanced/day4_orchestration_at_scale/labs/05_managed_agents_sessions.py`

1. Looks up or creates the agent (custom `get_unit` + the built-in toolset) and the environment; creates a session
   pinned to the agent's version; then two editors update the agent from the same version.
2. Opens the stream, sends a user message, and reads until the session stops for the custom tool.
3. Answers it from Kestrel's desk and drives the session to the end of its turn (stream plus history, de-duplicated).
4. Reads usage per request and per session.
5. Runs a second session with a 6-cent budget into its cap, tries to add work at the cap, and raises the budget.
6. Prints what the hosted runtime takes over and what stays yours; archives the sessions, not the agent.

```
  field-service ops: update(version=1, metadata) -> version 2
  quality, still on version 1: update(version=1) -> 409: version conflict: the agent is at version 2, the update was based on version 1; re-read it and retry
  quality re-reads (version 2), re-applies its change -> version 3
  agents.versions.list: 3 immutable versions; the session still runs version 1 - an update never reaches a running session
...
    agent.custom_tool_use            get_unit({"serial": "KP250-2608-0006"})
    session.status_idle              stop_reason=requires_action event_ids=['sevt_mock_000028']
...
    agent.tool_use                   write({"path": "recall/KP250-2608-0006.json", "content": "{\n \"serial\":...) permission=allow
    agent.tool_result                wrote recall/KP250-2608-0006.json
...
  3 span.model_request_end events: 3,063 prompt tokens (input 0, cache write 1,190, cache read 1,873) and 678 output
  sessions.retrieve().usage has the same totals: True; list_cost 3 cents (USD; rounded to the cent)
  at list prices: $0.0253 as run, $0.0323 if nothing had been cached
...
  budget 6 cents -> stop_reason=budget_reached after 8 model requests; 4 of 6 assessments written (KP250-2608-0007, KP250-2608-0008, KP100-2608-0002, KP250-2608-0002)
  consumed list cost: 7 cents (exact $0.0678): the request that crossed the cap completed, the next one was never made
  a user.message at the cap -> 400: session budget reached (budget_reached); raise the budget with sessions.update before sending more events
...
  sessions.update(budget=27 cents) -> session.updated (budget 27 cents), and the paused turn resumed:
  stop_reason=end_turn; 6 of 6 assessments written, 13 model requests, list cost 11 cents
```

The agent update is lab 02's compare-and-set on a platform resource: an update that names the version it was based
on either lands or gets a 409, and the loser re-reads and re-applies its change - while the session keeps the version
it was pinned to. Note the step 3 line explaining where `session.usage` came from: the typed stream skipped it and the
history supplied it, which is the consolidation pattern doing its job. The session caches its own history, so after
the first request its prompt is mostly cache reads. The budget stopped the second session after the request that
crossed six cents; a `user.message` was refused, and only the budget change - based on the consumed list cost, not
the old cap - resumed the paused turn. The mock's 400 message is its own; the platform's names the settle events it
accepts. *Live, watch:* the `session.status_running` -> `idle` rhythm between custom tool calls, the Console's view
of the session and of the agent's versions, and the real list cost including running time.

### Lab 06 - `06_managed_coordinator.py`: the hosted twin

`python advanced/day4_orchestration_at_scale/labs/06_managed_coordinator.py [--planners 2]`

1. Creates the roster (a read-only investigator, a read-only planner, the lead itself) and shows that a coordinator of a
   coordinator is refused.
2. Uploads the campaign data and a resources snapshot, mounts them in the session, and drives the session, printing the
   primary stream.
3. Shows the commits: what the system of record rejected and why.
4. Lists the threads with their usage.
5. Compares the run with lab 01's tuned swarm on the same eleven units.

```
  a 'campaign-director' whose roster holds recall-lead -> 400: multiagent.agents: only one level of delegation is allowed; 'recall-lead' carries its own roster
...
  session.resources - Files API uploads mounted into the container at creation: /workspace/campaign/units.json (10 kB), /workspace/campaign/resources.json (11 kB)
...
  record_plan #1: 7 recorded (7 scheduled), 4 rejected
    - KP100-2608-0002: slot FSE-01-2026-09-21-08 is no longer free (status booked)
    - KP250-2608-0007: slot FSE-02-2026-09-21-08 is no longer free (status booked)
    - KC2-2608-0002: KC-2-PSB out of stock at WH-EAST; stock elsewhere: {"WH-WEST": 1}
    - KP250-2608-0003: slot FSE-01-2026-09-21-13 is no longer free (status booked)
  record_plan #2: 4 recorded (4 scheduled), 0 rejected
...
  agent              thread   status  prompt tok  cache read  output tok  list cost
  -----------------  -------  ------  ----------  ----------  ----------  ---------
  recall-lead        primary  idle    62,642      50,252      6,702       27c
  unit-investigator  child    idle    4,379       785         829         4c
  unit-investigator  child    idle    4,337       764         468         3c
  unit-investigator  child    idle    4,337       764         468         3c
  schedule-planner   child    idle    11,118      5,434       1,477       8c
  schedule-planner   child    idle    5,288       1,149       839         5c
  recall-lead        child    idle    2,181       0           160         2c
  session: 22 model requests, 94,282 prompt tokens (59,148 read from the cache) and 10,943 output; list_cost 52 cents of a 200-cent budget (exact $0.5227)
...
  architecture          scheduled  requests  prompt tok  output tok  cost     cost uncached  latency*  largest prompt
  --------------------  ---------  --------  ----------  ----------  -------  -------------  --------  --------------
  self-hosted (lab 01)  11/11      41        84,000      9,764       $0.3548  $0.6641        79s       3,266
  hosted (this lab)     11/11      22        94,282      10,943      $0.5227  $0.7450        233s      12,390
```

The first planner's thread carries twice the tokens of the second because it was asked to re-plan - threads persist,
and the follow-up re-read its first task. The last row is the QA copy of the lead (the roster's `self`). Every thread
caches its own history: the lead, which re-reads its growing conversation on every turn, pays for four fifths of its
prompt at the cache-read price. In step 5 the self-hosted swarm needs 41 requests and $0.3548, the hosted one 22
requests and $0.5227: fewer, bigger requests, because the lead is the hub every report and commit passes through, plus
the re-plan the conflict cost. `--planners 1` removes the conflict (16 requests, $0.3060 - lab 07's last row). *Live,
watch:* the asynchronous rhythm - `send_to_agent` returning at once and the reports arriving in later coordinator
turns - and the threads' own streams in the Console.

### Lab 07 - `07_multi_agent_eval_and_decision.py`: the evaluation and the decision

`python advanced/day4_orchestration_at_scale/labs/07_multi_agent_eval_and_decision.py`

1. States the task and the scorer.
2. Runs five configurations on the same eleven units and prints the scoreboard and the coordination bill.
3. Attributes every failure and every repaired incident from the traces.
4. Prints the decision table and the rules of thumb, with the numbers computed in steps 2-3.

```
  configuration              success  requests  tokens processed  cost as run  cost uncached  $ per success
  -------------------------  -------  --------  ----------------  -----------  -------------  -------------
  one agent                  9/11     34        186,860           $0.3386      $1.0959        $0.0376
  self-hosted swarm, naive   11/11    48        147,040           $0.7267      $1.1380        $0.0661
  self-hosted swarm, tuned   11/11    41        93,764            $0.3548      $0.6641        $0.0323
  hosted swarm, 2 planners   11/11    22        105,225           $0.5227      $0.7450        $0.0475
  hosted swarm, one planner  11/11    16        56,002            $0.3060      $0.4396        $0.0278
...
  cost multiplication over one agent, as run: naive 2.15x, tuned 1.05x, hosted 1.54x (two planners) and 0.90x (one planner)
  uncached, one agent would cost 3.2x its bill and the tuned swarm 1.9x: the cache prices exactly the single agent's re-reading of its context
...
  configuration              coordination tokens  share  made of                                             largest prompt  latency*
  -------------------------  -------------------  -----  --------------------------------------------------  --------------  --------
  one agent                  0                    0%     -                                                   8,223           215s
  self-hosted swarm, naive   58,772               40%    28,936 coordinator + 23,584 briefs + 6,252 reports  11,486          163s
  self-hosted swarm, tuned   12,459               13%    9,190 coordinator + 887 briefs + 2,382 reports      3,266           79s
  hosted swarm, 2 planners   79,032               75%    71,685 coordinator + 5,131 briefs + 2,216 reports   12,390          233s
  hosted swarm, one planner  37,356               67%    33,636 coordinator + 1,888 briefs + 1,832 reports   7,591           153s
...
  one agent / KP250-2608-0007: visit 2026-09-24 after remedy_by 2026-09-23
    started at: one agent, turn 30
    category:   context contamination: a tool result made for another unit was reused
    evidence:   book_slot(FSE-01-2026-09-24-13) took a slot from the turn-21 search with
    not_after=2026-09-30 (made for KP250-2608-0004); this unit's remedy_by is 2026-09-23
...
  hosted swarm, 2 planners / KC2-2608-0002: rejected at the first commit, re-planned
    started at: schedule-planner #2 (thread sthread_mock_000398)
    category:   stale shared snapshot (no concurrency control between threads) - rejected at commit, re-planned
    evidence:   proposed the last KC-2-PSB at WH-EAST, which schedule-planner #1 had already given
    to KC2-2608-0001 in the same commit; both planned from the snapshot mounted at session start
```

Read the two cost columns together. As run - every configuration with prompt caching - the tuned swarm costs 1.05x
the single agent and the naive one 2.15x, but per *successful* unit the single agent is the dearer of the two
($0.0376 against $0.0323), because two of its eleven bookings are wrong. Uncached, one agent would cost 3.2x its
bill: most of what it processes is its own growing context, re-read on every turn, and the cache prices exactly that at
a tenth. The hosted swarm's coordination share is high because the lead is the hub - every report comes back to it
and every commit is written by it. *Live, watch:* whether Claude reproduces the single agent's contamination on your
own traces, how the costs move with real cache hit rates on both sides, and what the critical path looks like when
the threads truly run in parallel.

---

## Key takeaways

1. **Make the coordinator durable and the queue the system of record.** Dispatch is an idempotent effect; a crashed
   coordinator resumes from its log, a crashed worker from its own run; nothing is lost and nothing runs twice.
2. **Classify failures.** Retry what can succeed on retry (a dead worker, a timeout, a 429); dead-letter what cannot
   (bad input) with its history, and page a person.
3. **Guard shared state in code.** Versions and compare-and-set for records, atomic claims for work, a check at the
   system of record for scarce resources, fencing tokens against zombies; recompute aggregates from append-only results.
4. **Budgets are admission control, not tripwires.** A gate checked after the fact overshoots by one task per worker;
   reserve an estimate before the work starts. Add per-agent caps, breakers and loop detection in the tool executor.
5. **Messages between agents are APIs.** Typed envelopes, owners, correlation, capability checks - enforced by the
   bus before any agent spends a token. Delegation keeps the conversation, a handoff moves it with its state, a
   broadcast owns nothing. MCP is for one agent's tools, not for tasks between agents.
6. **Managed Agents takes the loop, the state, the sandbox and a hard cap off your hands** - and leaves you the
   custom tools, the idempotency of your effects, the policy at commit and the evals. Stream first and consolidate with
   the history; answer `requires_action`; set a budget.
7. **Hosted threads share a filesystem, not transactions.** Pass bulk data by reference (mounted files), validate
   commits in your code, and let one agent own a contended resource.
8. **Evaluate per unit of work and attribute from traces.** A swarm's summary hides dropped units and honest-looking
   escalations; the run logs and the session stream say which agent, which turn, which call.
9. **Measure the coordination bill.** Coordinator tokens, briefs re-read on every turn, reports: the naive swarm spent
   4.7x what the job needed, and caching moves the dollar break-even for splitting far out (14 units here, not 4).
10. **Decide with numbers, then with ownership.** Split for isolation, parallelism or durability you can measure;
    choose hosted or self-hosted by what you must own; and remember that many "multi-agent" workloads are a loop in
    code.

## Further reading

- Anthropic, "How we built our multi-agent research system": https://www.anthropic.com/engineering/multi-agent-research-system
- Anthropic, "Building effective agents": https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, "Effective harnesses for long-running agents": https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Claude Managed Agents overview: https://platform.claude.com/docs/en/managed-agents/overview
- Managed Agents sessions: https://platform.claude.com/docs/en/managed-agents/sessions.md
- Managed Agents events and streaming: https://platform.claude.com/docs/en/managed-agents/events-and-streaming.md
- Managed Agents tools (built-in toolset, custom tools): https://platform.claude.com/docs/en/managed-agents/tools.md
- Managed Agents files (session resources and outputs): https://platform.claude.com/docs/en/managed-agents/files.md
- Managed Agents multi-agent orchestration: https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration
- Managed Agents observability: https://platform.claude.com/docs/en/managed-agents/observability.md
- Managed Agents vaults (live only in this course): https://platform.claude.com/docs/en/managed-agents/vaults.md
- Managed Agents outcomes (live only in this course): https://platform.claude.com/docs/en/managed-agents/define-outcomes
- Prompt caching (fan-out and pre-warming): https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Rate limits: https://platform.claude.com/docs/en/api/rate-limits
- Errors and retries: https://platform.claude.com/docs/en/api/errors
- Batch processing (for fan-outs that need no coordinator): https://platform.claude.com/docs/en/build-with-claude/batch-processing
- Model Context Protocol: https://modelcontextprotocol.io
- OpenTelemetry semantic conventions for generative AI: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- In this repository: the first course's multi-agent day ([`../../day4_workflows_multi_agent/README.md`](../../day4_workflows_multi_agent/README.md)),
  the durable runtime ([`../lib/durable.py`](../lib/durable.py), Day 1), and the capstone's reference coordinator
  ([`../day7_capstone/reference/`](../day7_capstone/reference/)).
