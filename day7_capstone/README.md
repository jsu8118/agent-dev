# Day 7 — Capstone: the Kestrel Service Desk Copilot

> **The brief in one sentence:** put Kestrel's support agent in front of real customers, which means
> surrounding it with everything that makes an agent *deployable*: a front door that decides what the
> agent may see, deterministic handling of anything that must never go wrong, evidence that it works,
> and a system someone can operate.

On Days 1–6 you built the parts: a triage classifier (Day 1), a tool-using support agent with policy in
code (Day 2), retrieval over the manuals (Day 3), workflows and a quality investigation (Day 4), MCP
servers (Day 5), and evals, guardrails and tracing (Day 6). Today you **integrate** them into one system
and take it through a go-live review. It is the day that looks most like your actual job.

## Learning objectives

By the end of the day you can:

1. Decompose a business request into **what must be guaranteed** (code), **what needs judgement** (models) and **what needs a person** (review, escalation), and defend each boundary.
2. Build a **hybrid workflow + agent** system in which the agent does the open-ended work behind deterministic gates.
3. Write **acceptance criteria as executable gates** (zero tolerance on safety/security/privacy, pass rate, cost, latency, alert precision/recall) and make a go/no-go decision from them.
4. Handle the production realities that demos skip: at-least-once delivery, API outages, suspicious senders, multilingual input, human-review queues, audit trails.
5. Communicate the result the way an engineering lead must: a **design document** with alternatives and trade-offs, and a **go-live memo** with evidence, risks and a rollout plan.

## Agenda (≈ 7 hours)

| Time | Block |
|---|---|
| 0:00 – 0:30 | The brief, the acceptance criteria, the starter kit (this page) |
| 0:30 – 1:30 | **M1** input screen · **M2** routing gate |
| 1:30 – 2:30 | **M3** the pipeline end to end |
| 2:30 – 2:45 | Break |
| 2:45 – 3:30 | **M4** quality-hold detector · **M5** output guard |
| 3:30 – 4:15 | Lunch |
| 4:15 – 5:15 | **M6** acceptance suite: fix until GO (then: live run, if you have a key) |
| 5:15 – 6:15 | **M7** design doc + go-live memo (+ stretch: service, MCP server) |
| 6:15 – 7:00 | Go-live review: present your memo; compare with the reference |

---

## 1. The brief

*From: Head of Customer Service, Kestrel Pumps & Controls. To: you.*

> We receive about 1,900 support emails a month and our median first response is 9.5 business hours.
> The support-agent prototype impressed everyone in the demo. Before a single customer sees its output, I
> need a system I can defend to our COO, our Quality director and our security team:
>
> * Safety reports (SOP-SUP-007) must reach the on-call engineer immediately, **every time**, and the
>   customer must get the SOP instructions, never improvised repair advice.
> * Nothing suspicious (prompt injection, bank-detail changes, impersonation) may reach the agent or
>   trigger an action.
> * Refund limits, identity checks and approvals must hold no matter what an email says.
> * Quality is chasing two supplier lots on hold (`PS-2608-B` seal kits, `VD-2607-C` controller boards;
>   see `data/quality/incident_reports/INC-P2-0419.md` and `INC-P3-0214.md`). Units built with them have
>   shipped. If a customer reports a problem on one of those units, Quality must know the same day, and
>   the customer must not be told about the hold before inspection.
> * Leadership's constraints stand (`data/company/company_profile.md`): **mean cost below $0.40 per
>   ticket, replies within 30 seconds, every action auditable.**
>
> Give me something I can run, the evidence that it meets these criteria, and your recommendation on how
> we go live.

## 2. What you build

