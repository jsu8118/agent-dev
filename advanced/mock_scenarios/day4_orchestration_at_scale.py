"""Mock policies for Day 4 - multi-agent orchestration at scale (advanced/day4_orchestration_at_scale/labs).

Every policy matches on a marker in the system prompt (`<adv_day4_...>`), so it never captures another day's
requests, and derives everything it "says" from the request: tool results, the user message, the envelope it
was sent.  The policies never read the dataset.  Scripted misbehaviour (a worker that loops, a worker that
never stops exploring) is opt-in through marker attributes and is labelled [mock] by the labs that use it.

Section index
    unit planning ....... the worker (one batch of units) and the single agent (all units, one context)
    coordinator ......... list -> dispatch -> collect -> summary (labs 01, 02, 03, 07)
    A2A roles ........... coordinator triage, quality lead, unit workers (lab 04)
    hosted agents ....... Managed Agents worker, coordinator roster, investigator, planner (labs 05, 06)
"""

from __future__ import annotations

import json
import re
from typing import Any

from labkit.mock import MockRequest, Reply, json_reply, say, scenario, tool, use_tools


# ============================================================================ shared helpers
def _attr(text: str, tag: str, name: str) -> str | None:
    m = re.search(rf"<{tag}\s[^>]*\b{name}=\"([^\"]*)\"", text)
    return m.group(1) if m else None


def _between(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}(?:\s[^>]*)?>\s*(.*?)\s*</{tag}>", text, re.S)
    return m.group(1) if m else None


def _json_between(text: str, tag: str) -> Any:
    raw = _between(text, tag)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _serials_in(text: str) -> list[str]:
    seen: list[str] = []
    for s in re.findall(r"\b(?:KP\d{3}|KC\d)-\d{4}-\d{4}\b", text):
        if s not in seen:
            seen.append(s)
    return seen


def _ok(call) -> Any:
    """The parsed result of a successful tool call, else None."""
    if call is None or call.is_error:
        return None
    return call.result_json()


# ============================================================================ unit planning (worker + single agent)
class _UnitState:
    """What the conversation so far says about one serial - read from tool calls, never from the dataset."""

    def __init__(self, req: MockRequest, serial: str) -> None:
        self.serial = serial
        self.unit_call = next((c for c in reversed(req.calls("get_unit")) if c.input.get("serial") == serial), None)
        self.unit = _ok(self.unit_call)
        u = self.unit or {}
        self.contacts = next((_ok(c) for c in reversed(req.calls("get_contacts"))
                              if c.input.get("customer_id") == u.get("customer_id") and _ok(c)), None)
        self.parts = next((_ok(c) for c in reversed(req.calls("check_parts"))
                           if c.input.get("kit_sku") == u.get("kit_sku") and _ok(c)), None)
        self.slot_calls = [c for c in req.calls("find_engineer_slots") if c.input.get("region") == u.get("region")
                           and c.input.get("skill") == u.get("skill_required")]
        self.reserve_calls = [c for c in req.calls("reserve_kit") if c.input.get("serial") == serial]
        self.book_calls = [c for c in req.calls("book_slot") if c.input.get("serial") == serial]
        self.reservation = next((_ok(c) for c in reversed(self.reserve_calls) if _ok(c)), None)
        self.booking = next((_ok(c) for c in reversed(self.book_calls) if _ok(c)), None)

    @property
    def scheduling_down(self) -> bool:
        return bool(self.slot_calls) and self.slot_calls[-1].is_error

    def gave_up_parts(self) -> bool:
        return self.reservation is None and len(self.reserve_calls) >= 3

    def gave_up_slot(self) -> bool:
        return self.booking is None and (self.scheduling_down or len(self.book_calls) >= 3)

    def status(self) -> str:
        if self.reservation and self.booking:
            return "scheduled"
        if self.reservation is None and self.gave_up_parts():
            return "waiting_parts"
        return "pending_schedule"

    def contact(self) -> dict | None:
        c = self.contacts or {}
        if not c:
            return None
        if "out of office" in (c.get("notes") or "").lower():
            return c.get("site")
        return c.get("primary")


