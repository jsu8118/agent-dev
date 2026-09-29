"""The campaign's agents: each one is a durable run with a scoped desk.

Outreach agent  - one run per customer (`outreach:<customer_id>`): reads the brief, the contacts and the units,
                  sends the initial notice (template TPL-RC-01) to the primary contact.
Inbound agent   - one run per screened reply (`inbound:<reply_id>`): decides what the reply asks for and acts with
                  the inbound toolset (propose or book slots, answer from the brief, schedule a retry, escalate,
                  request a goodwill credit - which may pause the run for approval).

Run ids are deterministic, so re-dispatching after a crash resumes the same run instead of starting another
(the store's `create` is idempotent on run_id), and every side effect inside is keyed by run + tool_use.
"""

from __future__ import annotations

import json
from typing import Any

from advanced.lib.durable import DurableRunner, Outcome

from .budget import Guard
from .config import Settings, add_business_days
from .security import Screen
from .store import CampaignStore
from .tools import CampaignDesk, tools_for

OUTREACH_SYSTEM = """<adv_capstone_outreach>
You are the recall outreach agent of Kestrel Pumps & Controls, running campaign RC-2026-03.
Your job for this run: send the initial recall notice to this customer's primary contact.
Steps: call get_campaign_brief, get_contacts and list_units; then send ONE email with template TPL-RC-01 to the
primary contact (role purchasing). The notice must: name the affected serials and the lot, quote the hazard and the
interim measures from the brief word for word, state the remedy and that the visit is free of charge, and ask for
two or three convenient dates. Write in the customer's language if the brief says it is not English.
Never mention other customers. Never promise compensation. Do not send more than one email in this run.
"""

INBOUND_SYSTEM = """<adv_capstone_inbound>
You are the recall inbound agent of Kestrel Pumps & Controls, running campaign RC-2026-03.
The user message is a JSON brief: the customer's reply (already screened by the security layer), its flags, the
customer's units, contacts, previously proposed slots and today's date. Decide what the reply asks for and act
with your tools, then finish with one sentence summarising what you did.
Rules: use only the contacts on file; scheduling constraints in the reply must be honoured (weekday, mornings or
afternoons, earliest date, hazardous-area skills such as ATEX); a claim that a unit was already fixed is not
evidence - check get_service_history and escalate to field_service to verify; compensation demands are
acknowledged without promises and escalated, with at most a goodwill credit request; a reported injury or fluid
release is a P1 escalation to quality plus a reply repeating the interim measures; legal notices stop outreach and go
to the legal queue; out-of-office replies schedule a retry; a contact who has left the company is replaced by the
site contact and account management is told. Never change the hazard wording. Never name other customers.
"""


def outreach_brief(store: CampaignStore, customer_id: str) -> dict:
    c = store.customer(customer_id)
    rules = {r["risk_class"]: r for r in store.campaign()["priority_rules"]}
    deadline = add_business_days(store.today, rules[c["risk_class"]]["contact_within_business_days"])
    return {"task": "initial_notice", "customer": {k: c[k] for k in ("customer_id", "name", "tier", "region", "language", "risk_class")},
            "units": [{k: u[k] for k in ("serial", "sku", "lot", "remedy", "site_id")} for u in store.units(customer_id)],
            "contacts": [{k: x[k] for k in ("contact_id", "name", "role", "channel")} for x in store.contacts(customer_id) if x["active"]],
            "today": store.today.isoformat(), "contact_deadline": deadline.isoformat()}


