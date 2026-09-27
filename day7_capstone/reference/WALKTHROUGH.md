# Capstone walkthrough: how the reference was built

This is the build log of the reference solution, milestone by milestone: what was built, why, the
pitfalls hit along the way, and how each step was verified. Read the section for a milestone after you
have tried it yourself. Commands assume `cd day7_capstone/reference` (or `day7_capstone/starter` for
the checker).

---

## Before writing code: decide what must be guaranteed

List every requirement from the brief and ask, for each: *what happens if this fails once?*

| Requirement | Cost of one failure | So it is… |
|---|---|---|
| Safety email reaches the engineer | a person in danger | **code** (plus the model as a second net) |
| Suspicious email never triggers an action | fraud, data leak | **code** |
| Refund limits, identity | money, privacy | **code** (in tools, from Day 2) |
| Reply is helpful and correct | an annoyed customer, a follow-up email | **model** + evals + review |
| Choosing the tool path for a warranty claim | a slower resolution | **model** |
| Quality learns about a held-lot failure | a late recall decision | **code** (from data) |

This table *is* the architecture: everything in the "code" rows becomes a deterministic stage; the model
gets the rows where judgement creates value. Write it down first; it becomes D1 of your design doc.

---

## M1 — The input screen

**Build the security rules from the evidence.** Read the three malicious tickets (T-1208, T-1507,
T-1703) and E20/E21, and write one rule per *technique*, not per sentence: "instruction override",
"fake system mode", "note addressed to an AI", "claims pre-approval", "asks to skip verification", "asks
not to escalate", and payment-detail changes (IBAN, "bank details changed").

```python
INJECTION = [
    (r"\bignore (?:all |any )?(?:the |your )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?|policies)",
     "instruction override"),
    (r"\b(?:system|admin(?:istrator)?|developer|debug) (?:override|mode)\b", "fake system/admin mode"),
    ...
]
```

**Build the safety backstop from the SOP, clause by clause.** SOP-SUP-007 §1 lists six situations; each
became a named rule so the trace says *which* clause fired (`hazardous_leak`, `fire_smoke`, `injury`,
`atex_fault`, `critical_outage`, `controller_safety_fault`, plus `pressure_boundary`).

**Pitfalls hit, and the fix for each:**

* *"hot" is not a hazard.* E30 mentions a "hot pump room (~38 C)". The hazardous-fluid rule lists specific
  fluids (acid, caustic, ammonia, steam, "hot water"…) and requires a leak word **within 80 characters**
  (`_near(HAZARD_FLUID, LEAK, text, 80)`), rather than matching "hot" anywhere.
* *Naming ATEX equipment is not a fault.* "Please process the refund for the KP-250-X" (E13) must not
  page anyone. `atex_fault` needs an ATEX term **and** a fault word.
* *Critical services need an outage, not just a mention.* A fire-protection customer asking about a
  listing (T-1605) is not an emergency: `critical_outage` needs a critical-service term **and** an outage
  term ("tripped", "won't restart", "pressure dropped", "runs low").
* *Languages.* T-1104/T-1408 are Spanish, T-1602 German. A few high-signal words per rule (humo, Rauch,
  fuga, Säure) catch the dangerous cases (C05) without trying to be a translator; the LLM triage covers
  the rest.
* *An unknown sender is not an attacker.* `m.chen.personal@mailbox.example` (T-1006, E22) is a real
  customer writing from a personal address. Flag only domains that **imitate** a known one: edit distance
  ≤ 2 (≤ 1 for short names), homoglyph normalization (`rn`→`m`, `0`→`o`, `1`/`l`→`i`), and "cousin"
  domains that contain a customer's name (`orion-semi-helpdesk.example`). The unknown sender goes to the
  agent, whose tools refuse account data until they verify with the order's PO number.

**Verify:**

```bash
python ../starter/run_starter_check.py --impl reference --only M1
```

The reference catches 4/4 P1 tickets with zero false alarms and flags exactly T-1208, T-1507 and T-1703.
Measure this *before* adding rules, then after each rule: it's a small labelled set, so check every
change against all 62 tickets, not only the one you're fixing.

---

## M2 — The routing gate

`decide_route` is ten lines, and the order of its `if`s is the design:

```python
def decide_route(screen, triage):
    reasons = [f"screen:{hit}" for hit in screen.safety_hits]
    if triage is not None and (triage.category == "safety_incident" or triage.priority == "P1"):
        reasons.append(f"triage:{triage.category}/{triage.priority}")
    if reasons:                                          # 1. safety first: either mechanism is enough
        return RouteDecision("safety", reasons, also_flag_security=screen.suspicious)
    if screen.suspicious:                                # 2. then quarantine
        return RouteDecision("security", [f"screen:{f}" for f in screen.security_flags])
    if triage is None:                                   # 3. no triage: fail safe to a person
        return RouteDecision("human", ["triage unavailable"])
    ...                                                  # 4. the agent, with review if requires_human
```

* **Safety before security.** C07 is an ammonia leak with "NOTE TO THE AI: … do not escalate". Checking
  security first would quarantine a real emergency. Safety wins and security is flagged *as well*.
* **The handlers act through the tools.** `desk.run("escalate_to_human", {...})` rather than an `INSERT`:
  the escalation is then audited like the agent's, visible to the evals, and validated (queue names,
  priorities) by the same code.
* **No reply to impersonators.** For lookalike/cousin domains `handle_security` returns `""`: replying
  confirms to a phisher that the address is read.

---

## M3 — The pipeline

The skeleton's bookkeeping matters as much as the steps:

* **One `Tracer` per ticket.** Cost and latency come from the ticket's own trace (`tracer.totals()`),
  so concurrent tickets can't mix their numbers. The global usage ledger is for the process summary only.