def _stock_from_error(text: str) -> dict:
    """The stock map inside a reserve_kit error ('... out of stock at X; stock elsewhere: {...}')."""
    try:
        payload = json.loads(text or "")
        text = payload.get("error", "") if isinstance(payload, dict) else text
    except ValueError:
        pass
    m = re.search(r"stock elsewhere: (\{.*?\})", text or "")
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


def _next_step(req: MockRequest, st: _UnitState, booked_elsewhere: set[str]) -> list[dict] | None:
    """Tool calls for the unit's next phase, or None when the unit is finished (scheduled or given up)."""
    if st.unit is None:
        return None if st.unit_call is not None else [tool("get_unit", serial=st.serial)]
    u = st.unit
    missing = []
    if st.contacts is None:
        missing.append(tool("get_contacts", customer_id=u["customer_id"]))
    if st.parts is None:
        missing.append(tool("check_parts", kit_sku=u["kit_sku"], region=u["region"]))
    if not st.slot_calls:
        missing.append(tool("find_engineer_slots", region=u["region"], skill=u["skill_required"], not_after=u["remedy_by"]))
    if missing:
        return missing
    pending = []
    if st.reservation is None and not st.gave_up_parts():
        if not st.reserve_calls:
            stock = st.parts["stock"]
            preferred = st.parts["preferred_warehouse"]
            candidates = [preferred] + list(st.parts["alternatives"])
            warehouse = next((w for w in candidates if stock.get(w, 0) > 0), None)
        else:
            elsewhere = _stock_from_error(st.reserve_calls[-1].result)
            tried = {c.input.get("warehouse") for c in st.reserve_calls}
            warehouse = next((w for w in st.parts["alternatives"] if elsewhere.get(w, 0) > 0 and w not in tried), None)
        if warehouse is None:
            pending.append(tool("reserve_kit", kit_sku=u["kit_sku"], warehouse=st.parts["preferred_warehouse"], serial=st.serial))
        else:
            pending.append(tool("reserve_kit", kit_sku=u["kit_sku"], warehouse=warehouse, serial=st.serial))
    if st.booking is None and not st.gave_up_slot():
        tried = {c.input.get("slot_id") for c in st.book_calls}
        latest = _ok(st.slot_calls[-1]) or {}
        candidates = [s["slot_id"] for s in latest.get("slots", []) if s["slot_id"] not in tried and s["slot_id"] not in booked_elsewhere]
        if candidates:
            pending.append(tool("book_slot", slot_id=candidates[0], serial=st.serial))
        elif len(st.slot_calls) < 3:
            pending.append(tool("find_engineer_slots", region=u["region"], skill=u["skill_required"], not_after=u["remedy_by"],
                                limit=6))
    return pending or None


def _plan(st: _UnitState) -> dict:
    u, c = st.unit or {}, st.contact() or {}
    status = st.status()
    notes = []
    if st.reservation and st.reservation.get("warehouse") != (st.parts or {}).get("preferred_warehouse"):
        notes.append(f"kit from {st.reservation.get('warehouse')} - preferred warehouse out of stock")
    if c and (st.contacts or {}).get("site", {}).get("contact_id") == c.get("contact_id"):
        notes.append("site contact used: primary contact is out of office")
    if status == "waiting_parts":
        notes.append("no kit in stock at any warehouse; order against the lead time")
    if status == "pending_schedule":
        notes.append("scheduling unavailable or no slot before remedy_by; needs the field-service lead")
    visit = (st.booking or {}).get("start")
    return {"serial": st.serial, "customer_id": u.get("customer_id"), "status": status, "remedy": u.get("remedy"),
            "kit_sku": u.get("kit_sku"), "warehouse": (st.reservation or {}).get("warehouse"),
            "reservation_id": (st.reservation or {}).get("reservation_id"), "engineer_id": (st.booking or {}).get("engineer_id"),
            "slot_id": (st.booking or {}).get("slot_id"), "visit_start": visit, "contact_id": c.get("contact_id"),
            "contact_by": u.get("contact_by"), "remedy_by": u.get("remedy_by"),
            "sla_ok": bool(visit and visit[:10] <= (u.get("remedy_by") or "")), "notes": "; ".join(notes)}


