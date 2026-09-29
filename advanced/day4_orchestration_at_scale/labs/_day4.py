"""Shared helpers for the Day 4 labs: the recall dataset, the campaign's system of record, worker and coordinator
tool definitions, a SQLite work queue with leases and dead letters, a versioned shared record, per-role metering,
the in-code checker that scores a unit plan against the campaign rules, and the small Managed Agents helpers the
hosted labs share.

Nothing here decides how to orchestrate: the labs do.  This module only provides the pieces every architecture
shares, so that the comparisons in labs 06 and 07 are like-for-like.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import sqlite3
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from advanced.lib.durable import Crash, DurableRunner, Outcome, RunStore, ToolContext
from labkit import MODEL, REPO_ROOT, cost_usd

RECALL_DIR = REPO_ROOT / "advanced" / "data" / "recall"
LABS_DIR = Path(__file__).resolve().parent
ISSUED = dt.date(2026, 9, 16)                      # the campaign is issued on the 16th; "today" in the data is the 15th
SKILL_FOR_REMEDY = {"seal_kit_replacement": "seal_replacement", "controller_board_replacement": "controller_firmware"}
WAREHOUSE_FOR_REGION = {"US-EAST": "WH-EAST", "US-WEST": "WH-WEST", "EU": "WH-EU", "APAC": "WH-EU"}
WAREHOUSE_FALLBACK = {"WH-EAST": ["WH-WEST", "WH-EU"], "WH-WEST": ["WH-EAST", "WH-EU"], "WH-EU": ["WH-EAST", "WH-WEST"]}
REMEDY_HOURS = {"seal_kit_replacement": 2.0, "controller_board_replacement": 1.5}
PRIORITY = {"safety": 2, "production": 1, "standard": 0}

# The two swarm configurations of the case study (labs 01 and 07): what a first swarm looks like, and what it
# looks like after the coordination bill was read.
PROFILES = {
    "naive": {"batch": "unit", "brief": "long", "report": "verbose", "digest": False},
    "tuned": {"batch": "customer", "brief": "short", "report": "compact", "digest": True},
}


# ============================================================================== data
@lru_cache(maxsize=None)
def _load(name: str) -> Any:
    path = RECALL_DIR / name
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))


def campaign() -> dict:
    return _load("campaign.json")


def units() -> list[dict]:
    return _load("affected_units.json")["units"]


def unit(serial: str) -> dict:
    for u in units():
        if u["serial_number"] == serial:
            return u
    raise KeyError(serial)


def contacts() -> dict[str, dict]:
    return {c["customer_id"]: c for c in _load("contacts.json")["contacts"]}


def engineers() -> list[dict]:
    return _load("engineers.json")["engineers"]


def kits() -> list[dict]:
    return _load("parts.json")["kits"]


def replies() -> list[dict]:
    return _load("inbound_replies.jsonl")


def serials() -> list[str]:
    return [u["serial_number"] for u in units()]


def add_business_days(day: dt.date, n: int) -> dt.date:
    while n > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def deadlines(risk_class: str) -> tuple[str, str]:
    """(contact_by, remedy_by) from the campaign's priority rules, in business days from the issue date."""
    rule = next(r for r in campaign()["priority_rules"] if r["risk_class"] == risk_class)
    return (add_business_days(ISSUED, rule["contact_within_business_days"]).isoformat(),
            add_business_days(ISSUED, rule["remedy_within_business_days"]).isoformat())


def kit_for(sku: str) -> dict:
    return next(k for k in kits() if sku in k["fits"])


def campaign_notice() -> str:
    """The campaign in ~150 words: what every agent in the swarm needs to know (and a cacheable prefix)."""
    c = campaign()
    lines = [f"Recall {c['recall_id']} issued {c['issued']}: {c['title']}."]
    for lot, info in c["lots"].items():
        lines.append(f"Lot {lot} ({', '.join(info['affects'])}): {info['hazard']} Remedy: {info['remedy']} "
                     f"Interim: {info['interim']}")
    for r in c["priority_rules"]:
        lines.append(f"Risk class {r['risk_class']}: contact within {r['contact_within_business_days']} business "
                     f"day(s), remedy within {r['remedy_within_business_days']}.")
    b = c["budget"]
    lines.append(f"Budget: campaign cap ${b['campaign_cap_usd']:,}, per unit ${b['per_unit_cap_usd']:,}, model spend cap "
                 f"${b['model_spend_cap_usd']:.2f}. Stop conditions: " + "; ".join(c["stop_conditions"]) + ".")
    return "\n".join(lines)


# ============================================================================== the system of record
class ToolFailure(Exception):
    """A tool-level failure the executor turns into an is_error tool result (the model sees it and adapts)."""


