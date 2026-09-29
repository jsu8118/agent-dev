# Day 7 — Capstone: the Recall Campaign Orchestrator

> **The brief in one sentence:** run a supplier-defect recall across seven customers and eleven units over
> several business days with a system of agents that survives crashes and redeploys, waits days for a
> manager, never acts on hostile mail, stays inside a budget, and can be operated by someone who was not in
> the room when it was built.

On Days 1–6 you built the parts: a durable runtime (Day 1), tools at scale (Day 2), long-horizon context
(Day 3), orchestration at scale (Day 4), security engineering (Day 5) and evaluation and release science
(Day 6). Today you **integrate** them into one system that does real, multi-day work for Kestrel — and you
prove it with an acceptance suite and an operations memo.

## Learning objectives

By the end of the day you can:

1. Decompose a multi-day business process into **durable runs** with deterministic ids and keyed side effects, so that any worker can resume any step and nothing happens twice.
2. Put the model's judgement inside **scoped, guarded capabilities** — role allow-lists, row filters, recipient allow-lists, approval gates — instead of inside instructions.
3. Make security **a layer in code**: screen before the model, sanitise after it, quarantine with zero model calls, and prove it on a hostile corpus.
4. Run a **coordinator** that plans by risk and SLA, dispatches agent runs, sends reminders, enforces stop conditions and a budget, and persists its own diary.
5. Write **acceptance gates** for a system like this (security, SLA, durability, approvals, budget, operability, scenarios) and an **operations memo** that tells the on-call engineer what to do at 3 a.m.

## Agenda (≈ 7 hours)

| Time | Block |
|---|---|
| 0:00 – 0:30 | The brief, the data, the acceptance gates, the starter kit (this page) |
| 0:30 – 1:15 | **M1** inbound screening |
| 1:15 – 2:15 | **M2** scoped, idempotent, guarded tools |
| 2:15 – 2:30 | Break |
| 2:30 – 3:30 | **M3** priority plan and durable outreach (crash it, resume it) |
| 3:30 – 4:15 | Lunch |
| 4:15 – 5:15 | **M4** reply handling: quarantine, stop conditions, holds |
| 5:15 – 6:00 | **M5** budget, loop detection, circuit breaker, approvals, parts · **M6** acceptance: GO |
| 6:00 – 7:00 | **M7** design document + operations memo; review against the reference |

---

## 1. The brief

*From: Quality lead, Kestrel Pumps & Controls. To: you.*

> We have confirmed two bad lots: seal cartridge lot **PS-2608-B** (nine KP-100/KP-250 pumps) and KC-2
> board lot **VD-2607-C** (two controllers). The seal failure can release process fluid at the shaft — on
> chemical, pharma, fire and offshore duty that is a safety matter. Eleven units at seven customers must get
> a free field remedy. The notice (`advanced/data/recall/campaign.json`) sets the rules: contact within 1/2/3
> business days by risk class, remedy within 5/10/15; never change the hazard wording; goodwill credits
> above $500 need a support manager; an injury report stops the lot; parts at zero stop the remedy; model
> spend has a cap.
>
> The outreach will run for weeks. Workers will be redeployed in the middle of it. Managers will approve
> things the next day. Some replies will be hostile — last year a lookalike domain tried to have replacement
> parts shipped to a warehouse we had never heard of. I need a system that keeps its state outside any one
> process, that cannot be talked into anything by an email, that I can pause, resume and audit, and evidence
> that it does what the notice says.

## 2. What you build

```
run_campaign.py --days 5     the coordinator, one business day at a time: plan → outreach runs → screened replies →
                             inbound runs → reminders → stop conditions → dashboard
run_ops.py                   operations: approvals, stuck runs, SLA table, replay a run, forensics on a reply, lift a pause
run_evals.py                 the acceptance suite: three campaigns (uninterrupted, crashed-and-resumed, tiny budget) → GO / NO-GO
```

The reference package `reference/recall/` (≈ 1,100 lines) is the shape you are aiming for:

| Module | Role |
|---|---|
| `store.py` | one SQLite file: `RunStore` (event logs, leases, effects, approvals) + campaign tables with primary keys that make every write idempotent |
| `tools.py` | 16 tools with strict schemas and `meta`; `CampaignDesk` executes them scoped to one role and one customer, keyed by run + tool_use, gated by approvals |
| `security.py` | `screen_inbound` (sender status, injection, spoofed approvals, phishing, redirection, data requests, flags) and `sanitize_outbound` |
| `agents.py` | the outreach and inbound agents: each a `DurableRunner` run with a deterministic id and a scoped desk |
| `budget.py` | `SwarmBudget` (persisted campaign cap) and `Guard` (loop detection, circuit breaker) |
| `orchestrator.py` | `plan()`, `run_day()`, reply handling, reminders, stop conditions, `sla_status()` |
| `evals.py` | the acceptance suite: per-reply scenario checks and seven gates |

## 3. Requirements

### Functional

| ID | Requirement | Verified by |
|---|---|---|
| R1 | Every affected customer gets exactly one initial notice (TPL-RC-01) per contact address, most urgent first (risk class, tier), within its contact SLA. | M3, G2, G3 |
| R2 | Every reply is screened before any model call. Hostile replies (lookalike senders, instructions to the assistant, forged internal approvals, credential links, parts redirection, other customers' data) are quarantined to the security queue with **zero** model calls. | M1, M4, G1 |
| R3 | Benign replies are handled by the inbound agent with scoped tools: slots that honour the constraints (weekday, mornings/afternoons, earliest date, ATEX), answers from the brief, retries for out-of-office, re-targeting when a contact left, escalations for legal and compensation. | M4, G7 |
| R4 | A claim that a unit was already fixed is verified, never taken as evidence; `mark_remediated` needs a completed appointment. | RPL-012, G1 |
| R5 | An injury or fluid-release report pauses the lot and pages quality (P1) before the agent replies; nothing is scheduled on a paused lot. | RPL-018, M4 |
| R6 | Goodwill credits above $500 pause the run for a support manager, who decides from another process; the run resumes and records the approver. | M5, G4 |
| R7 | A worker crash anywhere leaves the campaign resumable; a resumed run replays completed tool calls and never sends twice. | M3, G3 |
| R8 | Model spend stays under the cap (persisted across processes); a tiny cap pauses the campaign. Loops and failing tools trip guards inside a run. | M5, G5 |
| R9 | Every message, booking, reservation, credit, escalation, refused call and pause is audited with its actor; every run has a replayable log; forensics can reconstruct who told the agent what. | G6, `run_ops.py` |

### Non-functional (the acceptance gates)

| Gate | Target |
|---|---|
| G1 security | 100% of hostile replies quarantined; 0 outbound to unknown addresses; 0 units closed on a customer's say-so |
| G2 SLA | 100% of customers contacted within their risk class's deadline |
| G3 durability | crash + resume produces exactly the same outbound messages as the uninterrupted run |
| G4 approvals | 0 above-threshold credits without a recorded human decision |
| G5 budget | spend ≤ cap; a $0.30 cap pauses the campaign instead of overspending |
| G6 operability | 0 unfinished or stuck runs; 0 outbound messages without an audit row |
| G7 scenarios | ≥ 90% of reply scenarios pass and 100% of the critical ones |

## 4. The starter kit

```
starter/
  recall/
    config.py, store.py, agents.py   provided (settings; durable state; the two agents as DurableRunner runs)
    security.py        M1  screen_inbound / sender_status to implement (sanitize_outbound provided)
    tools.py           M2  _scope, _contact, idempotent _deliver, booking checks to implement (the 16 tools provided)
    orchestrator.py    M3  plan(), resume of interrupted runs   M4  quarantine, stop conditions, holds   M5  parts stop condition
    budget.py          M5  SwarmBudget.check, Guard (loop detection, circuit breaker)
  run_starter_check.py   your progress report, milestone by milestone
```

```bash
cd advanced/day7_capstone/starter
python run_starter_check.py                    # what's done, what's failing, and why
python run_starter_check.py --only M2          # iterate on one milestone
python run_starter_check.py --impl reference   # what "done" looks like
```

The checker gives specific feedback ("RPL-013 (fraud) must be quarantined; got clean", "a run scoped to
C-1005 read C-1001's contacts"). Everything works in mock mode; with a key, M3–M6 run against Claude.

## 5. Milestones

**M1 — Inbound screening (≈ 45 min).** Implement `sender_status()` and `screen_inbound()` in `security.py`.
*Done when:* all five hostile replies in `advanced/data/recall/inbound_replies.jsonl` are quarantined for the
right reason, the injury report and the legal notice reach the agent **with** their flags, the automatic
reply carries `out_of_office`, and no benign reply is quarantined. *Hints:* the patterns at the top of the
file are a start; the labels in the data are the truth. Compare the sender's domain with the customer's
contact domains (edit distance ≤ 2; `harborfood` vs `harborfoods`; `mail-` prefixes). Quarantine is a
label, not a judgement call: if any hostile kind matches, the message never reaches a model.

**M2 — Scoped, idempotent, guarded tools (≈ 60 min).** In `tools.py` implement `_scope()` (a customer-scoped
desk refuses other customers), `_contact()` (only this customer's active contacts), the at-most-once send in
`_deliver()` (`ctx.effect(f"{key}:send")`) and the region/skill checks in `_book_visit()`.
*Done when:* the checker's eight probes pass, including "the same step delivered once and left a `done`
effect record". *Hint:* read `advanced/lib/durable.py`'s `_Effect` docstring; the effect record is what a
*different* process consults when it resumes the run.

**M3 — Priority plan and durable outreach (≈ 60 min).** In `orchestrator.py` implement `plan()` (skip
customers with a future retry date; order by risk class, tier, id) and step 1 of `run_day()` (resume runs a
crashed worker left in `running`/`pending`). *Done when:* the plan order is C-1002, C-1014, C-1001, C-1005,
C-1012, C-1016, C-1025; day 1 completes outreach; a crash after the fourth tool call of C-1005's run and a
second `run_day(1)` leave exactly one notice per customer with `report.resumed == ["outreach:C-1005 -> completed"]`.
*Hint:* `agents.resume()` handles both "crashed" and "approved" runs; leases expire in 30 s in the reference,
which is why a resumed run does not need the dead worker.

**M4 — Reply handling (≈ 60 min).** In `_handle_reply()` add the three deterministic paths before the agent:
quarantine (security escalation, no run), safety event (pause the customer's lots through the orchestrator's
desk, P1 quality escalation, then let the agent answer), and customers on hold (route to the hold's owner);
after the agent, put a customer under legal notice on hold. *Done when:* all hostile replies are logged as
quarantined with no `inbound:` run, PS-2608-B is paused after RPL-018, C-1012 is `held:legal` after RPL-019,
and no unit is remediated after RPL-012.

**M5 — Budget, guards, approvals, parts (≈ 45 min).** In `budget.py` implement `SwarmBudget.check()` and
`Guard.__call__()`; in `orchestrator.py` implement `_stop_conditions()` for exhausted parts. *Done when:* the
fourth identical call returns a "loop detected" error, three failures in six calls open the circuit,
`ApprovalRequired` propagates, a $0.30 cap pauses the campaign, the $750 credit waits for approval and
resumes with the approver recorded, and zero `KC-2-PSB` stock pauses `controller_board_replacement`.

**M6 — Acceptance (≈ 30 min).** `python run_starter_check.py --only M6` runs the shared suite through your
package: three campaigns and seven gates. Read every failed check; each names the reply and the reason.
With an API key, run it live too and put what changes in your memo.

**M7 — Design document and operations memo (≈ 60 min).** Write `DESIGN.md` (decisions, alternatives,
threat model, failure modes, cost) and `OPERATIONS_MEMO.md` (how to run, watch, pause, resume, approve,
investigate and roll back the campaign) for the quality lead and the on-call engineer. Compare with the
reference: [`reference/DESIGN.md`](reference/DESIGN.md), [`reference/OPERATIONS_MEMO.md`](reference/OPERATIONS_MEMO.md).

**Stretch goals** (pick any):
* **Hosted twin:** run the inbound agent as a Managed Agents session (Day 4, lab 05) with the campaign tools as custom tools; compare cost, latency and what you had to keep on your side.
* **Classifier in front of the patterns:** a model-based screen (Day 5, lab 02) that catches paraphrased injections; measure its false positives on the benign replies.
* **Eval science:** turn the campaign's outbound emails into a pairwise eval (Day 6, lab 03) and hill-climb the outreach prompt on a holdout.
* **Scheduling optimiser:** book visits to minimise engineer travel across customers in the same region.
* **Phone channel:** a second outreach channel with its own contact preferences and idempotency keys.

## 6. Deliverables and grading

Hand in your `starter/recall/` package, the acceptance report (`.runs/advanced_capstone/eval_report.md`),
`DESIGN.md` and `OPERATIONS_MEMO.md`.

| Area | Points | What earns full marks |
|---|---|---|
| Correctness (M1–M6) | 35 | All milestones pass in mock mode; live run done or its absence explained |
| Durability & idempotency | 15 | Deterministic run ids, keyed effects, resumable coordinator; you can explain what happens if the worker dies at every step |
| Security & authorization | 20 | Screening before the model, scoping and allow-lists in code, threat model mapping each threat to a control and a test |
| Operability | 10 | Approvals from another process, stuck-run detection, replay, forensics, pause/resume, budget persistence |
| Evaluation | 10 | Gates with reasons; failures analysed; live evidence or a plan for it |
| Documents | 10 | Alternatives compared; an operations memo someone else could act on |

## 7. The reference solution

```bash
cd advanced/day7_capstone/reference
python run_campaign.py --fresh --days 5 --decide approve   # five business days, a manager approving at the end of each
python run_campaign.py --fresh --days 2 --crash            # day 1 dies mid-outreach ...
python run_campaign.py --days 1                            # ... and resumes: one notice per customer, the send replayed
python run_campaign.py --fresh --days 2 --cap 0.30         # a tiny budget: the campaign pauses itself
python run_ops.py                                          # approvals, stuck runs, SLA, escalations
python run_ops.py --forensics RPL-014                      # who told the agent what
python run_evals.py                                        # the acceptance suite -> GO
```

In mock mode the reference passes all 19 scenario checks and 7 gates (`Decision: GO`). That proves the
**mechanics** (screening, scoping, idempotency, approvals, budgets, durability), not the quality of a live
model's emails; the operations memo says how to get the live evidence. The walkthrough
([`reference/WALKTHROUGH.md`](reference/WALKTHROUGH.md)) shows how the reference was built, milestone by
milestone, with the mistakes made on the way.

## 8. FAQ

**Why is the screen in code when Day 5 taught model-based classifiers?** Both. The patterns are the
boundary: cheap, deterministic, testable against the corpus, and immune to persuasion. A classifier in
front of them (stretch goal) widens coverage to paraphrases; it does not replace the boundary. A quarantined
message costs a person a few minutes; a missed redirection costs a pallet of parts.

**Why not let the inbound agent decide quarantine — it reads the reply anyway?** Because by then the
reply is in the model's context. The injected instruction in RPL-014 asks the assistant to mark units
remediated; the tool refuses without evidence, so the damage would be contained — but "contained by the
second line" is not a design, it is luck. Zero model calls is the property the gate checks.

**The mock passes everything. Am I done?** With the mechanics. The mock's agents are rule-based stand-ins
that derive every tool call from the brief; a live model will word emails differently, sometimes propose
slots in a different order, and occasionally escalate where the mock proposed. Run live, read the diffs, and
decide per case: fix the prompt, fix the tool, or fix the scenario.

**Why a business-day loop instead of a queue worker?** The loop is the simplest shape that shows every
durability property (a day boundary is just a restart). The same orchestrator runs as a worker on a queue:
`run_day` becomes "drain what is due now"; nothing in the store or the runs changes.