```
inbound email (sender verified by the mail gateway)
      │
      ▼
 ┌──────────┐  security flag   ┌────────────────────────────────────────────┐
 │ M1 screen├─────────────────►│ security route: escalate, neutral/no reply │   (no model ever reads it)
 │  (code)  │                  └────────────────────────────────────────────┘
 └────┬─────┘
      │ clean
      ▼
 ┌──────────┐
 │  triage  │  (LLM, structured output, provided)
 └────┬─────┘
      ▼
 ┌──────────┐  safety (screen backstop OR triage P1)  ┌────────────────────────────────────┐
 │ M2 gate  ├────────────────────────────────────────►│ safety route: P1 page + SOP reply   │
 │  (code)  ├── triage failed ────────────────────────►│ human route: holding reply + queue  │
 └────┬─────┘                                          └────────────────────────────────────┘
      │ everything else (review=True if requires_human)
      ▼
 ┌──────────────────────┐    ┌──────────────────┐     ┌──────────────────────────┐
 │ support agent (Day 2)│───►│ M5 output guard  │────►│ send, or hold for review │
 │ tools enforce policy │    │ (code)           │     └──────────────────────────┘
 └──────────────────────┘    └──────────────────┘
      │ (all non-quarantined routes)
      ▼
 ┌──────────────────────┐
 │ M4 quality detector  │──► alert to Quality (internal)
 │ (code, build records)│
 └──────────────────────┘
```

Only two boxes use a model. That is the central design idea of the capstone, and the design doc asks you
to defend it.

## 3. Requirements

### Functional

| ID | Requirement | Verified by |
|---|---|---|
| R1 | Every email gets exactly one route (`safety`, `security`, `human`, `agent`) and one disposition (`sent`, `review`, `quarantined`), with recorded reasons. | M2, M3 checks |
| R2 | Safety (SOP-SUP-007 §1): P1 escalation to `field_service` plus the SOP §2 reply (keep clear, isolate/de-energize, lockout/tagout, 1-hour callback, reference), in the customer's language where supported. Never repair advice. | E17–E19, C02, C05, C07 |
| R3 | Suspicious emails (instructions to an AI, payment-detail changes, lookalike/cousin sender domains) are quarantined: security escalation, no model call, no tool call other than the escalation, no reply to impersonating domains. | E20, E21, C04, C07 |
| R4 | If triage is unavailable (outage, refusal), fail safe: holding reply + `support_manager` queue. | C08 |
| R5 | Other emails go to the Day 2 support agent. When triage says `requires_human`, the reply is drafted but **held for review**. | C06 |
| R6 | Quality alert when a customer reports a symptom on a unit built with a held lot; no alert without a symptom or for units the sender doesn't own; the reply never mentions the hold. | E09, E30, C01–C03 |
| R7 | Output guard holds replies that mention other customers' orders or contacts, payment data, internal quality information, or admit liability/promise compensation. | M5 check |
| R8 | Re-delivery of the same email must not duplicate actions (RMAs, refunds, alerts). | C10 (+ service idempotency) |
| R9 | Every ticket produces a trace with cost and latency; every action is in the audit log. | run_pipeline.py output |

### Non-functional (the acceptance gates)

| Gate | Target |
|---|---|
| Safety, security and privacy scenarios | **100%** pass (zero tolerance) |
| Critical check failures (forbidden tool, unauthorized write, leaked text) | **0** |
| Overall scenario pass rate | ≥ **90%** |
| Quality-alert recall / precision | **100%** / ≥ **90%** |
| Mean cost per ticket | ≤ **$0.40** (live) |
| p95 latency | ≤ **30 s** (live) |

`reference/copilot/evals.py` implements these gates over 40 scenarios: the Day 6 support set (E01–E30,
`data/evals/support_eval_set.jsonl`) plus ten capstone scenarios (C01–C10,
[`scenarios/capstone_eval_set.jsonl`](scenarios/capstone_eval_set.jsonl)), each with a `why` field
explaining what it tests.

## 4. The starter kit

```
starter/
  copilot/
    config.py        provided: models, budgets, QUALITY_HOLDS
    triage.py        provided: TicketTriage schema + triage_email() (Day 1)
    screen.py        M1  (two example rules to extend)
    gate.py          M2
    pipeline.py      M3  (skeleton with the bookkeeping done: trace, cost, latency, error handling)
    quality.py       M4
    output_guard.py  M5  (one example check)
  run_starter_check.py   your progress report, milestone by milestone
```

```bash
cd day7_capstone/starter
python run_starter_check.py                    # what's done, what's failing, and why
python run_starter_check.py --only M1          # iterate on one milestone
python run_starter_check.py --impl reference   # what "done" looks like
```

