# Go-live memo: Service Desk Copilot

**To:** Head of Customer Service · **Cc:** COO, Director of Quality, Security lead
**From:** Support Engineering · **Decision requested:** approve a staged rollout starting with shadow mode

> This is the reference example of the capstone's M7 deliverable. Its mock-mode numbers are real
> (`python run_evals.py`). The **live** numbers must come from your own live run; the placeholders
> below show where they go and how to read them. A memo that quotes only mock results is not ready.

## 1. Recommendation

**Go, in three stages**, each gated on measured exit criteria (section 5): **shadow** (the copilot
drafts, humans send everything), then **assisted** (auto-send only for order-status, tracking, knowledge
and billing-information replies; everything else reviewed), then **general** (auto-send wherever the
disposition is `sent`). Safety and security handling is deterministic and can go live in stage 1
unchanged: it only adds an immediate P1 page and SOP instructions to what humans do today.

## 2. What it does

Every inbound email is screened (code), triaged (Claude), and routed: safety reports page the on-call
engineer and get the SOP instructions in seconds; suspicious emails are quarantined for security without
any model reading them; everything else is answered by the support agent, whose tools enforce refund
limits, identity checks and return/warranty policy. Replies pass a send-time guard. If a customer
reports a symptom on a unit built with a held supplier lot, Quality is alerted the same day.

## 3. Evidence

### Acceptance suite (40 scenarios: 30 support + 10 capstone)

| Criterion | Target | Mock run (mechanics) | Live run (quality) |
|---|---|---|---|
| Safety scenarios | 100% | 100% (6/6) | *fill in* |
| Security scenarios | 100% | 100% (3/3) | *fill in* |
| Privacy scenarios | 100% | 100% (3/3) | *fill in* |
| Critical failures | 0 | 0 | *fill in* |
| Overall pass rate | ≥ 90% | 100% (40/40) | *fill in* (expect 90–100%) |
| Quality-alert recall / precision | 100% / ≥ 90% | 100% / 100% | *fill in* |
| Mean cost per ticket | ≤ $0.40 | $0.025 (simulated) | *fill in* (estimate $0.07–0.10) |
| p95 latency | ≤ 30 s | n/a | *fill in* |

**How to read the two columns.** The mock run proves the mechanics: every route, gate, guard, alert and
idempotency rule behaves as specified, including outages (C08) and duplicate delivery (C10). It says
nothing about how well a live model writes replies. The live run (`python run_evals.py --repeats 3`,
≈ $6–12) is the evidence for quality; with three repeats a scenario passes only if it passes every time
(pass^3), which is what customers experience.

**If the live run shows failures:** classify each one before acting. (a) A gate category failure
(safety/security/privacy) or any critical failure is a **blocker**, whatever the overall rate. (b) A
wording miss where the reply is still correct ("ETA 17 Sept" vs "September 17") is a scenario to fix,
with the reason recorded. (c) A defensible but different path (the agent escalates where the scenario
expected an RMA) is discussed with the support manager before changing either side. 1–2 non-critical
failures out of 40 are within noise (Day 6: a 95% interval for 38/40 spans roughly 83–99%).

### On the real inbox (62 labelled tickets, mock mode)

`python run_pipeline.py --all`: the input screen catches all 4 P1 tickets with no false alarms and
quarantines exactly the 3 malicious emails (injections T-1208 and T-1507, impersonation T-1703). The
safety backstop does not depend on the classifier: in C05 (smoke reported in Spanish) it routes to the
safety path even if triage misses it.

## 4. Risks

| Risk | Likelihood | Impact | Mitigation | Residual |
|---|---|---|---|---|
| A live model's reply is wrong in a way no scenario covers | medium | medium | shadow stage with 100% human review; weekly sampled review in later stages; grow the eval set from every correction | low after shadow |
| Safety email missed by both the rules and triage (novel phrasing, new language) | low | high | two independent mechanisms; the agent's own prompt escalates safety too; monitor for P1s found by humans in reviewed tickets | low |
| False safety alarms page engineers | low | low–medium | two-condition rules; track weekly; tune | accepted |
| Reviewers rubber-stamp drafts | medium | medium | keep the review rate low (guard checks are near-zero false positive); show the guard's reason and the trace | medium: manage |
| Agent acts (within policy) on a ticket later judged `review` | low | low | writes are policy-limited and reversible; proposal mode before adding irreversible tools | low |
| Latency above 30 s on long multi-tool tickets | medium | low (email channel) | measure p95 live; lower `effort` for routine categories; see ask 3 | depends on live data |
| Model or prompt change regresses behaviour | medium | medium | the suite runs in CI on every change; live suite before each release | low |

## 5. Rollout plan and exit criteria

| Stage | Duration | What happens | Exit criteria to move on |
|---|---|---|---|
| 1. Shadow | 2 weeks | the copilot processes every email; safety/security handling is live; all agent replies go to the review queue; humans send | ≥ 300 tickets; reviewers accept ≥ 85% of drafts unchanged or with minor edits; zero safety/security misses; live suite GO |
| 2. Assisted | 2–4 weeks | auto-send for order status, tracking, knowledge and billing-information replies with disposition `sent`; everything else reviewed | acceptance of auto-sent replies (sampled 10%) ≥ 95%; complaint rate not above baseline; mean cost ≤ $0.20 |
| 3. General | ongoing | auto-send wherever disposition is `sent` | monthly review of metrics and a 5% sample |

**Rollback:** one configuration switch (`auto_send=False` in `CopilotConfig`) routes every agent reply to
`review` (stage 1 behaviour); the deterministic safety/security paths stay on. Trigger it on any safety/security miss, a
critical eval failure after a change, or an error rate above 2%.

## 6. Monitoring

Dashboards from `/metrics` and the traces: tickets by route and disposition, review rate, quarantines,
safety pages, quality alerts, errors, mean cost per ticket, p95 latency, refusal/fallback rate. Alerts:
any pipeline error; safety-route or quarantine spikes; review rate > 15%; mean cost > $0.20; fallbacks > 1%.

## 7. Asks

1. **Safety officer:** approve the SOP reply templates (en/es/de) word for word.
2. **Security:** own the quarantine queue (SLA: same business day) and confirm the no-reply rule for
   impersonating domains.
3. **Leadership:** confirm that the 30-second latency constraint applies to the portal chat, not email.
   For email, customers compare us with 9.5 business hours; a p95 of 60 s would change nothing for them
   and would let us keep the agent at default effort. We will meet 30 s for chat regardless.
4. **Quality:** name the recipient of alerts during stage 1 and agree on the QMS integration for stage 2.
5. **Budget:** ≈ $150–200/month in API usage at current volume (estimate; replaced by live data after stage 1),
   plus ~$10 per release for the live acceptance run.
