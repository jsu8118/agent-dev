"""Mock policies for the advanced capstone's agents (outreach and inbound).

They stand in for the model's judgement the way the rest of the course's policies do: every decision is derived
from the request - the JSON brief in the user message and the tool results in the conversation - and nothing
else. Intent detection is a handful of regexes where a real model reads the reply; the tool sequencing (read
before write, one email per outreach run, check history before marking anything remediated) is what a
well-prompted model does and what the capstone's evals check.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from labkit.mock import MockRequest, Reply, say, scenario, tool, use_tools

LOT_NAME = {"seal_lot": "seal cartridge lot", "board_lot": "controller board lot"}


def _brief(req: MockRequest) -> dict:
    try:
        return json.loads(req.first_user_text)
    except ValueError:
        return {}


def _last_json(req: MockRequest, name: str) -> dict:
    calls = req.calls(name)
    data = calls[-1].result_json() if calls else None
    return data if isinstance(data, dict) else {}


def _step(name: str, **kw) -> Reply:
    return use_tools(tool(name, **kw))


# ======================================================================= outreach
@scenario("adv.capstone.outreach", match=lambda r: "<adv_capstone_outreach>" in r.system_text, priority=20)
def outreach(req: MockRequest) -> Reply:
    brief = _brief(req)
    for name in ("get_campaign_brief", "get_contacts", "list_units"):
        if not req.called(name):
            kw = {"customer_id": brief.get("customer", {}).get("customer_id", "")} if name != "get_campaign_brief" else {}
            return _step(name, **kw)
    if not req.called("send_email"):
        campaign, contacts, units = _last_json(req, "get_campaign_brief"), _last_json(req, "get_contacts"), _last_json(req, "list_units")
        if "error" in contacts or "error" in units or "error" in campaign:
            return _step("escalate", queue="account_management", priority="P3", summary="Could not read the customer's record; please contact manually.")
        primary = next((c for c in contacts.get("contacts", []) if c["role"] == "purchasing"), None) or (contacts.get("contacts") or [None])[0]
        if primary is None:
            return _step("escalate", queue="account_management", priority="P2", summary="No active contact on file for the recall notice.")
        unit_list = units.get("units", [])
        lot = unit_list[0]["lot"] if unit_list else ""
        lot_info = campaign.get("lots", {}).get(lot, {})
        spanish = brief.get("customer", {}).get("language") == "es"
        serials = ", ".join(u["serial"] for u in unit_list)
        remedy = (unit_list[0]["remedy"] if unit_list else "").replace("_", " ")
        body = (f"Hello {primary['name']},\n\n"
                + ("(Versión en español disponible a petición.)\n\n" if spanish else "")
                + f"Kestrel is running field replacement campaign RC-2026-03 for {LOT_NAME.get(lot_info.get('kind', ''), 'lot')} {lot}. "
                f"The following units at your site are affected: {serials}.\n\n"
                f"Hazard: {lot_info.get('hazard', '')}\n\n"
                f"Interim measures until the visit: {lot_info.get('interim', '')}\n\n"
                f"Remedy: {lot_info.get('remedy', '')} The visit and the parts are free of charge.\n\n"
                f"Please reply with two or three dates in the next two weeks when an engineer can access the {remedy} work, "
                f"and the name of the person on site we should coordinate with.\n\n"
                f"Kestrel recall team (recall@kestrel-pumps.example)")
        return _step("send_email", contact_id=primary["contact_id"], template="TPL-RC-01",
                     subject=f"Recall RC-2026-03 - action required for {serials}", body=body)
    sent = _last_json(req, "send_email")
    if "error" in sent:
        return _step("escalate", queue="account_management", priority="P2", summary=f"Notice could not be sent: {sent['error']}")
    return say(f"Initial notice sent to {sent.get('to')} (message {sent.get('message_id')}).")


# ======================================================================= inbound
DATE_WORDS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8, "september": 9,
              "october": 10, "november": 11, "december": 12}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _date_in(text: str, today: dt.date) -> dt.date | None:
    m = re.search(r"\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b", text, re.I)
    if m:
        return dt.date(today.year, DATE_WORDS[m.group(2).lower()], int(m.group(1)))
    return None


def _intent(text: str, flags: list[str]) -> str:
    t = text.lower()
    if "safety_event" in flags:
        return "safety_event"
    if "legal" in flags:
        return "legal"
    if "out_of_office" in flags:
        return "out_of_office"
    if re.search(r"left the company|no longer work|stop emailing me", t):
        return "wrong_contact"
    if "compensation" in flags and re.search(r"expect|compensation|downtime", t):
        return "compensation"
    if re.search(r"already (had|been)|same fix|under ticket", t):
        return "already_remediated"
    if re.search(r"espa[ñn]ol|documentaci[óo]n", t):
        return "language"
    if re.search(r"in writing|bulletin|hse file|send the safety", t):
        return "documents"
    if re.search(r"crated|own fitters|does the .* also need|can you ship the kit", t):
        return "clarify"
    if re.search(r"confirmed for the slot|works\.|works for us|please confirm the engineer", t):
        return "confirm"
    if re.search(r"host|slot|visit|week of|earliest|can take a stop|any afternoon|morning", t):
        return "schedule"
    return "unclear"


def _constraints(text: str) -> dict:
    t = text.lower()
    out: dict = {}
    if "afternoon" in t or re.search(r"\b13:00\b|\b1 ?pm\b", t):
        out["afternoons_only"] = True
    if "morning" in t:
        out["mornings_only"] = True
    m = re.search(r"except (monday|tuesday|wednesday|thursday|friday)", t)
    if m:
        out["exclude_weekday"] = m.group(1).title()
    days = [d for d in WEEKDAYS[:5] if re.search(rf"\b{d}\b|\b{d[:3]}\b", t)]
    if days and "except" not in t:
        out["weekdays"] = [d.title() for d in days]
    if re.search(r"zone [12]|atex", t):
        out["extra_skill"] = "atex"
    return out


@scenario("adv.capstone.inbound", match=lambda r: "<adv_capstone_inbound>" in r.system_text, priority=20)
def inbound(req: MockRequest) -> Reply:
    brief = _brief(req)
    reply, flags = brief.get("reply", {}), brief.get("screen", {}).get("flags", [])
    text = f"{reply.get('subject', '')}\n{reply.get('body', '')}"
    today = dt.date.fromisoformat(brief.get("today", "2026-09-17"))
    contacts = brief.get("contacts", [])
    sender = brief.get("sender_contact_id") or (contacts[0]["contact_id"] if contacts else "")
    site = next((c["contact_id"] for c in contacts if c["role"] == "site engineer"), sender)
    units = brief.get("units", [])
    remedy = units[0]["remedy"] if units else "seal_kit_replacement"
    serials = [u["serial"] for u in units if u["remedy"] == remedy and u["status"] in ("pending", "contacted")]
    customer = brief.get("customer", {}).get("name", "your company")
    intent = _intent(text, flags)
    done = lambda name: req.called(name)  # noqa: E731

    if intent == "safety_event":
        if not done("escalate"):
            return _step("escalate", queue="quality", priority="P1",
                         summary=f"{customer} reports an operator sprayed with process fluid from a recalled unit ({reply.get('reply_id')}). Stop condition for the lot.")
        if not done("send_email"):
            return _step("send_email", contact_id=sender, template="TPL-RC-05", subject="RE: " + reply.get("subject", ""),
                         body=f"Hello {reply.get('from_name', '').split()[0]},\n\nThank you for telling us immediately; we are glad your colleague is unharmed. "
                              "Our quality lead has been paged and will call you within the hour. Until the seal is replaced, please keep the unit "
                              "stopped and isolated and do not run it unattended on hazardous duty, as described in the interim measures.\n\n"
                              "Kestrel recall team")
        return say("P1 quality escalation raised and the customer told to keep the unit stopped.")

    if intent == "legal":
        if not done("escalate"):
            return _step("escalate", queue="legal", priority="P2", summary=f"{customer} has placed communications under counsel and asserts liability for downtime; "
                                                                           "outreach stopped pending legal.")
        return say("Outreach stopped for this customer; the legal queue owns the thread now.")

    if intent == "out_of_office":
        if not done("schedule_retry"):
            back = _date_in(text, today) or (today + dt.timedelta(days=7))
            return _step("schedule_retry", not_before=back.isoformat(), reason="primary contact out of office; site contact to be tried")
        if not done("send_email"):
            return _step("send_email", contact_id=site, template="TPL-RC-01", subject="Recall RC-2026-03 - site contact",
                         body=f"Hello,\n\nYour purchasing contact is out of office, so we are writing to you as the site engineer about recall "
                              f"RC-2026-03 (units {', '.join(serials)}). Please reply with dates when an engineer can visit.\n\nKestrel recall team")
        return say("Retry scheduled for the primary contact and the site engineer notified.")

    if intent == "wrong_contact":
        if not done("send_email"):
            return _step("send_email", contact_id=site, template="TPL-RC-01", subject="Recall RC-2026-03 - action required",
                         body=f"Hello,\n\nWe are contacting you as the site engineer for recall RC-2026-03 (units {', '.join(serials)}); our "
                              "previous contact has left the company. Please reply with two or three dates for the engineer's visit.\n\nKestrel recall team")
        if not done("escalate"):
            return _step("escalate", queue="account_management", priority="P3", summary=f"Primary contact at {customer} has left the company; CRM record needs updating.")
        return say("Notice re-sent to the site engineer; account management asked to update the contact.")

    if intent == "compensation":
        if not done("send_email"):
            return _step("send_email", contact_id=sender, template="TPL-RC-05", subject="RE: " + reply.get("subject", ""),
                         body=f"Hello {reply.get('from_name', '').split()[0]},\n\nThe replacement, parts and visit are free of charge and we will schedule "
                              "them at your earliest convenience. I have passed your downtime claim to your account manager, who will come back to you "
                              "on it separately.\n\nKestrel recall team")
        if not done("escalate"):
            return _step("escalate", queue="account_management", priority="P2", summary=f"{customer} claims $6,800 of downtime; relationship at risk.")
        if not done("request_goodwill_credit"):
            return _step("request_goodwill_credit", amount_usd=750.0, reason="third fault on recalled controllers; downtime claim under review")
        credit = _last_json(req, "request_goodwill_credit")
        if "error" in credit:
            return say("Acknowledged without promises and escalated; the goodwill credit was declined: " + credit["error"])
        return say(f"Acknowledged without promises, escalated to account management, goodwill credit {credit.get('credit_id')} recorded.")

    if intent == "already_remediated":
        target = serials[0] if serials else (units[0]["serial"] if units else "")
        if not done("get_service_history"):
            return _step("get_service_history", serial=target)
        history = _last_json(req, "get_service_history")
        if history.get("visits"):
            return say("A completed visit exists for this unit; asking field service to confirm before closing.")
        if not done("escalate"):
            return _step("escalate", queue="field_service", priority="P3",
                         summary=f"{customer} says the board was already replaced under ticket SVC-3990 on 9 September; no campaign record - verify before closing.")
        if not done("send_email"):
            return _step("send_email", contact_id=sender, template="TPL-RC-05", subject="RE: " + reply.get("subject", ""),
                         body=f"Hello {reply.get('from_name', '').split()[0]},\n\nThank you - if the board fitted on 9 September was revision D with firmware "
                              "2.4.1, that is the same fix. Our field-service team will confirm from the service report and close the unit; if the "
                              "report shows an earlier board, we will schedule the swap.\n\nKestrel recall team")
        return say("Claim not taken as evidence: field service asked to verify the earlier repair before the unit is closed.")

    if intent == "language":
        if not done("send_email"):
            return _step("send_email", contact_id=sender, template="TPL-RC-05", subject="RE: " + reply.get("subject", ""),
                         body=f"Hola {reply.get('from_name', '').split()[0]},\n\nGracias por su mensaje. Le enviaremos la documentación de la campaña "
                              "RC-2026-03 en español (aviso, medidas provisionales y descripción de la reparación) en las próximas 24 horas. La visita "
                              "y las piezas son gratuitas.\n\nEquipo de retirada de Kestrel")
        if not done("escalate"):
            return _step("escalate", queue="account_management", priority="P3", summary=f"{customer} needs the recall documentation in Spanish.")
        return say("Replied in Spanish and asked account management for the translated documents.")

    if intent == "documents":
        if not done("send_email"):
            return _step("send_email", contact_id=sender, template="TPL-RC-05", subject="RE: " + reply.get("subject", ""),
                         body=f"Hello {reply.get('from_name', '').split()[0]},\n\nAttached for your HSE file: safety bulletin TSB-2026-09 for campaign "
                              "RC-2026-03 and the interim measures (reduce the seal-chamber temperature alarm to 70 C; do not run unattended on hazardous "
                              "duty until the cartridge is replaced). The bulletin is also at https://www.kestrel-pumps.example/bulletins/TSB-2026-09.\n\n"
                              "Once you have reviewed it, reply with dates for the visit.\n\nKestrel recall team")
        return say("Sent the bulletin and the interim measures in writing.")

    if intent == "clarify":
        if not done("send_email"):
            return _step("send_email", contact_id=sender, template="TPL-RC-05", subject="RE: " + reply.get("subject", ""),
                         body=f"Hello {reply.get('from_name', '').split()[0]},\n\nBoth units need the remedy, including the crated one: the cartridge "
                              "fails under thermal cycling regardless of running hours, so the crated pump would fail after installation. We can "
                              "replace both in one visit. Kits are shipped for self-fitting only where a Kestrel-certified fitter signs the job off; "
                              "otherwise our engineer does it, free of charge. Reply with dates and we will book it.\n\nKestrel recall team")
        return say("Answered from the campaign brief: both units need the remedy; self-fitting only with a certified fitter.")

    # scheduling: confirm a proposed slot, or propose slots that honour the constraints
    constraints = _constraints(text)
    proposed = brief.get("proposed_slots", [])
    def _matches(start_iso: str) -> bool:
        start = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if constraints.get("weekdays") and start.strftime("%A") not in constraints["weekdays"]:
            return False
        if constraints.get("afternoons_only") and start.hour != 13:
            return False
        if constraints.get("mornings_only") and start.hour != 8:
            return False
        return True

    if intent == "confirm":
        pick = next((p for p in proposed if _matches(p["start"])), None)
        if pick is None and not constraints.get("extra_skill"):                 # they named a time we did not propose: find it
            if not done("get_engineer_slots"):
                return _step("get_engineer_slots", remedy=remedy, extra_skill="", not_before=today.isoformat(), not_after="",
                             afternoons_only=bool(constraints.get("afternoons_only")), mornings_only=bool(constraints.get("mornings_only")),
                             exclude_weekday="")
            pick = next((s for s in _last_json(req, "get_engineer_slots").get("slots", []) if _matches(s["start"])), None)
        if pick is not None:
            if not done("book_visit"):
                return _step("book_visit", slot_id=pick["slot_id"], serials=serials or [u["serial"] for u in units], contact_id=sender)
            booked = _last_json(req, "book_visit")
            if "error" in booked:
                if not done("escalate"):
                    return _step("escalate", queue="field_service", priority="P3", summary=f"Booking failed for {customer}: {booked['error']}")
                return say("Booking failed; field service asked to schedule manually.")
            return say(f"Visit booked: {booked.get('appointment_id')} on {booked.get('start')} with {booked.get('engineer')}.")
        # nothing proposed yet (or a hazardous-area skill is needed): propose qualified slots instead

    if not done("get_engineer_slots"):
        kw = {"remedy": remedy, "extra_skill": constraints.get("extra_skill", ""), "not_before": "", "not_after": "",
              "afternoons_only": bool(constraints.get("afternoons_only")), "mornings_only": bool(constraints.get("mornings_only")),
              "exclude_weekday": constraints.get("exclude_weekday", "")}
        m = re.search(r"week of (\d{1,2}) (\w+)", text, re.I)
        if m:
            kw["not_before"] = dt.date(today.year, DATE_WORDS[m.group(2).lower()], int(m.group(1))).isoformat()
        elif re.search(r"next (tuesday|wednesday|week)", text, re.I):
            kw["not_before"] = (today + dt.timedelta(days=(7 - today.weekday()))).isoformat()
        return _step("get_engineer_slots", **kw)
    slots = _last_json(req, "get_engineer_slots").get("slots", [])
    if constraints.get("weekdays"):
        slots = [s for s in slots if dt.date.fromisoformat(s["start"][:10]).strftime("%A") in constraints["weekdays"]] or slots
    if constraints.get("mornings_only"):
        slots = [s for s in slots if s["start"][11:13] == "08"] or slots
    if not slots:
        if not done("escalate"):
            return _step("escalate", queue="field_service", priority="P2", summary=f"No engineer slot matches {customer}'s constraints {constraints}; needs manual scheduling.")
        return say("No matching slot; field service asked to schedule manually.")
    if not done("propose_slots"):
        note = "Thank you for the dates. " + ("Our engineer for a Zone 2 pump room will be ATEX-qualified. " if constraints.get("extra_skill") else "")
        return _step("propose_slots", contact_id=sender, slot_ids=[s["slot_id"] for s in slots[:3]], note=note.strip())
    proposed_result = _last_json(req, "propose_slots")
    if "error" in proposed_result:
        return _step("escalate", queue="field_service", priority="P3", summary=f"Could not propose slots to {customer}: {proposed_result['error']}")
    return say(f"Proposed {len(proposed_result.get('proposed', []))} slots to the customer; waiting for their choice.")