def _narrative(plan: dict, st: _UnitState) -> str:
    u = st.unit or {}
    c = st.contact() or {}
    return (f"Unit {plan['serial']} ({u.get('sku')}, lot {u.get('lot')}) is installed at {u.get('customer')} site {u.get('site_id')} "
            f"in region {u.get('region')} on {u.get('application')} duty, risk class {u.get('risk_class')}. The campaign remedy is "
            f"{u.get('remedy')} with kit {u.get('kit_sku')}; the required skill is {u.get('skill_required')} and the visit takes "
            f"about {u.get('visit_hours')} hours. Deadlines: contact by {u.get('contact_by')}, remedy by {u.get('remedy_by')}. "
            f"Interim measure to communicate: {u.get('interim_measure')} Contact: {c.get('name')} ({c.get('role')}, {c.get('email')}). "
            f"Outcome: {plan['status']}"
            + (f" - kit reserved at {plan['warehouse']} ({plan['reservation_id']}), engineer {plan['engineer_id']} on "
               f"{plan['visit_start']} ({plan['slot_id']})." if plan["status"] == "scheduled" else f". {plan['notes']}"))


def plan_units(req: MockRequest, targets: list[str], *, report: str, mode: str) -> Reply:
    if mode == "loop" and targets:                              # [mock] a stuck model: the same call, forever
        return use_tools(tool("get_unit", serial=targets[0]), preface="Let me check that unit again.")
    if mode == "thorough" and targets:                          # [mock] a model that never stops exploring
        st = _UnitState(req, targets[0])
        if st.unit is None:
            return use_tools(tool("get_unit", serial=targets[0]))
        n = len(req.calls("find_engineer_slots"))
        day = f"2026-09-{21 + n % 9:02d}"
        return use_tools(tool("find_engineer_slots", region=st.unit["region"], skill=st.unit["skill_required"], not_after=day),
                         preface=f"Checking availability up to {day} as well.")
    booked = {(_ok(c) or {}).get("slot_id") for c in req.calls("book_slot") if _ok(c)}
    booked.discard(None)
    states = []
    for serial in targets:
        st = _UnitState(req, serial)
        step = _next_step(req, st, booked)
        if step:
            return use_tools(*step)
        states.append(st)
    plans = [_plan(st) for st in states]
    if report == "verbose":
        body = "\n\n".join(_narrative(p, st) + "\n" + json.dumps(p) for p, st in zip(plans, states))
        return say("Recall remedy plan - full report\n\n" + body, complexity=0.6)
    return say("\n".join(json.dumps(p) for p in plans), complexity=0.4)


@scenario("adv.day4.worker", match=lambda r: "<adv_day4_worker" in r.system_text, priority=10)
def worker(req: MockRequest) -> Reply:
    report = _attr(req.system_text, "adv_day4_worker", "report") or "compact"
    mode = _attr(req.system_text, "adv_day4_worker", "mode") or "normal"
    return plan_units(req, _serials_in(req.first_user_text), report=report, mode=mode)


@scenario("adv.day4.single_agent", match=lambda r: "<adv_day4_single_agent" in r.system_text, priority=10)
def single_agent(req: MockRequest) -> Reply:
    listing = _ok(req.calls("list_affected_units")[-1]) if req.called("list_affected_units") else None
    if listing is None:
        return use_tools(tool("list_affected_units"), preface="Listing the affected units first.")
    targets = [u["serial"] for u in listing.get("units", [])]
    return plan_units(req, targets, report="compact", mode="normal")