The checker gives specific feedback ("safety backstop missed P1 tickets ['T-1804']", "E20: a quarantined
email should never reach a model"). Everything works in mock mode; with a key, M3–M6 run against Claude.

## 5. Milestones

Each milestone lists what "done" means (the checker tests it) and hints. Try before you look at the
reference; when stuck for more than 20 minutes, read the matching section of
[`reference/WALKTHROUGH.md`](reference/WALKTHROUGH.md).

**M1 — Input screen (≈ 45 min).** Implement `screen_email()` and `domain_impersonation()`.
*Done when:* all 4 P1 tickets in `data/support/tickets.jsonl` are caught with zero false alarms among the
other 58; security flags on exactly T-1208, T-1507, T-1703; C04's lookalike domain and C05's Spanish
smoke report are caught; order IDs and serials are extracted.
*Hints:* read SOP-SUP-007 §1 and turn each of its six clauses into a rule. Some need two conditions
(ATEX equipment **and** a fault). Compare the sender domain with `customers.email_domain` in the ops DB
(edit distance ≤ 2 for lookalikes; "contains a customer's name" for cousins). Don't flag
`m.chen.personal@mailbox.example` (T-1006): an unknown sender is not an attacker; the tools handle identity.

**M2 — Routing gate (≈ 45 min).** Implement `decide_route()` and the three deterministic handlers.
*Done when:* the checker's truth table passes (including "safety **and** security"), handlers escalate
through `desk.run(...)`, the safety reply follows SOP §2, and impersonating domains get no reply.
*Hint:* go through the tools (`desk.run("escalate_to_human", {...})`) instead of writing to the database:
the action is then audited, visible to evals, and identical to what the agent would do.

**M3 — Pipeline (≈ 60 min).** Fill in `handle_email()`.
*Done when:* E03 → agent/sent with the tracking number, E17 → safety/sent, E20 → security/quarantined
with **zero** LLM calls.
*Hints:* never call triage on a suspicious email. Pass the pipeline's `desk` and `tracer` into
`run_support_agent` so its tool calls and costs land in the same Outcome and trace. Honour the rollout
switches in `config.py` (`auto_send`, `auto_send_categories`) when you set the disposition: they are how
the go-live memo's staged rollout and rollback work.

**M4 — Quality detector (≈ 45 min).** Implement `detect_quality_signals()`.
*Done when:* alerts on E09 (`PS-2608-B`), E30 (`VD-2607-C`), C02 (safety route!), and none on C03 or E03.
*Hints:* units come from three sources: serials in the email (sender-owned only), (order, SKU) pairs the
agent looked up successfully, and orders the sender owns named in the email. Require a symptom.

**M5 — Output guard (≈ 30 min).** Complete `review_reply()`.
*Done when:* the five sample violations are caught and a clean reply isn't flagged.
*Hint:* allowed order IDs = the sender's orders ∪ orders quoted in their email ∪ orders the tools returned.

**M6 — Acceptance (≈ 60 min).** Run the full suite (`run_starter_check.py --only M6`) until the decision
is GO. Read every failed check: each one names the scenario and the reason. With an API key, also run it
live; expect a few failures that mock mode can't show you (wording, a model choosing a different but
defensible tool path). Decide for each: fix the system, or fix the scenario? Write the reasoning down;
it goes into your memo.

**M7 — Design doc and go-live memo (≈ 60 min).** Write `DESIGN.md` (decisions, alternatives, trade-offs,
threat model, failure modes, cost model) and `GO_LIVE_MEMO.md` (recommendation, evidence, risks,
rollout, monitoring, rollback) for the Head of Customer Service. Compare with the reference versions:
[`reference/DESIGN.md`](reference/DESIGN.md), [`reference/GO_LIVE_MEMO.md`](reference/GO_LIVE_MEMO.md).

**Stretch goals** (pick any):
* **Service:** expose `handle_email` over HTTP with idempotency on the gateway's message ID, API-key auth,
  input limits and `/metrics` (reference: `copilot/service.py`, `run_service_smoke.py`).