class RecallDesk:
    """The campaign's system of record: units, contacts, kit stock and engineer calendars, with the side effects
    (`reserve_kit`, `book_slot`) keyed by serial so that a retry never double-books.  Thread-safe: labs 02 and 03
    run several workers against one desk.  `faults` maps a tool name to a callable that may raise (outages)."""

    def __init__(self, *, faults: dict[str, Callable[[dict], None]] | None = None) -> None:
        self.stock: dict[str, dict[str, int]] = {k["sku"]: dict(k["stock"]) for k in kits()}
        self.slots: dict[str, dict] = {}
        for eng in engineers():
            for s in eng["slots"]:
                self.slots[s["slot_id"]] = {**s, "engineer_id": eng["engineer_id"], "engineer": eng["name"],
                                            "region": eng["region"], "skills": list(eng["skills"])}
        self.reservations: dict[str, dict] = {}         # serial -> reservation
        self.bookings: dict[str, dict] = {}             # serial -> booking
        self.calls: Counter = Counter()
        self.faults = faults or {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ dispatch
    def run(self, name: str, tool_input: dict) -> dict:
        with self._lock:
            self.calls[name] += 1
            if name in self.faults:
                self.faults[name](tool_input)           # may raise ToolFailure (an outage) or do nothing
            handler = getattr(self, f"t_{name}", None)
            if handler is None:
                raise ToolFailure(f"unknown tool {name}")
            try:
                return handler(**tool_input)
            except KeyError as exc:                     # an identifier the desk does not know is a tool error
                raise ToolFailure(f"not found: {exc.args[0]}") from None

    # ------------------------------------------------------------------ read tools
    def t_list_affected_units(self) -> dict:
        rows = [{"serial": u["serial_number"], "sku": u["sku"], "customer_id": u["customer_id"], "customer": u["customer"],
                 "region": u["region"], "risk_class": u["risk_class"], "remedy": u["remedy"]} for u in units()]
        return {"recall_id": campaign()["recall_id"], "count": len(rows), "units": rows}

    def t_get_unit(self, serial: str) -> dict:
        u = unit(serial)
        contact_by, remedy_by = deadlines(u["risk_class"])
        kit = kit_for(u["sku"])
        return {"serial": serial, "sku": u["sku"], "lot": u["lot"], "customer_id": u["customer_id"], "customer": u["customer"],
                "tier": u["tier"], "site_id": u["site_id"], "region": u["region"], "country": u["country"],
                "application": u["application"], "installed": u["installed"], "risk_class": u["risk_class"],
                "remedy": u["remedy"], "skill_required": SKILL_FOR_REMEDY[u["remedy"]], "kit_sku": kit["sku"],
                "visit_hours": REMEDY_HOURS[u["remedy"]], "contact_by": contact_by, "remedy_by": remedy_by,
                "interim_measure": campaign()["lots"][u["lot"]]["interim"]}

    def t_get_contacts(self, customer_id: str) -> dict:
        c = contacts()[customer_id]
        return {"customer_id": customer_id, "customer": c["customer"], "tier": c["tier"], "account_manager": c["account_manager"],
                "timezone": c["timezone"], "language": c["language"], "primary": c["primary"], "site": c["site"],
                "notes": c["notes"], "do_not_contact_before_utc": c["do_not_contact_before_utc"],
                "escalation": campaign()["channels"]["escalation"]}

    def t_check_parts(self, kit_sku: str, region: str) -> dict:
        kit = next((k for k in kits() if k["sku"] == kit_sku), None)
        if kit is None:
            raise ToolFailure(f"unknown kit {kit_sku}")
        stock = dict(self.stock[kit_sku])
        preferred = WAREHOUSE_FOR_REGION.get(region, "WH-EAST")
        alternatives = [w for w in WAREHOUSE_FALLBACK.get(preferred, []) if w in stock]   # nearest first
        return {"kit_sku": kit_sku, "name": kit["name"], "unit_cost_usd": kit["unit_cost_usd"], "lead_time_days": kit["lead_time_days"],
                "stock": stock, "preferred_warehouse": preferred, "alternatives": alternatives}

    def t_find_engineer_slots(self, region: str, skill: str, not_after: str, limit: int = 3) -> dict:
        free = sorted((s for s in self.slots.values() if s["status"] == "free" and s["region"] == region
                       and skill in s["skills"] and s["start"][:10] <= not_after), key=lambda s: (s["start"], s["slot_id"]))
        return {"region": region, "skill": skill, "not_after": not_after, "matches": len(free),
                "slots": [{"slot_id": s["slot_id"], "engineer_id": s["engineer_id"], "engineer": s["engineer"],
                           "start": s["start"], "end": s["end"]} for s in free[:limit]]}

    # ------------------------------------------------------------------ side effects (idempotent per serial)
    def t_reserve_kit(self, kit_sku: str, warehouse: str, serial: str) -> dict:
        if serial in self.reservations:
            return {**self.reservations[serial], "note": "already reserved for this serial (idempotent)"}
        stock = self.stock.get(kit_sku)
        if stock is None or warehouse not in stock:
            raise ToolFailure(f"unknown kit or warehouse: {kit_sku} @ {warehouse}")
        if stock[warehouse] <= 0:
            alternatives = {w: n for w, n in stock.items() if n > 0}
            raise ToolFailure(f"{kit_sku} out of stock at {warehouse}; stock elsewhere: {json.dumps(alternatives)}")
        stock[warehouse] -= 1
        reservation = {"reservation_id": f"RSV-{len(self.reservations) + 1:03d}", "kit_sku": kit_sku, "warehouse": warehouse,
                       "serial": serial}
        self.reservations[serial] = reservation
        return reservation

    def t_book_slot(self, slot_id: str, serial: str) -> dict:
        if serial in self.bookings:
            return {**self.bookings[serial], "note": "already booked for this serial (idempotent)"}
        slot = self.slots.get(slot_id)
        if slot is None:
            raise ToolFailure(f"unknown slot {slot_id}")
        if slot["status"] != "free":
            raise ToolFailure(f"slot {slot_id} is no longer free (status {slot['status']})")
        slot["status"] = "booked"
        booking = {"booking_id": f"BK-{len(self.bookings) + 1:03d}", "slot_id": slot_id, "engineer_id": slot["engineer_id"],
                   "engineer": slot["engineer"], "start": slot["start"], "serial": serial}
        self.bookings[serial] = booking
        return booking

    def release(self, serial: str) -> None:
        """Compensation: undo a unit's reservation and booking (used when a plan is abandoned)."""
        with self._lock:
            r = self.reservations.pop(serial, None)
            if r:
                self.stock[r["kit_sku"]][r["warehouse"]] += 1
            b = self.bookings.pop(serial, None)
            if b:
                self.slots[b["slot_id"]]["status"] = "free"

    # ------------------------------------------------------------------ bulk views and commits (the hosted swarm's tools)
    def campaign_data(self) -> dict:
        """Every unit (as get_unit returns it) and every customer's contacts, in one document."""
        with self._lock:
            self.calls["get_campaign_data"] += 1
            return {"recall_id": campaign()["recall_id"], "units": [self.t_get_unit(s) for s in serials()],
                    "contacts": [self.t_get_contacts(c) for c in sorted({u["customer_id"] for u in units()})]}

    def resources(self) -> dict:
        """A snapshot of kit stock and of the free slots in the regions with affected units."""
        with self._lock:
            self.calls["get_resources"] += 1
            regions = sorted({u["region"] for u in units()})
            free = sorted((s for s in self.slots.values() if s["status"] == "free" and s["region"] in regions),
                          key=lambda s: (s["start"], s["slot_id"]))
            return {"as_of": "snapshot", "warehouse_for_region": {r: WAREHOUSE_FOR_REGION[r] for r in regions},
                    "kits": [{"sku": sku, "stock": dict(stock)} for sku, stock in self.stock.items()],
                    "free_slots": [{"slot_id": s["slot_id"], "engineer_id": s["engineer_id"], "region": s["region"],
                                    "skills": s["skills"], "start": s["start"]} for s in free]}

    def commit(self, plan: dict) -> dict:
        """Validate a proposed unit plan against the campaign rules, then reserve and book - all or nothing.
        This is where policy lives when agents only *propose*: the model's plan is a request, not a decision."""
        with self._lock:
            serial = plan.get("serial", "")
            try:
                u = self.t_get_unit(serial)
            except KeyError:
                raise ToolFailure(f"unknown serial {serial!r}") from None
            if plan.get("status") != "scheduled":
                return {"serial": serial, "status": plan.get("status"), "committed": False}
            slot = self.slots.get(plan.get("slot_id") or "")
            if slot is None:
                raise ToolFailure(f"{serial}: unknown slot {plan.get('slot_id')!r}")
            if slot["region"] != u["region"] or u["skill_required"] not in slot["skills"]:
                raise ToolFailure(f"{serial}: engineer {slot['engineer_id']} is outside {u['region']} or lacks {u['skill_required']}")
            if slot["start"][:10] > u["remedy_by"]:
                raise ToolFailure(f"{serial}: visit {slot['start'][:10]} is after remedy_by {u['remedy_by']}")
            if plan.get("kit_sku") != u["kit_sku"]:
                raise ToolFailure(f"{serial}: kit {plan.get('kit_sku')!r} does not fit {u['sku']} (needs {u['kit_sku']})")
            reservation = self.t_reserve_kit(u["kit_sku"], plan.get("warehouse") or "", serial)
            try:
                booking = self.t_book_slot(slot["slot_id"], serial)
            except ToolFailure:
                self.release(serial)                    # compensate: no kit held for a visit that was not booked
                raise
            return {"serial": serial, "status": "scheduled", "committed": True, "reservation_id": reservation["reservation_id"],
                    "booking_id": booking["booking_id"], "slot_id": booking["slot_id"], "warehouse": reservation["warehouse"]}


# ============================================================================== tool definitions and prompts
def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required}}