# ============================================================================ the coordinator
def _batches(units_: list[dict], batch: str, brief: str, notice: str) -> list[dict]:
    groups: list[list[dict]] = []
    if batch == "unit":
        groups = [[u] for u in units_]
    else:
        by_customer: dict[str, list[dict]] = {}
        for u in units_:
            by_customer.setdefault(u["customer_id"], []).append(u)
        groups = list(by_customer.values())
    out = []
    for g in groups:
        serials_ = [u["serial"] for u in g]
        risk = sorted({u["risk_class"] for u in g})
        if brief == "long":
            text = (f"Customer {g[0]['customer']} ({g[0]['customer_id']}), region {g[0]['region']}, units {', '.join(serials_)}, "
                    f"risk class {'/'.join(risk)}. Context you must respect:\n{notice}\nMethod: for each unit call get_unit, "
                    "then get_contacts, check_parts and find_engineer_slots, then reserve_kit and book_slot. Prefer the "
                    "region's warehouse; fall back to another warehouse with stock. Use the site contact when the primary "
                    "contact is out of office. Report status, reservation, booking, contact and SLA per unit, with a short "
                    "narrative of what you found and why you chose the slot and warehouse.")
        else:
            text = f"{g[0]['customer']}: units {', '.join(serials_)} ({'/'.join(risk)} risk)."
        out.append({"serials": serials_, "brief": text})
    return out


def _summary(results: dict, stats: dict | None) -> str:
    plans = results.get("plans") or []
    by_status: dict[str, int] = {}
    kits: dict[str, int] = {}
    late, pending = [], []
    for p in plans:
        by_status[p.get("status", "?")] = by_status.get(p.get("status", "?"), 0) + 1
        if p.get("warehouse"):
            kits[p["warehouse"]] = kits.get(p["warehouse"], 0) + 1
        if p.get("status") == "scheduled" and not p.get("sla_ok"):
            late.append(p["serial"])
        if p.get("status") != "scheduled":
            pending.append(f"{p['serial']} ({p.get('status')})")
    lines = [f"Campaign RC-2026-03 status: {len(plans)} unit plans - " + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) + "."]
    lines.append("Kits reserved by warehouse: " + (", ".join(f"{w} {n}" for w, n in sorted(kits.items())) or "none") + ".")
    lines.append("SLA risks: " + (", ".join(late) if late else "none - every scheduled visit is on or before its remedy_by date") + ".")
    lines.append("Needs a human decision: " + (", ".join(pending) if pending else "nothing") + ".")
    queued = (results.get("queued") or 0) + (results.get("claimed") or 0)
    if queued or results.get("failed"):
        lines.append(f"Not finished: {queued} task(s) still queued, {results.get('failed') or 0} failed - see the queue.")
    if stats and stats.get("stopped"):
        lines.append(f"PAUSED: dispatch stopped early ({stats['stopped']}); reporting rather than continuing, per the campaign's stop conditions.")
    return "\n".join(lines)


@scenario("adv.day4.coordinator", match=lambda r: "<adv_day4_coordinator" in r.system_text, priority=10)
def coordinator(req: MockRequest) -> Reply:
    batch = _attr(req.system_text, "adv_day4_coordinator", "batch") or "customer"
    brief = _attr(req.system_text, "adv_day4_coordinator", "brief") or "short"
    listing = _ok(req.calls("list_affected_units")[-1]) if req.called("list_affected_units") else None
    if listing is None:
        return use_tools(tool("list_affected_units"), preface="Listing the affected units.")
    if not req.called("dispatch_units"):
        notice = _between(req.system_text, "campaign_notice") or ""
        return use_tools(tool("dispatch_units", batches=_batches(listing.get("units", []), batch, brief, notice)),
                         preface=f"Dispatching {listing.get('count')} units to the workers.")
    stats = _ok(req.calls("dispatch_units")[-1]) or {}
    if not req.called("collect_results"):
        return use_tools(tool("collect_results"), preface="Collecting the workers' plans.")
    results = _ok(req.calls("collect_results")[-1]) or {}
    return say(_summary(results, stats), complexity=0.5)