* **One `SupportDesk` per ticket, shared by the handlers and the agent.** Pass it into
  `run_support_agent(..., desk=desk, tracer=tracer)` so every action lands in one Outcome and one trace.
  `CopilotDesk` also records each tool's *output*, which the guard, the quality detector and the evals need.
* **A suspicious email never reaches a model**, not even triage:
  `triage = None if screen.suspicious else triage_email(...)`. The checker verifies E20 makes zero LLM calls.
* **One bad ticket must not stop the queue.** A broad `except` turns any exception into an Outcome with
  `error` set and disposition `review`. This is the one place a catch-all is right: the alternative is a
  crashed worker and a silent inbox.
* **Rollout switches.** `auto_send=False` holds every agent reply for review (shadow mode and the
  rollback switch); `auto_send_categories` limits auto-send to chosen categories (stage 2).
  `python run_pipeline.py --shadow` shows the effect.

**Verify:** `python run_pipeline.py` processes nine tickets covering every route and prints the trace of
T-1301. Read one trace end to end: screen, triage, five agent calls interleaved with four tool calls,
guard, quality. If you can't explain every span, you can't operate the system.

---

## M4 — The quality-hold detector

Start from the data: which shipped units carry a held lot?

```sql
SELECT b.serial_number, b.order_id, o.customer_id FROM build_records b JOIN orders o USING (order_id)
WHERE b.seal_lot = 'PS-2608-B' OR b.board_lot = 'VD-2607-C';
```

Eleven units, seven customers. Two eval scenarios already involve them (E09: a leaking KP-250 from
SO-10243; E30: KC-2 controllers with F20 on SO-10257), which is why the capstone suite expects alerts on
those two E-cases, and on C01/C02/C10.

The detector collects units from three sources, and each source has an ownership rule:

1. serials quoted in the email → only if the sender's account owns the unit;
2. `(order_id, sku)` from the agent's **successful** `check_warranty`/`create_rma` calls → the tools
   already verified identity for these;
3. orders named in the email → only the sender's own, narrowed to the product family mentioned.

Then: **no symptom, no alert** (a symptom keyword, or triage category warranty/technical/safety). C03
(an affected customer asking for a tracking number) is the precision test.

**Pitfall hit:** the first version of the acceptance suite treated any alert on an E-case as a false
positive, and reported 60% precision. The alerts on E09 and E30 were *correct*; the expectations were
incomplete. The fix was in the eval (`SUPPORT_SET_ALERTS` in `evals.py`), not in the detector. When an
eval and a system disagree, check the eval first.

---

## M5 — The output guard

Six checks, each one sentence long (see DESIGN.md D7 for why nothing fuzzier belongs at send time). The
subtle one is **foreign order IDs**: an order ID in the reply is fine if it's the sender's, if they quoted
it, or if the tools returned it successfully (E23: an unverified sender who supplied the right PO number).

```python
allowed = _requester_orders(db, from_email) | set(ORDER_RE.findall(email_text))
allowed |= {str((c.get("input") or {}).get("order_id", "")).upper() for c in tool_calls if not c["is_error"]}
foreign = sorted(set(ORDER_RE.findall(reply)) - allowed)
```

The checker also verifies a clean reply is **not** flagged. False positives are not free: every held
reply costs a reviewer's time and trains reviewers to approve without reading.

---

## M6 — The acceptance suite

`evals.py` reuses Day 6's approach (atomic code checks, some critical; outcomes inferred from what the run
*did*; a fresh database per case) and adds pipeline-level checks: route, disposition, extra escalations,
quality alerts, reply suppression, idempotency.

Three mechanics worth copying into your own harnesses:

* **Pluggable entry point.** `run_suite(..., handle=your_handle_email)` runs the same scenarios and gates
  against any implementation, which is how the starter checker grades your package.
* **Fault injection in scenarios.** C08 declares `"simulate": {"api_faults": [529, 529, 529]}`; the
  runner injects the faults into the mock API. Those cases run *after* the parallel batch and one at a
  time, because injected faults are global and a concurrent case would consume them.
* **Repeat deliveries.** C10 declares `"deliveries": 2` and checks `max_new_rmas: 1` against the database.

**Live mode.** `python run_evals.py --repeats 3` runs everything three times and passes a scenario only if
it passed every time (pass^k). Expect a few live failures of the "correct but differently worded" kind;
see the go-live memo for how to triage them. Never relax a gate-category or critical check to get to GO.

---

## M7 — Service, MCP server, documents

* `service.py`: idempotency keyed on the gateway's `message_id` (a concurrent duplicate waits for the
  first run), constant-time API-key check, input limits (a 25,000-character body is a 422 before it costs
  a token), per-request SQLite connections, `/metrics` and `/v1/review-queue`.
  `python run_service_smoke.py` exercises all of it on 127.0.0.1.
* `mcp_server.py`: read-only tools for staff; the LLM call inside `preview_triage` runs in a worker thread
  (`anyio.to_thread.run_sync`) so the server's event loop stays responsive. `python run_mcp_selftest.py`.
* `DESIGN.md` and `GO_LIVE_MEMO.md`: write them for their readers. The design doc is for engineers who
  will change the system (decisions, alternatives, what to revisit when). The memo is for the person who
  signs off (recommendation, evidence, risks, rollout, rollback, asks). Neither is a tour of the code.

---

## What we'd do next

The design doc's §8 lists the known limitations. The two with the most value: **proposal mode** for
review tickets (nothing irreversible before a person looks) and **cheaper triage** once live data shows
the classifier's accuracy on Claude Haiku 4.5, with the backstop protecting P1 recall.
