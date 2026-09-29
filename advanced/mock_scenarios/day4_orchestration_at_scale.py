"""Mock policies for Day 4 - multi-agent orchestration at scale (advanced/day4_orchestration_at_scale/labs).

Every policy matches on a marker in the system prompt (`<adv_day4_...>`), so it never captures another day's
requests, and derives everything it "says" from the request: tool results, the task message, the envelope it
was sent, the files it read from a session workspace.  The policies never read the dataset.  They do not
think: they apply the campaign's rules to what the request contains.  Scripted misbehaviour is opt-in through
marker attributes and is labelled [mock] by the labs that use it.

Section index
    unit planning ....... the worker (one batch of units) and the single agent (all units, one context)
    coordinator ......... list -> dispatch -> collect -> summary (labs 01, 03, 07)
    agent-to-agent ...... coordinator triage, quality lead, unit workers reading typed envelopes (lab 04)
    hosted agents ....... Managed Agents assessor (lab 05); lead, investigator, planner (labs 06, 07)

One behaviour worth knowing before you read lab 07's numbers: the unit planner answers a unit's scheduling
question from the most recent slot search it can see for the same region and skill.  Inside one worker's
context (one customer: one risk class, one deadline) that is harmless.  In the single agent's context, where
eleven units with three different deadlines share one conversation, it reuses a search made for another unit
- the cross-item contamination a long, repetitive context invites.  It is a scripted stand-in for a failure
mode, not a measurement of a model: live, measure it on your own traces.
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
    for s in re.findall(r"\b(?:KP\d{3}|KC\d)-\d{4}-\d{4}\b", text or ""):
        if s not in seen:
            seen.append(s)
    return seen


def _ok(call) -> Any:
    """The parsed result of a successful tool call, else None."""
    if call is None or call.is_error:
        return None
    return call.result_json()


def _json_objects(text: str) -> list[dict]:
    """Every top-level JSON object in a text (reports mix prose and JSON)."""
    found: list[dict] = []
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text or ""):
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = end
    return found


def _json_lines(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _latest_task(req: MockRequest) -> tuple[str, int]:
    """(text, message index) of the most recent user message that carries text - a thread's current task."""
    for i in range(len(req.messages) - 1, -1, -1):
        m = req.messages[i]
        if m.get("role") != "user":
            continue
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return text, i
    return "", -1