# ============================================================================ agent-to-agent roles (lab 04)
SAFETY_RE = re.compile(r"injur|sprayed|incident|fluid release|hospital", re.I)


@scenario("adv.day4.a2a", match=lambda r: "<adv_day4_a2a" in r.system_text, priority=10)
def a2a(req: MockRequest) -> Reply:
    role = _attr(req.system_text, "adv_day4_a2a", "role") or "coordinator"
    env = _json_between(req.last_user_text or req.first_user_text, "envelope") or {}
    payload = env.get("payload") or {}
    kind = env.get("type")
    if role == "coordinator":
        body = payload.get("body", "")
        if SAFETY_RE.search(body):
            return json_reply({"classification": "safety_event", "stop_condition": True,
                               "reason": "the customer reports an operator sprayed with process fluid and a filed incident",
                               "recommended_pattern": "handoff", "to": "quality-lead",
                               "also": {"broadcast": "lot_pause", "lot": payload.get("lot")}})
        return json_reply({"classification": "routine", "stop_condition": False, "recommended_pattern": "none"})
    if role == "quality_lead":
        if kind == "delegation":
            return json_reply({"verdict": "stop_condition_met", "lot": payload.get("lot"),
                               "actions": ["pause scheduling for the lot", "page the quality lead", "open incident file",
                                           "reply to the customer with interim measures only"],
                               "customer_reply_allowed": False, "owner_after": "coordinator"})
        if kind == "handoff":
            return json_reply({"accepted": True, "owner": "quality-lead", "conversation_id": env.get("conversation_id"),
                               "first_action": "phone the site engineer within the hour; confirm the unit is isolated",
                               "customer_reply": "We have received your incident report and stopped the campaign for this "
                                                 "lot. Our quality lead will call you within the hour. Please keep the unit "
                                                 "isolated and the seal chamber below 70 C."})
        if kind == "message":
            return json_reply({"reply": "Thank you - our quality lead has your incident report; the engineer visit is "
                                        "on hold until the investigation clears the lot."})
    if role == "unit_worker":
        mine = _serials_in(req.system_text)
        if kind == "broadcast":
            affected = [s for s in mine if s in (payload.get("serials") or []) or payload.get("lot") in (payload.get("lots_by_serial") or {}).get(s, "")]
            return json_reply({"ack": True, "worker": _attr(req.system_text, "adv_day4_a2a", "name"),
                               "paused_serials": affected or mine if payload.get("scope") == "lot" else affected,
                               "action": "no new bookings for these serials; existing bookings kept pending review"})
        return json_reply({"ack": True, "note": "no action for this message type"})
    return json_reply({"ack": True})


# ============================================================================ hosted agents (labs 05, 06)
def _assessment(u: dict, contact_of: dict[str, dict] | None) -> dict:
    contact = None
    if contact_of and u.get("customer_id") in contact_of:
        c = contact_of[u["customer_id"]]
        contact = c["site"] if "out of office" in (c.get("notes") or "").lower() else c["primary"]
    return {"serial": u["serial"], "customer_id": u["customer_id"], "risk_class": u["risk_class"], "remedy": u["remedy"],
            "kit_sku": u["kit_sku"], "skill": u["skill_required"], "region": u["region"], "contact_by": u["contact_by"],
            "remedy_by": u["remedy_by"], "contact_id": (contact or {}).get("contact_id"),
            "interim_measure": u.get("interim_measure")}


