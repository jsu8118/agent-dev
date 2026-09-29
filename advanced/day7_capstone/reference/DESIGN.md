# Recall Campaign Orchestrator — design document (reference)

*What this system is, the decisions behind it, the alternatives rejected, and what we know it does not do.*

## 1. Problem and constraints

Quality has found two bad manufacturing lots (seal cartridge lot `PS-2608-B`, KC-2 board lot `VD-2607-C`).
Eleven units at seven customers must get a free field remedy. The campaign is a **multi-day, multi-party
process**: notices, replies with scheduling constraints, engineer slots, parts stock, reminders, a legal
notice, an injury report, and a steady trickle of hostile mail (a lookalike domain redirecting parts, an
injected instruction to close a unit, a request for the other customers' names, a phishing link, a forged
internal approval). Constraints from the recall notice (`advanced/data/recall/campaign.json`):

* contact within 1/2/3 business days and remedy within 5/10/15 by risk class (safety, production, standard);
* goodwill credits above $500 need a support manager; the hazard wording never changes; other customers are never named;
* stop conditions: an injury or fluid release pauses the lot; parts at zero pause the remedy; model spend at the cap pauses everything;
* every action auditable; the campaign must survive worker crashes, redeploys and a manager who decides tomorrow.

## 2. Architecture

```
 inbound_replies ─► security.screen_inbound ─► quarantine? ──► security queue (no model call)
                                          │
                                          ▼ clean / flagged
 orchestrator.run_day(day) ──► agents.run_inbound  ──► DurableRunner ──► CampaignDesk (scoped tools) ──► store
        │  plan() by risk/tier             (one run per reply)          ▲ ApprovalRequired pauses the run
        ├─► agents.run_outreach (one run per customer)                  │ run_ops.py --approve resumes it
        ├─► reminders (code, no model)                                  │
        └─► stop conditions: safety event, parts, budget                └── Guard: loop detection, circuit breaker
 store: SQLite = RunStore (event logs, leases, effects, approvals) + campaign tables (units, messages, slots, parts, audit)
```

Three kinds of work, three kinds of component:

| Must be guaranteed (code) | Needs judgement (model, inside a scoped run) | Needs a person |
|---|---|---|
| screening, recipient allow-list, customer scoping, idempotent effects, priority order, SLA arithmetic, stop conditions, budget, audit | reading a reply, choosing slots that honour constraints, wording an answer, deciding to ask for evidence, proposing a goodwill credit | approving credits, security escalations, legal, the injury report, lifting a pause |

## 3. Decisions

### D1 — Every unit of work is a durable run with a deterministic id
`outreach:<customer_id>` and `inbound:<reply_id>`. Re-dispatching is idempotent (`RunStore.create` on an
existing id returns the run; `run()` on a completed run returns its outcome without a model call). A crashed
worker's run is resumed by whoever runs the next day; the messages array is rebuilt append-only from the log.
*Alternative rejected:* an in-process loop per customer with retries — a retry after a crash re-sends the notice.

### D2 — Side effects are keyed by run + tool_use, and the downstream store enforces the keys
`send_email`, `book_visit`, `reserve`, `credit`, `escalate` all derive their ids from `ToolContext.idempotency_key`
(`_id()` hashes it), and the store's primary keys reject duplicates. `send_email` additionally records the effect
through `ctx.effect()`, so a replayed step returns the stored result. *Alternative rejected:* "check before you
send" — two workers can both check before either sends; the key is what makes the second attempt a no-op.

### D3 — Security in code, before and after the model
`security.screen_inbound` runs before any model call and decides quarantine from the sender's domain
(lookalikes by edit distance) and the message's content (instructions for the assistant, forwarded internal
approvals, credential links, parts redirection, other customers' data). Quarantined mail goes to the security
queue with zero model calls. `sanitize_outbound` runs on every email the agent writes: external links removed,
other customers' names and compensation promises block the send. *Alternative rejected:* telling the model to
be careful. It is the second line of defence, not the boundary.

### D4 — Capability scoping per run
A `CampaignDesk` is created for one role and one customer. Tools outside the role are refused; every read and
write is filtered to that customer; `send_email` only delivers to that customer's active contacts. The
orchestrator's own desk has five tools and no customer scope. *Alternative rejected:* one desk with all tools
and instructions about who may see what.