* **MCP server:** give support leads a read-only `preview_triage` and `quality_hold_check` in Claude Code
  (reference: `copilot/mcp_server.py`, `run_mcp_selftest.py`).
* **Proposal mode:** for `review` tickets, run the agent with write tools replaced by *proposals* (a
  `SupportDesk` subclass whose `create_rma`/`issue_refund` record a pending action for the reviewer to
  approve) so nothing irreversible happens before a person looks.
* **Cheaper triage:** measure Claude Haiku 4.5 or Claude Sonnet 5 as the triage model with Day 1's
  harness. Does the screen's backstop let you accept a lower P1 recall from the classifier? Put the
  numbers in your memo.
* **Batch mode:** a nightly Batches API run over the day's quarantined and reviewed tickets that drafts a
  summary for the support manager (50% cheaper, no latency requirement).

## 6. Deliverables and grading

Hand in your `starter/` package, your eval report (`.runs/capstone_starter/eval_report.md`), `DESIGN.md`
and `GO_LIVE_MEMO.md`.

| Area | Points | What earns full marks |
|---|---|---|
| Correctness (M1–M6) | 35 | All milestones pass in mock mode; live run done or its absence explained |
| Safety & security design | 20 | Deterministic guarantees where they belong; threat model maps each threat to a control and a test |
| Evaluation | 15 | Failures analysed (not just counted); scenario changes justified; pass^k or repeated runs if live |
| Operability | 10 | Traces, metrics, review queue, idempotency, graceful degradation |
| Design document | 10 | Alternatives compared with trade-offs, not just the chosen design described |
| Go-live memo | 10 | A decision with evidence, staged rollout with exit criteria, monitoring and rollback |

A strong submission is not the one with the most code. It is the one whose author can say, for every
box in the diagram, *why it is code or a model, what happens when it fails, and how they know it works.*

## 7. The reference solution

```bash
cd day7_capstone/reference
python run_pipeline.py              # 9 sample tickets covering every route, with a trace
python run_pipeline.py --all        # the whole inbox (62 tickets)
python run_evals.py                 # the 40-scenario acceptance suite -> GO/NO-GO (+ .runs/capstone/eval_report.md)
python run_service_smoke.py         # the HTTP service: auth, limits, idempotency, review queue, metrics
python run_mcp_selftest.py          # the staff MCP server, in-process
```

* [`reference/WALKTHROUGH.md`](reference/WALKTHROUGH.md): how the reference was built, milestone by
  milestone, with the pitfalls hit on the way.
* [`reference/DESIGN.md`](reference/DESIGN.md): the decisions and the alternatives rejected.
* [`reference/GO_LIVE_MEMO.md`](reference/GO_LIVE_MEMO.md): an example memo.

In mock mode the reference passes all 40 scenarios. That proves the **mechanics** (routing, gates,
guards, idempotency, alerts), not the quality of a live model's replies. The memo explains how to get
the live evidence and what to do with it.

## 8. FAQ

**Why not let the agent handle safety emails? Its prompt already covers them.** It does, and that
remains a second line of defense. But the SOP reply is fixed text, the escalation is a single action,
and the cost of a miss is a person in danger. A deterministic path is instant, identical every time and
testable exhaustively. Models add value where judgement is needed; here they would only add variance.

**Regexes feel old-fashioned.** They are the backstop, not the classifier. On Kestrel's inbox the
screen catches 4/4 P1 tickets with zero false alarms, costs nothing, answers in microseconds and cannot
be argued with by the text it inspects. Its weakness (paraphrases, languages it doesn't know) is covered
by the LLM triage running in parallel. Two different mechanisms fail in different ways; that is the point.

**Why quarantine a whole email when only one sentence is an injection (E21, T-1208)?** Because
"remove the bad part and continue" means trusting a filter to find every variant of an attack. The cost
of quarantine is a person handling a few tickets (3 of the 62 in the sample inbox, which is deliberately
rich in edge cases); the cost of a missed injection is an unauthorized action. See DESIGN.md, D4.

**Mock mode passes everything. Am I done?** You're done with the mechanics. Mock replies come from
rule-based stand-ins; a live model will phrase things differently and occasionally choose a different
tool path. Run live, read the failures, and put what you learned in the memo.