def _calls_since(req: MockRequest, index: int, name: str) -> list:
    """Tool calls named `name` made after message `index` (the calls that belong to the current task)."""
    ids: set[str] = set()
    for m in req.messages[index + 1:]:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            ids.update(b.get("id") for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use")
    return [c for c in req.calls(name) if c.id in ids]


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
        # The shortcut described in the module docstring: any search for the same region and skill counts.
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
            candidates = [st.parts["preferred_warehouse"]] + list(st.parts["alternatives"])
            warehouse = next((w for w in candidates if stock.get(w, 0) > 0), None)
        else:
            elsewhere = _stock_from_error(st.reserve_calls[-1].result)
            tried = {c.input.get("warehouse") for c in st.reserve_calls}
            warehouse = next((w for w in st.parts["alternatives"] if elsewhere.get(w, 0) > 0 and w not in tried), None)
        pending.append(tool("reserve_kit", kit_sku=u["kit_sku"], warehouse=warehouse or st.parts["preferred_warehouse"],
                            serial=st.serial))
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
            f"I checked stock at every warehouse and the engineers' calendars before choosing. Outcome: {plan['status']}"
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
    targets = [u["serial"] for u in listing.get("units", [])]           # listing order: the order it was given
    return plan_units(req, targets, report="compact", mode="normal")


# ============================================================================ the coordinator
def _batches(units_: list[dict], batch: str, brief: str, notice: str) -> list[dict]:
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


def _summary(plans: list[dict], stats: dict | None, queue: dict, dead: list[dict]) -> str:
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
    unfinished = (queue.get("queued") or 0) + (queue.get("claimed") or 0)
    if unfinished:
        lines.append(f"Not finished: {unfinished} task(s) still queued.")
    if dead:
        lines.append("Dead letters for a human: " + "; ".join(f"{d.get('task_id')} ({d.get('error')})" for d in dead) + ".")
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
        return use_tools(tool("collect_results"), preface="Collecting the workers' results.")
    results = _ok(req.calls("collect_results")[-1]) or {}
    plans = results.get("plans")
    if plans is None:                                       # full reports: read them and pull out the plans
        plans = [o for r in results.get("reports", []) for o in _json_objects(r) if "serial" in o]
    return say(_summary(plans, stats, results.get("queue") or {}, results.get("dead_letters") or []), complexity=0.5)


# ============================================================================ agent-to-agent roles (lab 04)
SAFETY_RE = re.compile(r"injur|sprayed|incident|fluid release|hospital", re.I)
INTERIM = ("Please keep the unit isolated and the seal-chamber temperature alarm at 70 C; do not run it unattended.")


@scenario("adv.day4.a2a", match=lambda r: "<adv_day4_a2a" in r.system_text, priority=10)
def a2a(req: MockRequest) -> Reply:
    role = _attr(req.system_text, "adv_day4_a2a", "role") or "coordinator"
    name = _attr(req.system_text, "adv_day4_a2a", "name") or role
    env = _json_between(req.last_user_text or req.first_user_text, "envelope") or {}
    payload = env.get("payload") or {}
    kind = env.get("type")
    flags = set(payload.get("thread_flags") or [])
    if role == "coordinator":
        if kind == "inbound":
            body = payload.get("body", "")
            if "safety_event_open" in flags:
                return json_reply({"classification": "safety_followup", "stop_condition": False,
                                   "needs": "quality lead's answer on whether and when the visit can happen"})
            if SAFETY_RE.search(body):
                return json_reply({"classification": "safety_event", "stop_condition": True, "lot": payload.get("lot"),
                                   "reason": "the customer reports an operator sprayed with process fluid and a filed incident",
                                   "actions": ["involve the quality lead", "pause the lot for every unit worker"]})
            return json_reply({"classification": "routine", "stop_condition": False})
        if kind == "result":                                # turn the quality lead's answer into the customer reply
            return json_reply({"body": payload.get("customer_reply") or "We will come back to you shortly.",
                               "template": "TPL-RC-01", "hazard_wording_changed": False})
        return json_reply({"ack": True})
    if role == "quality_lead":
        if kind == "delegation" and payload.get("kind") == "followup_question":
            return json_reply({"verdict": "visit_on_hold", "customer_reply": "Thank you for the update. The engineer visit "
                               "stays on hold until our investigation clears the lot; our quality lead will call you with a "
                               "date. " + INTERIM})
        if kind == "delegation":
            lot = payload.get("lot") or (payload.get("context") or {}).get("lot")
            return json_reply({"verdict": "stop_condition_met", "lot": lot,
                               "actions": ["pause scheduling for the lot", "open an incident file", "phone the site engineer"],
                               "customer_reply": "We have received your incident report and stopped the campaign for this "
                                                 "lot while we investigate. " + INTERIM})
        if kind == "handoff":
            state = payload.get("state") or {}
            return json_reply({"accepted": True, "owner": name, "conversation_id": env.get("conversation_id"),
                               "first_action": f"phone the site engineer within the hour about {state.get('unit')}",
                               "customer_reply": "We have received your incident report and stopped the campaign for this "
                                                 "lot. I own your case from now on and will call you within the hour. " + INTERIM})
        if kind == "inbound":
            return json_reply({"customer_reply": "Good to hear. The visit stays on hold until the investigation clears the "
                                                 "lot; I will call you with a date this week. " + INTERIM})
        return json_reply({"ack": True})
    if role == "unit_worker":
        mine = _serials_in(req.system_text)
        if kind == "broadcast":
            paused = [s for s in mine if s in (payload.get("serials") or [])]
            return json_reply({"ack": True, "worker": name, "paused_serials": paused,
                               "action": "no new bookings for these serials; existing bookings held pending review"})
        return json_reply({"ack": True})
    return json_reply({"ack": True})


# ============================================================================ hosted agents (labs 05, 06, 07)
@scenario("adv.day4.hosted_assessor", match=lambda r: "<adv_day4_hosted_assessor" in r.system_text, priority=10)
def hosted_assessor(req: MockRequest) -> Reply:
    """Lab 05: one unit at a time - look it up through the client's custom tool, write the assessment into the
    session's workspace with the built-in `write` tool, then summarize."""
    task, index = _latest_task(req)
    targets = _serials_in(task)
    done: list[dict] = []
    for serial in targets:
        call = next((c for c in reversed(_calls_since(req, index, "get_unit")) if c.input.get("serial") == serial), None)
        if call is None:
            return use_tools(tool("get_unit", serial=serial), preface=f"Looking up {serial}.")
        u = _ok(call)
        if u is None:
            done.append({"serial": serial, "error": call.result})
            continue
        path = f"recall/{serial}.json"
        if not any(c.input.get("path") == path for c in _calls_since(req, index, "write")):
            assessment = {k: u.get(k) for k in ("serial", "customer_id", "risk_class", "remedy", "kit_sku", "skill_required",
                                                "contact_by", "remedy_by", "interim_measure")}
            return use_tools(tool("write", path=path, content=json.dumps(assessment, indent=1)),
                             preface=f"Writing the assessment for {serial}.")
        done.append(u)
    lines = []
    for u in done:
        if "error" in u:
            lines.append(f"- {u['serial']}: could not be read ({u['error']})")
        else:
            lines.append(f"- {u['serial']} ({u['customer_id']}, {u['risk_class']}): {u['remedy']} with kit {u['kit_sku']}, "
                         f"skill {u['skill_required']}; contact by {u['contact_by']}, remedy by {u['remedy_by']}. "
                         f"Written to recall/{u['serial']}.json.")
    return say(f"Assessed {len(done)} unit(s):\n" + "\n".join(lines), complexity=0.3)


def _contact_for(customer_id: str, contacts: list[dict]) -> dict:
    c = next((x for x in contacts if x.get("customer_id") == customer_id), {})
    if "out of office" in (c.get("notes") or "").lower():
        return c.get("site") or {}
    return c.get("primary") or {}


UNITS_FILE = "/workspace/campaign/units.json"
RESOURCES_FILE = "/workspace/campaign/resources.json"


@scenario("adv.day4.hosted_investigator", match=lambda r: "<adv_day4_hosted_investigator" in r.system_text, priority=10)
def hosted_investigator(req: MockRequest) -> Reply:
    """A worker thread: reads the campaign file mounted in the session's workspace and assesses its batch."""
    task, index = _latest_task(req)
    targets = _serials_in(task)
    read = next((c for c in reversed(_calls_since(req, index, "read")) if c.input.get("path") == UNITS_FILE), None)
    if read is None:
        return use_tools(tool("read", path=UNITS_FILE), preface="Reading the campaign file from the workspace.")
    try:
        data = json.loads(read.result or "")
    except ValueError:
        return say(f"{UNITS_FILE} is not valid JSON; I cannot assess the units.")
    units_ = {u["serial"]: u for u in data.get("units", [])}
    rows = []
    for s in targets:
        u = units_.get(s)
        if u is None:
            rows.append({"serial": s, "error": f"not in {UNITS_FILE}"})
            continue
        contact = _contact_for(u["customer_id"], data.get("contacts", []))
        rows.append({"serial": s, "customer_id": u["customer_id"], "risk_class": u["risk_class"], "region": u["region"],
                     "remedy": u["remedy"], "kit_sku": u["kit_sku"], "skill": u["skill_required"],
                     "contact_id": contact.get("contact_id"), "contact_by": u["contact_by"], "remedy_by": u["remedy_by"]})
    return say("\n".join(json.dumps(r) for r in rows), complexity=0.3)


@scenario("adv.day4.hosted_planner", match=lambda r: "<adv_day4_hosted_planner" in r.system_text, priority=10)
def hosted_planner(req: MockRequest) -> Reply:
    """A worker thread: allocates a kit and a slot per assessed unit from a resources snapshot - the one given in the
    task if there is one, else the file mounted in the workspace. Earliest deadline first; it never reuses a slot or
    a kit within its own batch - and knows nothing about other planner threads working from the same snapshot."""
    task, index = _latest_task(req)
    res = _json_between(task, "resources")
    if res is None:
        read = next((c for c in reversed(_calls_since(req, index, "read")) if c.input.get("path") == RESOURCES_FILE), None)
        if read is None:
            return use_tools(tool("read", path=RESOURCES_FILE), preface="Reading stock and calendars from the workspace.")
        try:
            res = json.loads(read.result or "")
        except ValueError:
            return say(f"{RESOURCES_FILE} is not valid JSON.")
    assessments = _json_lines(_between(task, "assessments") or "")
    stock = {k["sku"]: dict(k["stock"]) for k in res.get("kits", [])}
    wh_for = res.get("warehouse_for_region", {})
    free = sorted(res.get("free_slots", []), key=lambda s: (s["start"], s["slot_id"]))
    out = []
    for a in sorted(assessments, key=lambda a: (a.get("remedy_by", ""), a.get("serial", ""))):
        kit = stock.get(a.get("kit_sku"), {})
        pref = wh_for.get(a.get("region"))
        candidates = ([pref] if pref else []) + sorted((w for w in kit if w != pref), key=lambda w: (-kit[w], w))
        warehouse = next((w for w in candidates if kit.get(w, 0) > 0), None)
        if warehouse:
            kit[warehouse] -= 1
        slot = next((s for s in free if s["region"] == a.get("region") and a.get("skill") in s["skills"]
                     and s["start"][:10] <= a.get("remedy_by", "")), None)
        if slot:
            free.remove(slot)
        status = "scheduled" if warehouse and slot else ("waiting_parts" if not warehouse else "pending_schedule")
        out.append({"serial": a.get("serial"), "customer_id": a.get("customer_id"), "status": status,
                    "remedy": a.get("remedy"), "kit_sku": a.get("kit_sku"), "warehouse": warehouse,
                    "engineer_id": (slot or {}).get("engineer_id"), "slot_id": (slot or {}).get("slot_id"),
                    "visit_start": (slot or {}).get("start"), "contact_id": a.get("contact_id"),
                    "contact_by": a.get("contact_by"), "remedy_by": a.get("remedy_by"), "sla_ok": bool(slot)})
    return say("\n".join(json.dumps(o) for o in out), complexity=0.4)


def _qa_reply(task: str) -> Reply:
    plans = _json_lines(_between(task, "plans") or "")
    unscheduled = [p["serial"] for p in plans if p.get("status") != "scheduled"]
    late = [p["serial"] for p in plans if p.get("status") == "scheduled" and (p.get("visit_start") or "")[:10] > (p.get("remedy_by") or "")]
    no_contact = [p["serial"] for p in plans if not p.get("contact_id")]
    verdict = "PASS" if not (unscheduled or late or no_contact) else "ATTENTION"
    return say(f"QA {verdict}: {len(plans)} unit plans checked; {len(plans) - len(unscheduled)} scheduled with a kit and a slot; "
               f"late visits: {', '.join(late) or 'none'}; missing contact: {', '.join(no_contact) or 'none'}; "
               f"not scheduled: {', '.join(unscheduled) or 'none'}.", complexity=0.3)


def _fresh_subset(resources: dict, assessments: list[dict]) -> dict:
    """The part of a fresh snapshot the re-plan needs: the kits and the free slots for these units only."""
    regions = {a.get("region") for a in assessments}
    skills = {a.get("skill") for a in assessments}
    latest = max((a.get("remedy_by") or "" for a in assessments), default="")
    kits = {a.get("kit_sku") for a in assessments}
    return {"warehouse_for_region": {r: w for r, w in (resources.get("warehouse_for_region") or {}).items() if r in regions},
            "kits": [k for k in resources.get("kits", []) if k.get("sku") in kits],
            "free_slots": [s for s in resources.get("free_slots", []) if s["region"] in regions
                           and skills & set(s["skills"]) and s["start"][:10] <= latest]}


@scenario("adv.day4.hosted_lead", match=lambda r: "<adv_day4_hosted_lead" in r.system_text, priority=10)
def hosted_lead(req: MockRequest) -> Reply:
    """The coordinator of the hosted swarm. The bulk data is mounted in the workspace; the lead only lists the units
    (a custom tool), delegates, commits through a custom tool the client validates, and re-plans what is rejected."""
    task, _ = _latest_task(req)
    if task.startswith("QA:"):                                   # a copy of the coordinator (roster entry `self`)
        return _qa_reply(task)
    name = _attr(req.system_text, "adv_day4_hosted_lead", "name") or "recall-lead"
    planners = max(1, int(_attr(req.system_text, "adv_day4_hosted_lead", "planners") or 1))
    if not req.called("list_agents"):
        return use_tools(tool("list_agents"), preface="Checking the roster.")
    if not req.called("list_affected_units"):
        return use_tools(tool("list_affected_units"), preface="Listing the affected units from Kestrel's system of record.")
    listing = _ok(req.calls("list_affected_units")[-1]) or {}
    sends = req.calls("send_to_agent")
    investigations = [c for c in sends if c.input.get("agent") == "unit-investigator"]
    if not investigations:
        by_customer: dict[str, list[str]] = {}
        for u in listing.get("units", []):
            by_customer.setdefault(u["customer_id"], []).append(u["serial"])
        groups = list(by_customer.values())
        batches = [sum(groups[i::3], []) for i in range(3)]
        return use_tools(*[tool("send_to_agent", agent="unit-investigator",
                                message=f"Assess units {', '.join(b)} from {UNITS_FILE}. Reply with one JSON line per unit.")
                           for b in batches if b], preface="Three investigators, one batch of customers each.")
    assessments = [line for c in investigations for line in ((_ok(c) or {}).get("reply", "")).splitlines()
                   if line.strip().startswith("{")]
    plannings = [c for c in sends if c.input.get("agent") == "schedule-planner"]
    if not plannings:
        chunks = [assessments[i::planners] for i in range(planners)]
        return use_tools(*[tool("send_to_agent", agent="schedule-planner",
                                message=f"Plan these units from {RESOURCES_FILE}. One JSON line per unit."
                                        "\n<assessments>\n" + "\n".join(chunk) + "\n</assessments>")
                           for chunk in chunks if chunk],
                         preface="Planning in parallel." if planners > 1 else "Handing the assessments to the planner.")
    records = req.calls("record_plan")
    first_round = plannings[:planners]
    plans = [p for c in first_round for p in _json_lines((_ok(c) or {}).get("reply", ""))]
    if not records:
        return use_tools(tool("record_plan", plans=plans), preface="Committing the plans to Kestrel's system of record.")
    rejected = (_ok(records[0]) or {}).get("rejected") or []
    if rejected:
        rejected_serials = {r["serial"] for r in rejected}
        redo = [json.loads(a) for a in assessments if json.loads(a).get("serial") in rejected_serials]
        if not req.called("get_resources"):
            return use_tools(tool("get_resources"), preface=f"{len(rejected)} plan(s) were rejected at commit: fetching a "
                                                            "fresh snapshot of stock and calendars.")
        if len(plannings) == len(first_round):
            fresh = _fresh_subset(_ok(req.calls("get_resources")[-1]) or {}, redo)
            thread_id = (_ok(first_round[0]) or {}).get("thread_id")
            return use_tools(tool("send_to_agent", agent="schedule-planner", thread_id=thread_id,
                                  message="These units were rejected at commit (another planner took the same slots or "
                                          "kits). Re-plan them from the fresh snapshot below, not from the file.\n"
                                          "<assessments>\n" + "\n".join(json.dumps(a) for a in redo) + "\n</assessments>\n"
                                          "<resources>\n" + json.dumps(fresh) + "\n</resources>"),
                             preface="Asking the first planner to re-plan the rejected units in its existing thread.")
        if len(records) < 2:
            return use_tools(tool("record_plan", plans=_json_lines((_ok(plannings[-1]) or {}).get("reply", ""))),
                             preface="Committing the re-planned units.")
    final = {p["serial"]: p for p in plans}
    if rejected and len(plannings) > len(first_round):
        for p in _json_lines((_ok(plannings[-1]) or {}).get("reply", "")):
            final[p["serial"]] = p
    scheduled = sum((_ok(c) or {}).get("scheduled", 0) for c in records)
    qa_calls = [c for c in sends if c.input.get("agent") == name]
    if not qa_calls:
        return use_tools(tool("send_to_agent", agent=name, message="QA: verify these unit plans against the campaign rules."
                              "\n<plans>\n" + "\n".join(json.dumps(p) for p in final.values()) + "\n</plans>"),
                         preface="Asking a copy of myself for an independent check.")
    qa = (_ok(qa_calls[-1]) or {}).get("reply", "")
    kits: dict[str, int] = {}
    for p in final.values():
        if p.get("warehouse"):
            kits[p["warehouse"]] = kits.get(p["warehouse"], 0) + 1
    return say(f"Campaign RC-2026-03: {len(final)} unit plans, {scheduled} committed as scheduled"
               + (f" ({len(rejected)} rejected at the first commit and re-planned from a fresh snapshot)" if rejected else "")
               + ". Kits by warehouse: " + ", ".join(f"{w} {n}" for w, n in sorted(kits.items())) + f". {qa}",
               complexity=0.4)