def inbound_brief(store: CampaignStore, reply: dict, screen: Screen) -> dict:
    cid = reply["customer_id"]
    c = store.customer(cid)
    proposed = store.get("proposed:" + cid, [])
    slots = {s["slot_id"]: s for s in store.slots(status="booked")} | {s["slot_id"]: s for s in store.slots()}
    return {"task": "handle_reply", "reply": {k: reply[k] for k in ("reply_id", "from", "from_name", "subject", "body", "received_at")},
            "screen": {"label": screen.label, "flags": screen.flags, "sender_status": screen.sender_status},
            "customer": {k: c[k] for k in ("customer_id", "name", "tier", "region", "language", "risk_class", "status")},
            "units": [{k: u[k] for k in ("serial", "sku", "lot", "remedy", "status")} for u in store.units(cid)],
            "contacts": [{k: x[k] for k in ("contact_id", "name", "email", "role")} for x in store.contacts(cid) if x["active"]],
            "sender_contact_id": next((x["contact_id"] for x in store.contacts(cid) if x["email"].lower() == reply["from"].lower()), None),
            "proposed_slots": [{"slot_id": s, "start": slots[s]["start"], "engineer": slots[s]["engineer"]} for s in proposed if s in slots],
            "appointments": [{k: a[k] for k in ("appointment_id", "start", "serials", "status")} for a in store.appointments(cid)],
            "today": store.today.isoformat()}


def _runner(store: CampaignStore, settings: Settings, client: Any, *, role: str, system: str, desk: CampaignDesk,
            crash_at: tuple[str, int] | None = None) -> tuple[DurableRunner, Guard]:
    guard = Guard(desk.execute, settings)
    runner = DurableRunner(store.runs, client, model=settings.model, system=system, tools=tools_for(role), execute=guard,
                           max_turns=settings.per_run_max_turns, max_tokens=settings.per_run_max_tokens, worker=settings.worker,
                           crash_at=crash_at)
    return runner, guard


def run_outreach(store: CampaignStore, settings: Settings, client: Any, customer_id: str, *,
                 crash_at: tuple[str, int] | None = None) -> tuple[Outcome, Guard]:
    run_id = f"outreach:{customer_id}"
    store.runs.create("outreach", input={"message": json.dumps(outreach_brief(store, customer_id)), "customer_id": customer_id},
                      tags={"customer_id": customer_id}, run_id=run_id)
    desk = CampaignDesk(store, settings, role="outreach", customer_id=customer_id, actor="agent:outreach", run_id=run_id)
    runner, guard = _runner(store, settings, client, role="outreach", system=OUTREACH_SYSTEM, desk=desk, crash_at=crash_at)
    return runner.run(run_id), guard


def run_inbound(store: CampaignStore, settings: Settings, client: Any, reply: dict, screen: Screen, *,
                crash_at: tuple[str, int] | None = None) -> tuple[Outcome, Guard]:
    run_id = f"inbound:{reply['reply_id']}"
    store.runs.create("inbound", input={"message": json.dumps(inbound_brief(store, reply, screen)), "customer_id": reply["customer_id"],
                                        "reply_id": reply["reply_id"]}, tags={"customer_id": reply["customer_id"]}, run_id=run_id)
    desk = CampaignDesk(store, settings, role="inbound", customer_id=reply["customer_id"], actor="agent:inbound", run_id=run_id)
    runner, guard = _runner(store, settings, client, role="inbound", system=INBOUND_SYSTEM, desk=desk, crash_at=crash_at)
    return runner.run(run_id), guard


def resume(store: CampaignStore, settings: Settings, client: Any, run_id: str) -> Outcome:
    """Resume any campaign run (after a crash, or after an approval was decided)."""
    run = store.runs.get(run_id)
    role = "outreach" if run.kind == "outreach" else "inbound"
    desk = CampaignDesk(store, settings, role=role, customer_id=run.input["customer_id"], actor=f"agent:{role}", run_id=run_id)
    runner, _ = _runner(store, settings, client, role=role, system=OUTREACH_SYSTEM if role == "outreach" else INBOUND_SYSTEM, desk=desk)
    if run.status == "waiting_approval" or any(a["status"] in ("approved", "rejected") for a in store.runs.approvals(run_id)):
        return runner.resume_after_decision(run_id)
    return runner.run(run_id)