@scenario("adv.day4.hosted_worker", match=lambda r: "<adv_day4_hosted_worker" in r.system_text, priority=10)
def hosted_worker(req: MockRequest) -> Reply:
    serial = (_serials_in(req.first_user_text) or ["?"])[0]
    unit_call = req.calls("get_unit")[-1] if req.called("get_unit") else None
    if unit_call is None:
        return use_tools(tool("get_unit", serial=serial), preface=f"Looking up unit {serial}.")
    u = _ok(unit_call)
    if u is None:
        return say(f"I could not read unit {serial}: {unit_call.result}")
    assessment = _assessment(u, None)
    if not req.called("write"):
        return use_tools(tool("write", path=f"recall/{serial}.json", content=json.dumps(assessment, indent=1)),
                         preface="Writing the assessment to the workspace.")
    return say(f"Assessment for {serial} written to recall/{serial}.json: risk class {u['risk_class']}, remedy {u['remedy']} "
               f"with kit {u['kit_sku']} (skill {u['skill_required']}); contact by {u['contact_by']}, remedy by {u['remedy_by']}. "
               f"Interim measure: {u['interim_measure']}", complexity=0.3)


@scenario("adv.day4.hosted_investigator", match=lambda r: "<adv_day4_hosted_investigator" in r.system_text, priority=10)
def hosted_investigator(req: MockRequest) -> Reply:
    targets = _serials_in(req.first_user_text)
    reads = {c.input.get("path"): c for c in req.calls("read")}
    needed = [p for p in ("campaign/units.json", "campaign/resources.json") if p not in reads]
    if needed:
        return use_tools(*[tool("read", path=p) for p in needed], preface="Reading the campaign files from the shared workspace.")
    try:
        units_ = json.loads(reads["campaign/units.json"].result or "[]")
        resources = json.loads(reads["campaign/resources.json"].result or "{}")
    except ValueError:
        return say("The campaign files are not valid JSON; I cannot assess the units.")
    contact_of = {c["customer_id"]: c for c in resources.get("contacts", [])}
    rows = [_assessment(u, contact_of) for u in units_ if u["serial"] in targets]
    return say("\n".join(json.dumps(r) for r in rows), complexity=0.3)


@scenario("adv.day4.hosted_planner", match=lambda r: "<adv_day4_hosted_planner" in r.system_text, priority=10)
def hosted_planner(req: MockRequest) -> Reply:
    text = req.first_user_text
    if not req.called("read"):
        return use_tools(tool("read", path="campaign/resources.json"), preface="Reading stock and calendars.")
    try:
        resources = json.loads(req.calls("read")[-1].result or "{}")
    except ValueError:
        return say("resources.json is not valid JSON.")
    assessments = [json.loads(line) for line in (_between(text, "assessments") or "").splitlines() if line.strip().startswith("{")]
    stock = {k["sku"]: dict(k["stock"]) for k in resources.get("kits", [])}
    free = sorted(resources.get("free_slots", []), key=lambda s: (s["start"], s["slot_id"]))
    region_wh = {"US-EAST": "WH-EAST", "US-WEST": "WH-WEST", "EU": "WH-EU", "APAC": "WH-EU"}
    out = []
    for a in assessments:
        wh_pref = region_wh.get(a["region"], "WH-EAST")
        candidates = [wh_pref] + [w for w in sorted(stock.get(a["kit_sku"], {}), key=lambda w: -stock[a["kit_sku"]][w]) if w != wh_pref]
        warehouse = next((w for w in candidates if stock.get(a["kit_sku"], {}).get(w, 0) > 0), None)
        if warehouse:
            stock[a["kit_sku"]][warehouse] -= 1
        slot = next((s for s in free if s["region"] == a["region"] and a["skill"] in s["skills"] and s["start"][:10] <= a["remedy_by"]), None)
        if slot:
            free.remove(slot)
        status = "scheduled" if warehouse and slot else ("waiting_parts" if not warehouse else "pending_schedule")
        out.append({"serial": a["serial"], "status": status, "kit_sku": a["kit_sku"], "warehouse": warehouse,
                    "engineer_id": (slot or {}).get("engineer_id"), "slot_id": (slot or {}).get("slot_id"),
                    "visit_start": (slot or {}).get("start"), "remedy_by": a["remedy_by"], "contact_id": a.get("contact_id"),
                    "sla_ok": bool(slot)})
    return say("\n".join(json.dumps(o) for o in out), complexity=0.4)