S = {"type": "string"}
WORKER_TOOLS: list[dict] = [
    _tool("get_unit", "The affected unit: customer, site, risk class, remedy, required skill and kit, contact/remedy deadlines.",
          {"serial": S}, ["serial"]),
    _tool("get_contacts", "Primary and site contacts of a customer, account manager, notes (e.g. out of office).",
          {"customer_id": S}, ["customer_id"]),
    _tool("check_parts", "Stock of a remedy kit per warehouse, the preferred warehouse for a region and the alternatives.",
          {"kit_sku": S, "region": S}, ["kit_sku", "region"]),
    _tool("find_engineer_slots", "Earliest free field-service slots in a region for engineers with a skill, on or before a date.",
          {"region": S, "skill": S, "not_after": {"type": "string", "description": "ISO date"}, "limit": {"type": "integer"}},
          ["region", "skill", "not_after"]),
    _tool("reserve_kit", "Reserve one remedy kit at a warehouse for a serial (a side effect; idempotent per serial).",
          {"kit_sku": S, "warehouse": S, "serial": S}, ["kit_sku", "warehouse", "serial"]),
    _tool("book_slot", "Book an engineer slot for a serial (a side effect; idempotent per serial).",
          {"slot_id": S, "serial": S}, ["slot_id", "serial"]),
]
LIST_UNITS_TOOL = _tool("list_affected_units", "The units in the recall campaign (serial, customer, region, risk class, remedy).",
                        {}, [])
COORDINATOR_TOOLS: list[dict] = [
    LIST_UNITS_TOOL,
    _tool("dispatch_units", "Enqueue unit batches for the worker agents and drain the queue; returns queue statistics. "
                            "Idempotent: batches already queued or done are not repeated.",
          {"batches": {"type": "array", "items": {"type": "object", "properties": {
              "serials": {"type": "array", "items": S}, "brief": S}, "required": ["serials"]}}}, ["batches"]),
    _tool("collect_results", "The workers' results so far, plus the queue's statistics (queued, done, dead letters).", {}, []),
]

WORKER_INSTRUCTIONS = """\
You are a recall-remedy planner at Kestrel Pumps & Controls working ONE batch of affected units for campaign \
RC-2026-03. For each unit: get_unit, then (in one turn) get_contacts, check_parts and find_engineer_slots; then \
reserve_kit at the preferred warehouse (fall back to an alternative with stock) and book_slot for the earliest slot \
on or before remedy_by. Use the site contact when the primary contact is out of office. If scheduling is unavailable \
or no slot fits, report status "pending_schedule"; if no kit is in stock anywhere, "waiting_parts". Finish with a JSON \
object per unit: serial, customer_id, status, remedy, kit_sku, warehouse, reservation_id, engineer_id, slot_id, \
visit_start, contact_id, contact_by, remedy_by, sla_ok, notes. Never promise compensation and never change hazard wording."""

COORDINATOR_INSTRUCTIONS = """\
You coordinate the recall remedy campaign RC-2026-03. You do not plan units yourself: list_affected_units, then \
dispatch_units in batches (one batch per customer unless told otherwise; a short brief per batch), then \
collect_results and write the campaign summary: units scheduled / waiting parts / pending schedule, kits by \
warehouse, SLA risks, and what needs a human decision. Pause and report if a stop condition is met."""

