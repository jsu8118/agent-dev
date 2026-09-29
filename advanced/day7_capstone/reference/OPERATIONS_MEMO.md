# Operations memo: Recall Campaign Orchestrator (RC-2026-03)

*For the quality lead (campaign owner) and the on-call engineer. Everything below runs from
`advanced/day7_capstone/reference/` against the campaign's state file in `.runs/advanced_capstone/reference/`.*

## 1. Recommendation

Run the campaign with the orchestrator in **auto-send** mode for outreach and reminders, with these
holds in place: goodwill credits above $500 go to a support manager (built in), every quarantined reply
goes to the security queue (built in), and the first two business days are reviewed by a person each
evening (`run_ops.py`). In mock mode the acceptance suite passes all 19 scenario checks and 7 gates; the
live evidence (section 3) must be collected on day 1 before day 2 is dispatched.

## 2. Daily routine

```bash
python run_campaign.py --days 1          # dispatch today: outreach, replies, reminders, stop conditions
python run_ops.py                        # approvals waiting, stuck runs, SLA table, escalations
python run_ops.py --approve apr_xxx --by "your.name" [--note "..."]     # or --reject
```

The day report says what happened; the dashboard says where the campaign stands. Three lines matter most:
`STOP CONDITION`, `waiting for approval`, and the SLA table's `LATE` column.

## 3. Evidence to collect on the live day 1

* the seven outreach emails, read by a person before day 2 (hazard wording verbatim? interim measures present? no promises?);
* every quarantined reply, confirmed hostile by security (false quarantines cost minutes; a missed one costs parts);
* the model spend from the dashboard against the $25 cap, and the per-run cost (expect a few cents to a few tens of cents per run);
* one deliberate crash-and-resume (`--crash`) on the staging copy of the state file, with the outbound table compared before and after.

## 4. When something goes wrong

| Symptom | What it means | What to do |
|---|---|---|
| `STOP CONDITION: safety event ...` | an injury or fluid release was reported; the lot is paused, quality was paged (P1) | quality reviews; when cleared, `run_ops.py --resume-lot PS-2608-B --by name --note "..."` |
| `STOP CONDITION: parts: ... exhausted` | no kits left for a remedy; scheduling for it is refused | logistics transfers stock; then `--resume-remedy <remedy>` |
| `STOP CONDITION: budget ...` | model spend reached the cap; nothing more is dispatched | review spend; raise the cap in `config.py` deliberately, then `--resume-campaign` |
| `Stuck runs` lists a run | a worker died mid-run (lease expired) | `run_ops.py --resume <run_id>`; the run replays what it already did |
| a customer is `held:legal` | a legal notice arrived; outreach stopped, thread owned by legal | nothing automatic; legal decides |
| `guard tripped: loop` / `circuit_breaker` | the model repeated a call or a tool kept failing in a run; the run ended with an escalation | read `--replay <run_id>`; fix the tool or the prompt; re-run the reply later if needed |
| an email looks wrong | the output guard blocks other customers' names, promises and external links, not tone | `--shadow` mode holds every outbound email for review; use it for day 1 if unsure |

## 5. Pause, resume, roll back

* **Pause everything:** stop calling `run_campaign.py`. Nothing runs between days; state is on disk.
* **Pause part:** the stop conditions above, or edit `paused_lots` / `paused_remedies` through `run_ops.py`.
* **Resume:** `run_campaign.py --days 1` picks up interrupted runs first (`resumed runs:` in the report).
* **Roll back to manual:** switch to `--shadow`; every outbound email is recorded as held and a person sends it.
  Nothing already sent can be unsent — which is why sends are keyed and audited.
* **Investigate:** `run_ops.py --forensics <reply_id>` (who told the agent what) and `--replay <run_id>` (every turn and tool call).

## 6. Risks

| Risk | Mitigation |
|---|---|
| a hostile reply phrased outside the screen's patterns reaches the agent | the agent's tools refuse the dangerous outcomes (unknown recipient, remediation without evidence, other customers); add a classifier in front (Day 5) before scaling to more campaigns |
| a live model proposes slots that ignore a constraint | the scheduling check in the acceptance suite; review day 1's proposals by hand |
| approvals wait too long and the SLA slips | the dashboard's SLA table; a second approver role |
| the cap is reached mid-campaign | spend is checked before every run; raise deliberately, never silently |

## 7. Asks

A named security contact for the quarantine queue; a support manager on rota for approvals during the
first week; agreement on the $25 model cap; and a decision on the phone channel for silent customers.