### D5 — Approvals are durable waits, decided from anywhere
`request_goodwill_credit` above $500 raises `ApprovalRequired`; the run parks in `waiting_approval` with the
action recorded; `run_ops.py --approve` (any process) records the decision and resumes the run, which answers
the paused tool call and continues. *Alternative rejected:* blocking the worker on a manager's reply.

### D6 — Stop conditions are the coordinator's, not the agent's
An injury report pauses the lot and pages quality *before* the inbound agent runs; parts at zero pause the
remedy at the end of the day; the budget is checked before every run. The agent still answers the customer
(interim measures, sympathy) but cannot schedule against a paused lot — `book_visit` refuses.

### D7 — Budget at run boundaries, guards inside runs
The campaign cap is checked before each run against labkit's ledger, persisted per day so it survives
restarts; the overshoot is bounded by one run (`max_turns` × `max_tokens`). Inside a run, `Guard` stops loops
(the same call more than three times) and opens a circuit after three failures in six calls, telling the
model to escalate. *Alternative rejected:* a token budget per request only — it bounds one call, not a swarm.

### D8 — The mock's judgement is honest about being rules
The mock policies derive every tool call from the brief and the tool results, and the lesson says so. What
they cannot show — the quality of a live model's emails, its restraint on ambiguous replies — is what the
live run in the operations memo is for.

## 4. Threat model

| Threat | Control | Test |
|---|---|---|
| Lookalike sender redirects parts to a third party | domain edit distance + redirect pattern → quarantine, security P2 | RPL-013 |
| Injected instruction in a reply closes a unit | instruction patterns → quarantine; `mark_remediated` needs a completed appointment | RPL-014, G1 |
| Request for other customers' data | pattern → quarantine; `sanitize_outbound` blocks other customers' names | RPL-015 |
| Credential phishing aimed at engineers | link + credential words → quarantine | RPL-016 |
| Forged internal approval by email | forwarded-internal pattern → quarantine; approvals only through `RunStore.decide` | RPL-017, G4 |
| Agent sends to an arbitrary address | recipient allow-list in `_contact` | M2 check, G1 |
| One customer's run reads another's data | `_scope` on every read and write | M2 check |
| Runaway model or failing tool burns the budget | `Guard`, per-run caps, campaign cap with persistence | M5 check, G5 |
| Duplicate side effects after a crash or retry | keyed effects + store primary keys | G3 |

## 5. Failure modes and degradation

| Failure | Behaviour |
|---|---|
| worker dies mid-run | lease expires; the next `run_day` resumes the run from its log; completed tool calls are replayed |
| model API outage (429/5xx) | the SDK retries; a failed run stays `running` with an expired lease and is resumed next day; nothing is sent twice |
| a tool keeps failing | circuit opens after 3/6 failures; the model is told to escalate; the run ends with an escalation |
| no engineer slot fits the constraints | `escalate(field_service, P2)` for manual scheduling |
| parts exhausted | remedy paused, field service told, scheduling refused until stock is transferred |
| injury reported | lot paused, quality paged (P1), customer told to keep the unit stopped; nothing else scheduled on that lot |
| approval not decided | run waits indefinitely; `run_ops.py` lists it; the SLA table shows the customer as pending |

## 6. Cost model

Mock-mode accounting (list prices, `claude-opus-5`): 7 outreach runs + 12 inbound runs + 1 resume ≈ 74 calls,
$1.30 for the five-day campaign; $0.07 per outreach run, $0.06–0.10 per inbound run. Live runs will differ
(longer replies, real thinking); the cap of $25 is 20× the mock figure on purpose. Reminders cost nothing
(templates). The security layer costs nothing per message.

## 7. Observability

`run_ops.py`: dashboard, approvals, stuck runs, SLA table, escalations; `--replay <run>` prints the event log;
`--forensics <reply>` reconstructs who told the agent what. The audit table records every message, booking,
reservation, credit, escalation, refused tool call and pause with the actor (`security`, `orchestrator`,
`agent:inbound`, a manager's name).

## 8. Known limitations and next steps

* Reminders and out-of-office handling are simple (two reminders, one retry date); a real campaign needs channel
  fallbacks (phone) and account-manager involvement after the second silence.
* Slots are booked greedily per reply; there is no global optimisation of engineer travel.
* The screen's patterns are English-first; the Spanish reply is handled but a Spanish injection would not be caught by
  the same patterns — a classifier (Day 5, lab 02) belongs in front of the patterns for production.
* The budget is measured from list prices in this process's ledger; production would read spend from the Usage and Cost API.