SINGLE_AGENT_INSTRUCTIONS = """\
You plan the recall remedy for every affected unit of campaign RC-2026-03 yourself, one unit after another: \
list_affected_units, then for each unit get_unit, get_contacts, check_parts and find_engineer_slots, then reserve_kit \
and book_slot for the earliest slot on or before that unit's remedy_by. Finish with one JSON object per unit: serial, \
customer_id, status, remedy, kit_sku, warehouse, reservation_id, engineer_id, slot_id, visit_start, contact_id, \
contact_by, remedy_by, sla_ok, notes. Never promise compensation and never change hazard wording."""


def worker_system(*, report: str = "compact", mode: str = "normal", cache: bool = True) -> str | list[dict]:
    text = (f'<adv_day4_worker report="{report}" mode="{mode}">\n{WORKER_INSTRUCTIONS}\n</adv_day4_worker>\n\n'
            f"<campaign_notice>\n{campaign_notice()}\n</campaign_notice>")
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}] if cache else text


def coordinator_system(*, batch: str = "customer", brief: str = "short") -> list[dict]:
    text = (f'<adv_day4_coordinator batch="{batch}" brief="{brief}">\n{COORDINATOR_INSTRUCTIONS}\n</adv_day4_coordinator>\n\n'
            f"<campaign_notice>\n{campaign_notice()}\n</campaign_notice>")
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def single_agent_system() -> list[dict]:
    text = (f"<adv_day4_single_agent>\n{SINGLE_AGENT_INSTRUCTIONS}\n</adv_day4_single_agent>\n\n"
            f"<campaign_notice>\n{campaign_notice()}\n</campaign_notice>")
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def worker_message(serials_: list[str], brief: str | None = None) -> str:
    head = f"Plan the recall remedy for unit(s): {', '.join(serials_)} (campaign RC-2026-03)."
    return f"{head}\n\n<brief>\n{brief}\n</brief>" if brief else head


# ============================================================================== executors
def make_executor(desk: RecallDesk, *, tracer: Any = None, on_call: Callable[[str, dict], None] | None = None,
                  after_call: Callable[[str, dict, dict], None] | None = None,
                  crash_after_calls: int | None = None) -> Callable:
    """A DurableRunner executor over the desk. Side-effect tools go through ctx.effect() (at-most-once per run and
    tool call); read tools just run.  `on_call` runs before a call and may raise ToolFailure to veto it (budget
    guards, breakers, loop detectors); `after_call` sees every result (breakers count failures there)."""
    state = {"calls": 0}

    def execute(name: str, tool_input: dict, ctx: ToolContext) -> Any:
        state["calls"] += 1
        if crash_after_calls is not None and state["calls"] > crash_after_calls:
            raise Crash(f"worker died before {name} (call #{state['calls']})")
        if on_call is not None:
            try:
                on_call(name, tool_input)
            except ToolFailure as exc:                    # vetoed: the model sees an error result, the desk is not called
                return {"error": str(exc)}

        def do() -> dict:
            try:
                return desk.run(name, tool_input)
            except ToolFailure as exc:
                return {"error": str(exc)}

        span = tracer.span(f"tool.{name}", **{k: v for k, v in tool_input.items() if isinstance(v, (str, int))}) \
            if tracer is not None else None
        with (span if span is not None else _null()) as sp:
            if name in ("reserve_kit", "book_slot"):
                with ctx.effect() as eff:
                    if eff.done:
                        return eff.stored
                    result = do()
                    if "error" not in result:
                        eff.commit(result)
            else:
                result = do()
            if sp is not None and "error" in result:
                sp.error(result["error"])
            if after_call is not None:
                after_call(name, tool_input, result)
            return result

    return execute


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ============================================================================== running one worker
@dataclass
class WorkerResult:
    outcome: Outcome
    plans: list[dict]
    run_id: str


def parse_plans(text: str) -> list[dict]:
    """Every JSON object with a serial in a reply (workers end with one object per unit, possibly inside prose)."""
    found: list[dict] = []
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(obj, dict) and "serial" in obj:
            found.append(obj)
        elif isinstance(obj, dict) and isinstance(obj.get("plans"), list):
            found.extend(p for p in obj["plans"] if isinstance(p, dict))
        i = end
    return found


def run_unit_worker(client: Any, store: RunStore, desk: RecallDesk, serials_: list[str], *, run_id: str | None = None,
                    worker: str = "worker", brief: str | None = None, report: str = "compact", mode: str = "normal",
                    model: str = MODEL, max_turns: int = 40, crash_after_calls: int | None = None,
                    crash_at: tuple[str, int] | None = None, tracer: Any = None,
                    on_call: Callable[[str, dict], None] | None = None,
                    after_call: Callable[[str, dict, dict], None] | None = None) -> WorkerResult:
    """One worker agent = one durable run over the worker tools. Re-running the same run_id resumes it."""
    run_id = run_id or f"unit:{'+'.join(serials_)}"
    store.create("unit_plan", input={"message": worker_message(serials_, brief), "serials": serials_}, run_id=run_id,
                 tags={"serials": serials_})
    runner = DurableRunner(store, client, model=model, system=worker_system(report=report, mode=mode), tools=WORKER_TOOLS,
                           execute=make_executor(desk, tracer=tracer, on_call=on_call, after_call=after_call,
                                                 crash_after_calls=crash_after_calls),
                           max_turns=max_turns, worker=worker, crash_at=crash_at,
                           create_kwargs={"cache_control": {"type": "ephemeral"}})
    outcome = runner.run(run_id)
    return WorkerResult(outcome=outcome, plans=parse_plans(outcome.reply) if outcome.status == "completed" else [], run_id=run_id)


# ============================================================================== the work queue
@dataclass
class Task:
    task_id: str
    kind: str
    payload: dict
    priority: int
    status: str
    owner: str | None
    attempts: int
    max_attempts: int
    version: int
    result: Any = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(task_id=row["task_id"], kind=row["kind"], payload=json.loads(row["payload"]), priority=row["priority"],
                   status=row["status"], owner=row["owner"], attempts=row["attempts"], max_attempts=row["max_attempts"],
                   version=row["version"], result=json.loads(row["result"]) if row["result"] else None, error=row["error"])


QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, kind TEXT, payload TEXT, priority INTEGER, status TEXT, owner TEXT, lease_until REAL,
    attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, version INTEGER DEFAULT 0, result TEXT, error TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, attempt INTEGER, owner TEXT, payload TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, version INTEGER, data TEXT, updated_at TEXT);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class WorkQueue:
    """A SQLite work queue: `enqueue` is idempotent (task_id), `claim` is one atomic statement that takes the highest
    priority queued task (or one whose lease expired), oldest first, `complete` / `fail` release it, a task that
    exhausts its attempts - or fails permanently - becomes a dead letter (status 'dead') for a human, and `results` is
    append-only and keeps every attempt. The same file also holds `SharedRecord` documents.

    "Oldest first" is insertion order (SQLite's rowid), not `created_at`: a producer that enqueues a batch writes
    several rows in the same millisecond, and a timestamp tie would make the claim order vary from run to run."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn().executescript(QUEUE_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ producers
    def enqueue(self, task_id: str, kind: str, payload: dict, *, priority: int = 0, max_attempts: int = 3) -> bool:
        try:
            self._conn().execute("INSERT INTO tasks (task_id, kind, payload, priority, status, attempts, max_attempts, version, "
                                 "created_at, updated_at) VALUES (?,?,?,?,'queued',0,?,0,?,?)",
                                 (task_id, kind, json.dumps(payload), priority, max_attempts, _now(), _now()))
            return True
        except sqlite3.IntegrityError:
            return False                                   # already queued/done: at-least-once producers are harmless

    # ------------------------------------------------------------------ consumers
    def claim(self, owner: str, *, lease_s: float = 30.0, now: float | None = None) -> Task | None:
        now = time.time() if now is None else now
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "UPDATE tasks SET status = 'claimed', owner = ?, lease_until = ?, attempts = attempts + 1, "
                "version = version + 1, updated_at = ? WHERE task_id = (SELECT task_id FROM tasks WHERE status = 'queued' "
                "OR (status = 'claimed' AND lease_until < ?) ORDER BY priority DESC, rowid LIMIT 1) "
                "RETURNING *", (owner, now + lease_s, _now(), now)).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return Task.from_row(row) if row else None

    def claim_specific(self, task_id: str, owner: str, *, expected_version: int, lease_s: float = 30.0) -> bool:
        """Optimistic claim of one known task: succeeds only if nobody changed it since the caller read it."""
        cur = self._conn().execute(
            "UPDATE tasks SET status = 'claimed', owner = ?, lease_until = ?, attempts = attempts + 1, version = version + 1, "
            "updated_at = ? WHERE task_id = ? AND version = ? AND status = 'queued'",
            (owner, time.time() + lease_s, _now(), task_id, expected_version))
        return cur.rowcount == 1

    def heartbeat(self, task_id: str, owner: str, *, lease_s: float = 30.0) -> bool:
        cur = self._conn().execute("UPDATE tasks SET lease_until = ? WHERE task_id = ? AND owner = ? AND status = 'claimed'",
                                   (time.time() + lease_s, task_id, owner))
        return cur.rowcount == 1

    def complete(self, task_id: str, owner: str, result: Any) -> bool:
        conn = self._conn()
        task = self.get(task_id)
        cur = conn.execute("UPDATE tasks SET status = 'done', result = ?, error = NULL, version = version + 1, updated_at = ? "
                           "WHERE task_id = ? AND owner = ? AND status = 'claimed'", (json.dumps(result), _now(), task_id, owner))
        if cur.rowcount == 1:
            conn.execute("INSERT INTO results (task_id, attempt, owner, payload, at) VALUES (?,?,?,?,?)",
                         (task_id, task.attempts, owner, json.dumps(result), _now()))
        return cur.rowcount == 1

    def fail(self, task_id: str, owner: str, error: str, *, retry: bool = True) -> str:
        """Release a failed claim. A transient failure goes back to 'queued' while attempts remain; a permanent one
        (retry=False) or the last attempt becomes a dead letter. Returns the new status."""
        task = self.get(task_id)
        status = "queued" if retry and task.attempts < task.max_attempts else "dead"
        self._conn().execute("UPDATE tasks SET status = ?, owner = NULL, lease_until = NULL, error = ?, version = version + 1, "
                             "updated_at = ? WHERE task_id = ? AND owner = ?", (status, error, _now(), task_id, owner))
        return status

    def reap(self, *, now: float | None = None) -> list[str]:
        """The supervisor's sweep: claimed tasks whose lease expired (a dead worker) go back to the queue - or to the
        dead letters when they have used all their attempts."""
        now = time.time() if now is None else now
        rows = self._conn().execute("SELECT * FROM tasks WHERE status = 'claimed' AND lease_until < ?", (now,)).fetchall()
        out = []
        for row in rows:
            task = Task.from_row(row)
            status = "dead" if task.attempts >= task.max_attempts else "queued"
            self._conn().execute("UPDATE tasks SET status = ?, owner = NULL, lease_until = NULL, error = ?, version = version + 1, "
                                 "updated_at = ? WHERE task_id = ?", (status, f"lease expired (owner {task.owner})", _now(), task.task_id))
            out.append(task.task_id)
        return out

    # ------------------------------------------------------------------ views
    def get(self, task_id: str) -> Task:
        row = self._conn().execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return Task.from_row(row)

    def tasks(self, status: str | None = None) -> list[Task]:
        sql, args = "SELECT * FROM tasks", []
        if status:
            sql, args = sql + " WHERE status = ?", [status]
        return [Task.from_row(r) for r in self._conn().execute(sql + " ORDER BY priority DESC, rowid", args)]

    def dead_letters(self) -> list[Task]:
        return self.tasks("dead")

    def stats(self) -> dict[str, int]:
        rows = self._conn().execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status ORDER BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def results(self, *, latest_only: bool = True) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM results ORDER BY seq").fetchall()
        out = [{"task_id": r["task_id"], "attempt": r["attempt"], "owner": r["owner"], "payload": json.loads(r["payload"])}
               for r in rows]
        if latest_only:
            latest: dict[str, dict] = {}
            for r in out:
                latest[r["task_id"]] = r
            out = list(latest.values())
        return out


class SharedRecord:
    """A versioned JSON document with compare-and-set writes (optimistic concurrency)."""

    def __init__(self, queue: WorkQueue, key: str, initial: dict | None = None) -> None:
        self.queue, self.key = queue, key
        try:
            queue._conn().execute("INSERT INTO records (key, version, data, updated_at) VALUES (?, 1, ?, ?)",
                                  (key, json.dumps(initial or {}), _now()))
        except sqlite3.IntegrityError:
            pass

    def read(self) -> tuple[int, dict]:
        row = self.queue._conn().execute("SELECT version, data FROM records WHERE key = ?", (self.key,)).fetchone()
        return int(row["version"]), json.loads(row["data"])

    def write(self, data: dict, *, expected_version: int) -> bool:
        cur = self.queue._conn().execute("UPDATE records SET data = ?, version = version + 1, updated_at = ? WHERE key = ? "
                                         "AND version = ?", (json.dumps(data), _now(), self.key, expected_version))
        return cur.rowcount == 1

    def update(self, fn: Callable[[dict], dict], *, max_retries: int = 10,
               between: Callable[[], None] | None = None) -> tuple[int, int]:
        """Read-modify-write with retry on conflict. Returns (new version, conflicts seen). `between` runs after the
        read and before the write (the window in which another writer can get in first)."""
        conflicts = 0
        for _ in range(max_retries + 1):
            version, data = self.read()
            new = fn(dict(data))
            if between is not None:
                between()
            if self.write(new, expected_version=version):
                return version + 1, conflicts
            conflicts += 1
        raise RuntimeError(f"{self.key}: gave up after {conflicts} conflicts")


# ============================================================================== metering per role
@dataclass
class RoleTotals:
    calls: int = 0
    input_tokens: int = 0            # everything read: uncached + cache writes + cache reads
    cache_read: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    uncached_cost: float = 0.0       # the same tokens priced with no cache at all (like-for-like with the hosted mock)
    largest_prompt: int = 0


class Meter:
    """Token/cost totals per role (coordinator, worker, ...), read from durable-run logs or from responses."""

    def __init__(self) -> None:
        self.roles: dict[str, RoleTotals] = {}

    def add_usage(self, role: str, usage: dict, model: str) -> None:
        t = self.roles.setdefault(role, RoleTotals())
        prompt = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) + \
            int(usage.get("cache_creation_input_tokens") or 0)
        t.calls += 1
        t.input_tokens += prompt
        t.cache_read += int(usage.get("cache_read_input_tokens") or 0)
        t.output_tokens += int(usage.get("output_tokens") or 0)
        t.cost += cost_usd(usage, model)
        t.uncached_cost += cost_usd({"input_tokens": prompt, "output_tokens": int(usage.get("output_tokens") or 0)}, model)
        t.largest_prompt = max(t.largest_prompt, prompt)

    def add_run(self, role: str, store: RunStore, run_id: str, model: str = MODEL) -> None:
        for event in store.events(run_id, types=("model.response",)):
            self.add_usage(role, event["usage"], model)

    def add_response(self, role: str, response: Any) -> None:
        self.add_usage(role, response.usage.model_dump(), response.model)

    def total(self) -> RoleTotals:
        t = RoleTotals()
        for r in self.roles.values():
            t.calls += r.calls
            t.input_tokens += r.input_tokens
            t.cache_read += r.cache_read
            t.output_tokens += r.output_tokens
            t.cost += r.cost
            t.uncached_cost += r.uncached_cost
            t.largest_prompt = max(t.largest_prompt, r.largest_prompt)
        return t

    def rows(self) -> list[list]:
        return [[role, t.calls, f"{t.input_tokens:,}", f"{t.cache_read:,}", f"{t.output_tokens:,}", money(t.cost),
                 f"{t.largest_prompt:,}"] for role, t in self.roles.items()]


METER_HEADERS = ["role", "calls", "input tok", "cache read", "output tok", "cost", "largest prompt"]


# ============================================================================== the checker (ground truth in code)
def expected_plan(serial: str) -> dict:
    """What a correct plan must contain, derived from the dataset and the campaign rules - never from a model."""
    u = unit(serial)
    contact_by, remedy_by = deadlines(u["risk_class"])
    c = contacts()[u["customer_id"]]
    contact = c["site"] if "out of office" in (c["notes"] or "").lower() else c["primary"]
    return {"serial": serial, "customer_id": u["customer_id"], "remedy": u["remedy"], "kit_sku": kit_for(u["sku"])["sku"],
            "skill": SKILL_FOR_REMEDY[u["remedy"]], "region": u["region"], "contact_id": contact["contact_id"],
            "contact_by": contact_by, "remedy_by": remedy_by}


def check_plan(plan: dict, desk: RecallDesk | None = None) -> list[str]:
    """Problems with a unit plan (empty list = the plan is correct). Slots and warehouses may legitimately differ
    between runs, so the checker verifies properties, not identities."""
    problems: list[str] = []
    serial = plan.get("serial")
    try:
        exp = expected_plan(serial)
    except (KeyError, StopIteration):
        return [f"unknown serial {serial!r}"]
    for key in ("customer_id", "remedy", "kit_sku", "contact_id", "remedy_by"):
        if plan.get(key) != exp[key]:
            problems.append(f"{key}={plan.get(key)!r} (expected {exp[key]!r})")
    status = plan.get("status")
    if status == "scheduled":
        if not plan.get("slot_id"):
            problems.append("scheduled without a booked slot")
        if desk is not None:
            booking = desk.bookings.get(serial)
            slot = desk.slots.get(plan.get("slot_id") or "")
            if booking is None or booking["slot_id"] != plan.get("slot_id"):
                problems.append("slot_id does not match the desk's booking")
            elif slot is not None:
                if slot["region"] != exp["region"] or exp["skill"] not in slot["skills"]:
                    problems.append("engineer outside the region or without the skill")
                if slot["start"][:10] > exp["remedy_by"]:
                    problems.append(f"visit {slot['start'][:10]} after remedy_by {exp['remedy_by']}")
            if desk.reservations.get(serial, {}).get("kit_sku") != exp["kit_sku"]:
                problems.append("no matching kit reservation on the desk")
    elif status not in ("pending_schedule", "waiting_parts"):
        problems.append(f"unknown status {status!r}")
    if any(w in json.dumps(plan).lower() for w in ("compensation", "goodwill credit")):
        problems.append("promises compensation")
    return problems


def score(plans: list[dict], desk: RecallDesk | None = None) -> dict:
    """Per-unit verdicts: 'ok' = a correct plan with a valid booking; 'escalated' = correct, honestly not scheduled."""
    by_serial = {p.get("serial"): p for p in plans}
    rows, ok, escalated = [], 0, 0
    for s in serials():
        p = by_serial.get(s)
        problems = check_plan(p, desk) if p else ["no plan produced"]
        status = (p or {}).get("status", "-")
        ok += not problems and status == "scheduled"
        escalated += not problems and status != "scheduled"
        rows.append({"serial": s, "status": status, "problems": problems})
    return {"ok": ok, "escalated": escalated, "total": len(serials()), "rows": rows}


# ============================================================================== Managed Agents helpers (labs 05-07)
def custom_tool(defn: dict) -> dict:
    """A Messages-API tool definition as a Managed Agents custom tool (your application executes it)."""
    return {"type": "custom", "name": defn["name"], "description": defn["description"], "input_schema": defn["input_schema"]}


def get_or_create_agent(client: Any, name: str, **config: Any) -> Any:
    """Agents are persistent and versioned: create once, then update in place (a new version) only when the
    configuration you want differs from the stored one. Never create a fresh agent per run."""
    for agent in client.beta.agents.list():
        if agent.name == name and not getattr(agent, "archived_at", None):
            wanted = {k: v for k, v in config.items() if k in ("system", "description")}
            if any(getattr(agent, k, None) != v for k, v in wanted.items()):
                return client.beta.agents.update(agent.id, version=agent.version, **config)
            return agent
    return client.beta.agents.create(name=name, **config)


def get_or_create_environment(client: Any, name: str) -> Any:
    """Environment names are unique (a second create with the same name is a 409), so look it up first."""
    for env in client.beta.environments.list():
        if env.name == name and not getattr(env, "archived_at", None):
            return env
    return client.beta.environments.create(name=name, config={"type": "cloud", "networking": {"type": "unrestricted"}})


def drive_session(client: Any, session_id: str, answer: Callable[[Any], tuple[str, bool]],
                  on_event: Callable[[Any], None] | None = None, *, seen: set[str] | None = None,
                  max_rounds: int = 50) -> Any:
    """Run a session until it stops for a reason other than `requires_action`; returns that stop reason.

    The loop lab 05 builds by hand: open the stream (stream first), fetch the history and de-duplicate by event
    id (the stream does not replay what happened before it opened), hand every event to `on_event`, and when the
    session goes idle with `requires_action`, answer each pending `agent.custom_tool_use` with
    `answer(event) -> (text, is_error)` in ONE send.  Works the same against the mock (which processes events
    synchronously) and the platform (which processes them asynchronously)."""
    seen = set() if seen is None else seen
    uses: dict[str, Any] = {}
    stop = None
    for _ in range(max_rounds):
        stop = None
        with client.beta.sessions.events.stream(session_id) as stream:
            def fresh():
                for ev in client.beta.sessions.events.list(session_id):
                    yield ev
                for ev in stream:
                    yield ev
            for ev in fresh():
                if ev.id in seen:
                    continue
                seen.add(ev.id)
                if ev.type == "agent.custom_tool_use":
                    uses[ev.id] = ev
                if on_event is not None:
                    on_event(ev)
                if ev.type == "session.status_idle":
                    stop = ev.stop_reason
                    break
                if ev.type == "session.status_terminated":
                    return None
        if stop is None or stop.type != "requires_action":
            return stop
        results = []
        for event_id in stop.event_ids:
            text, is_error = answer(uses[event_id])
            result = {"type": "user.custom_tool_result", "custom_tool_use_id": event_id,
                      "content": [{"type": "text", "text": text}]}
            if is_error:
                result["is_error"] = True
            if getattr(uses[event_id], "session_thread_id", None):
                result["session_thread_id"] = uses[event_id].session_thread_id
            results.append(result)
        client.beta.sessions.events.send(session_id, events=results)
    return stop


def describe_event(ev: Any, width: int = 70) -> str:
    """One line per session event: its type and the fields that matter for that type."""
    def short(text: str) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= width else text[: width - 3] + "..."
    kind = ev.type
    if kind in ("user.message", "agent.message"):
        return f"{kind:<32} {short(' '.join(getattr(b, 'text', '') for b in ev.content))}"
    if kind == "agent.custom_tool_use":
        thread = f" (thread {ev.session_thread_id})" if getattr(ev, "session_thread_id", None) else ""
        return f"{kind:<32} {ev.name}({short(json.dumps(ev.input))}){thread}"
    if kind == "user.custom_tool_result":
        return f"{kind:<32} for {ev.custom_tool_use_id}: {short(' '.join(getattr(b, 'text', '') for b in ev.content))}"
    if kind == "agent.tool_use":
        return f"{kind:<32} {ev.name}({short(json.dumps(ev.input))}) permission={ev.evaluated_permission}"
    if kind == "agent.tool_result":
        return f"{kind:<32} {'error ' if ev.is_error else ''}{short(' '.join(getattr(b, 'text', '') for b in ev.content))}"
    if kind in ("session.status_idle", "session.thread_status_idle"):
        stop = ev.stop_reason
        who = f" {ev.agent_name}" if getattr(ev, "agent_name", None) else ""
        ids = f" event_ids={stop.event_ids}" if getattr(stop, "event_ids", None) else ""
        return f"{kind:<32}{who} stop_reason={stop.type}{ids}"
    if kind == "span.model_request_end":
        u = usage_tokens(ev.model_usage)
        return (f"{kind:<32} in={u['input_tokens']} cache_w={u['cache_creation_input_tokens']} "
                f"cache_r={u['cache_read_input_tokens']} out={u['output_tokens']}")
    if kind == "session.usage":
        return f"{kind:<32} list_cost={ev.usage.list_cost.amount} cents"
    if kind == "session.thread_created":
        return f"{kind:<32} {ev.agent_name} ({ev.session_thread_id})"
    if kind == "agent.thread_message_sent":
        return f"{kind:<32} -> {ev.to_agent_name}: {short(' '.join(getattr(b, 'text', '') for b in ev.content))}"
    if kind == "agent.thread_message_received":
        return f"{kind:<32} <- {ev.from_agent_name}: {short(' '.join(getattr(b, 'text', '') for b in ev.content))}"
    if kind in ("session.thread_status_running",):
        return f"{kind:<32} {ev.agent_name}"
    return kind


def session_events(client: Any, session_id: str) -> list:
    """Every event of a session. The session-level list is the primary thread plus a condensed view of the other
    threads; on the platform each child thread's own events (where its model requests are) come from its thread
    event list, merged here and de-duplicated by id. [mock] the mock puts every event on the session-level list and
    has no per-thread event endpoint."""
    from labkit import is_mock
    events = list(client.beta.sessions.events.list(session_id))
    if is_mock():
        return events
    seen = {e.id for e in events}
    for thread in client.beta.sessions.threads.list(session_id):
        if thread.parent_thread_id is None:
            continue
        for ev in client.beta.sessions.threads.events.list(thread.id, session_id=session_id):
            if ev.id not in seen:
                seen.add(ev.id)
                events.append(ev)
    return events


def usage_tokens(usage: Any) -> dict:
    """Token totals from a session's, a thread's or a span's usage object (cache writes may come as a total or as a
    per-TTL breakdown)."""
    written = getattr(usage, "cache_creation_input_tokens", None)
    if written is None and getattr(usage, "cache_creation", None) is not None:
        breakdown = usage.cache_creation.model_dump() if hasattr(usage.cache_creation, "model_dump") else dict(usage.cache_creation)
        written = sum(int(v or 0) for v in breakdown.values() if isinstance(v, (int, float)))
    return {"input_tokens": int(getattr(usage, "input_tokens", 0) or 0), "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(written or 0),
            "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0)}


def prompt_tokens(totals: dict) -> int:
    """Everything a model read: uncached input + cache writes + cache reads (caching changes the price, not the count)."""
    return int(totals.get("input_tokens") or 0) + int(totals.get("cache_creation_input_tokens") or 0) + \
        int(totals.get("cache_read_input_tokens") or 0)


def uncached_cost(totals: dict, model: str = MODEL) -> float:
    """The same tokens priced with no cache at all - what caching saved, or an architecture's cost before caching."""
    return cost_usd({"input_tokens": prompt_tokens(totals), "output_tokens": int(totals.get("output_tokens") or 0)}, model)