@scenario("adv.day4.hosted_lead", match=lambda r: "<adv_day4_hosted_lead" in r.system_text, priority=10)
def hosted_lead(req: MockRequest) -> Reply:
    text = req.first_user_text
    if text.startswith("QA:"):                                  # a copy of the coordinator (roster entry `self`)
        plans = [json.loads(line) for line in (_between(text, "plans") or "").splitlines() if line.strip().startswith("{")]
        missing = [p["serial"] for p in plans if p.get("status") != "scheduled"]
        return say(f"QA: {len(plans)} unit plans checked; {len(plans) - len(missing)} scheduled with a kit and a slot; "
                   f"needs attention: {', '.join(missing) if missing else 'none'}. No plan mentions compensation.", complexity=0.3)
    units_ = _json_between(text, "units") or []
    resources = _json_between(text, "resources") or {}
    if not req.called("list_agents"):
        return use_tools(tool("list_agents"), preface="Checking the roster.")
    if not req.called("write"):
        return use_tools(tool("write", path="campaign/units.json", content=json.dumps(units_, indent=1)),
                         tool("write", path="campaign/resources.json", content=json.dumps(resources, indent=1)),
                         preface="Sharing the campaign data through the workspace.")
    sends = req.calls("send_to_agent")
    to_investigator = [c for c in sends if c.input.get("agent") == "unit-investigator"]
    if not to_investigator:
        by_customer: dict[str, list[str]] = {}
        for u in units_:
            by_customer.setdefault(u["customer_id"], []).append(u["serial"])
        groups = list(by_customer.values())
        batches = [sum(groups[i::3], []) for i in range(3)]
        return use_tools(*[tool("send_to_agent", agent="unit-investigator",
                                message=f"Assess units {', '.join(b)} from campaign/units.json and campaign/resources.json in "
                                        "the shared workspace. Reply with one JSON object per unit (serial, customer_id, "
                                        "risk_class, remedy, kit_sku, skill, region, contact_by, remedy_by, contact_id).")
                          for b in batches if b], preface="Three investigators, one batch of customers each.")
    assessments = []
    for c in to_investigator:
        reply = (_ok(c) or {}).get("reply", "")
        assessments += [line for line in reply.splitlines() if line.strip().startswith("{")]
    to_planner = [c for c in sends if c.input.get("agent") == "schedule-planner"]
    if not to_planner:
        return use_tools(tool("send_to_agent", agent="schedule-planner",
                              message="Assign a kit warehouse and an engineer slot to each assessed unit using campaign/resources.json "
                                      "(prefer the region's warehouse; earliest free slot on or before remedy_by; one slot per unit). "
                                      "Reply with one JSON object per unit.\n<assessments>\n" + "\n".join(assessments) + "\n</assessments>"),
                         preface="Handing the assessments to the planner.")
    plans_text = (_ok(to_planner[-1]) or {}).get("reply", "")
    self_name = _attr(req.system_text, "adv_day4_hosted_lead", "name") or "recall-lead"
    to_self = [c for c in sends if c.input.get("agent") == self_name]
    if not to_self:
        return use_tools(tool("send_to_agent", agent=self_name, message="QA: verify these plans.\n<plans>\n" + plans_text + "\n</plans>"),
                         preface="Asking a copy of myself for an independent check.")
    if not req.called("record_plan"):
        plans = [json.loads(line) for line in plans_text.splitlines() if line.strip().startswith("{")]
        return use_tools(tool("record_plan", plans=plans), preface="Recording the plan in Kestrel's system of record.")
    recorded = _ok(req.calls("record_plan")[-1]) or {}
    qa = (_ok(to_self[-1]) or {}).get("reply", "")
    return say(f"Campaign plan recorded: {recorded.get('recorded', 0)} units ({recorded.get('scheduled', 0)} scheduled). {qa}",
               complexity=0.4)