def api_error_message(exc: Exception) -> str:
    """The message of an API error (anthropic.APIStatusError), without the SDK's wrapping."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return str((body.get("error") or {}).get("message") or exc)
    return str(exc)


def session_spend(client: Any, session_id: str, model: str = MODEL) -> dict:
    """What a session consumed: token totals from the session's own usage (authoritative - every thread), the exact
    list price of those tokens, the platform's list_cost (cents, rounded; live it also counts running time), and -
    from the span.model_request_end events of every thread - the number of model requests and the largest prompt."""
    session = client.beta.sessions.retrieve(session_id)
    totals = usage_tokens(session.usage)
    requests, largest = 0, 0
    for ev in session_events(client, session_id):
        if ev.type == "span.model_request_end":
            u = ev.model_usage.model_dump()
            requests += 1
            largest = max(largest, int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                          + int(u.get("cache_creation_input_tokens") or 0))
    return {"requests": requests, **totals, "cost": cost_usd(totals, model), "largest_prompt": largest,
            "list_cost_cents": int(session.usage.list_cost.amount) if session.usage and session.usage.list_cost else 0}


# ============================================================================== printing and misc
def table(rows: list[list], headers: list[str]) -> str:
    cols = [headers] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cols) for i in range(len(headers))]
    lines = ["  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(),
             "  " + "  ".join("-" * w for w in widths)]
    lines += ["  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cols[1:]]
    return "\n".join(lines)


def money(x: float) -> str:
    return f"${x:.4f}"


def mock_note(text: str) -> None:
    from labkit import is_mock
    if is_mock():
        print(f"  [mock] {text}")


def fresh_db(name: str) -> Path:
    from labkit import runs_dir
    path = runs_dir("advanced", "day4") / name
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    return path


def load_lab(stem: str) -> Any:
    """Import a sibling lab as a module (lab 07 reuses labs 01 and 06 so that the comparison is like-for-like)."""
    if str(LABS_DIR) not in sys.path:
        sys.path.insert(0, str(LABS_DIR))
    return importlib.import_module(stem)


# ============================================================================== latency model (illustrative)
# (seconds to first token, output tokens per second) - planning assumptions for a modelled critical path, as in the
# first course's Day 4; replace them with the p50s you measure live.  Never quoted as measurements.
LATENCY_ASSUMPTIONS = {"claude-opus-5": (2.0, 55.0), "claude-sonnet-5": (1.2, 75.0), "claude-haiku-4-5": (0.6, 140.0)}


def modelled_seconds(usage: dict, model: str = MODEL) -> float:
    ttft, tps = LATENCY_ASSUMPTIONS.get(model, (1.5, 60.0))
    uncached = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
    return ttft + uncached / 20_000.0 + int(usage.get("output_tokens") or 0) / tps


def run_seconds(store: RunStore, run_id: str, model: str = MODEL) -> float:
    return sum(modelled_seconds(e["usage"], model) for e in store.events(run_id, types=("model.response",)))
