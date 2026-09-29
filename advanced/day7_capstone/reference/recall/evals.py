"""The acceptance suite: run the whole campaign in a fresh store and check what must be true afterwards.

Scenarios come from the inbound replies themselves (`advanced/data/recall/inbound_replies.jsonl` carries an
`expected` block per reply) plus campaign-level checks. Gates are the go/no-go criteria from the brief:

  G1 security     every hostile reply is quarantined before any model call; zero side effects from them
  G2 SLA          every customer is contacted within its risk class's deadline
  G3 durability   a crash mid-outreach and a resume produce exactly the same outbound messages (none twice)
  G4 approvals    no goodwill credit above the threshold without a recorded human decision
  G5 budget       model spend stays under the cap, and a tiny cap pauses the campaign instead of overspending
  G6 operability  every run finished or is parked with a reason; every outbound message has an audit row
  G7 scenarios    >= 90% of reply scenarios pass, and 100% of the security ones
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import importlib

from advanced.lib.durable import Crash
from labkit import runs_dir

from .config import Settings

HOSTILE = {"fraud", "injection", "data_request", "phishing", "spoofed_internal"}


@dataclass
class Impl:
    """The package under test: the reference (`recall`) or a learner's starter package (`starter.recall`)."""
    base: str
    Orchestrator: Any
    agents: Any
    CampaignStore: Any
    SwarmBudget: Any
    load_replies: Any
    sla_status: Any

    @classmethod
    def load(cls, base: str) -> "Impl":
        orch = importlib.import_module(f"{base}.orchestrator")
        return cls(base=base, Orchestrator=orch.Orchestrator, agents=importlib.import_module(f"{base}.agents"),
                   CampaignStore=importlib.import_module(f"{base}.store").CampaignStore,
                   SwarmBudget=importlib.import_module(f"{base}.budget").SwarmBudget, load_replies=orch.load_replies, sla_status=orch.sla_status)


def reference() -> Impl:
    return Impl.load(__package__)


@dataclass
class Check:
    id: str
    ok: bool
    detail: str
    critical: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    gates: dict[str, tuple[bool, str]] = field(default_factory=dict)

    @property
    def go(self) -> bool:
        return all(ok for ok, _ in self.gates.values())

    def add(self, id: str, ok: bool, detail: str, *, critical: bool = False) -> None:
        self.checks.append(Check(id, ok, detail, critical))


def fresh_store(impl: Impl, name: str):
    base = runs_dir("advanced_capstone", "evals", impl.base.replace(".", "_"))
    for p in base.glob(name + "*"):
        p.unlink()
    return impl.CampaignStore(base / name).load()


def run_campaign(impl: Impl, store, settings: Settings, client: Any, days: int = 5, *, crash: dict | None = None,
                 decide: str = "approve") -> list:
    orchestrator = impl.Orchestrator(store, settings, client, budget=impl.SwarmBudget(settings.model_spend_cap_usd, carried_usd=store.get("spend_usd", 0.0)))
    reports = []
    for day in range(1, days + 1):
        try:
            reports.append(orchestrator.run_day(day, crash=crash if crash and crash["day"] == day else None))
        except Crash:
            crash = None                                                      # "the process died"; the next day-run resumes
            reports.append(orchestrator.run_day(day))
        for run in store.runs.list(status="waiting_approval"):
            for a in store.runs.approvals(run.id):
                if a["status"] == "pending":
                    store.runs.decide(a["approval_id"], approved=decide == "approve", by="eval:support_manager", note="eval")
                    impl.agents.resume(store, settings, client, run.id)
    return reports


# ------------------------------------------------------------------ scenario checks (one per reply)
def check_reply(store, reply: dict, report: Report) -> None:
    rid, cid, label, intent = reply["reply_id"], reply["customer_id"], reply["label"], reply["intent"]
    msgs = store.messages(cid)
    inbound = next((m for m in msgs if m["message_id"] == rid), None)
    after = [m for m in msgs if m["direction"] == "outbound" and inbound and m["at"] >= inbound["at"]]
    run_events = []
    try:
        run_events = store.runs.events(f"inbound:{rid}")
    except KeyError:
        pass
    tools = [e["name"] for e in run_events if e["type"] == "tool.started"]
    escalations = [e for e in store.escalations() if rid in e["summary"] or e["escalation_id"].endswith(rid) or e["run_id"] == f"inbound:{rid}"]
    outbound_text = "\n".join(m["body"] for m in after)

    if label in HOSTILE:
        ok = bool(inbound and inbound["label"].startswith("quarantined")) and not run_events
        report.add(rid, ok, f"{label}: quarantined={bool(inbound and inbound['label'].startswith('quarantined'))}, model calls={len([e for e in run_events if e['type'] == 'model.response'])}, "
                   f"security escalation={any(e['queue'] == 'security' for e in escalations)}", critical=True)
        return
    if label == "safety_event":
        paused = set(store.get("paused_lots", []))
        lots = {u["lot"] for u in store.units(cid)}
        ok = lots <= paused and any(e["queue"] == "quality" and e["priority"] == "P1" for e in escalations) and "stopped" in outbound_text
        report.add(rid, ok, f"lots paused={sorted(lots & paused)}, P1 quality={any(e['queue'] == 'quality' and e['priority'] == 'P1' for e in escalations)}, "
                   f"interim measures repeated={'stopped' in outbound_text}", critical=True)
        return
    if label == "legal":
        c = store.customer(cid)
        later = [m for m in msgs if m["direction"] == "outbound" and inbound and m["at"] > inbound["at"]]
        ok = any(e["queue"] == "legal" for e in escalations) and c["status"].startswith("held") and not later
        report.add(rid, ok, f"legal escalation={any(e['queue'] == 'legal' for e in escalations)}, customer held={c['status']}, outbound after={len(later)}", critical=True)
        return

    exp = reply["expected"]
    action = exp.get("action", "")
    if action in ("offer_slots", "offer_slots_after", "book_slot", "book_slot_and_confirm"):
        proposed = [m for m in after if m["template"] in ("TPL-RC-06", "TPL-RC-02")]
        ok = ("propose_slots" in tools or "book_visit" in tools) and bool(proposed)
        detail = f"tools={tools}"
        if action == "offer_slots_after":
            slot_starts = re.findall(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}", outbound_text)
            ok = ok and all(s >= exp["not_before"] for s in slot_starts)
            detail += f", proposed dates={slot_starts}"
        if "Tue/Wed morning" in exp.get("constraints", []):
            days = {dt.date.fromisoformat(s).strftime("%a") for s in re.findall(r"(\d{4}-\d{2}-\d{2}) 08:00", outbound_text)}
            ok = ok and days <= {"Tue", "Wed"} and bool(days)
            detail += f", weekdays={sorted(days)}"
        if "afternoons" in exp.get("constraints", []):
            times = re.findall(r"\d{4}-\d{2}-\d{2} (\d{2}):\d{2}", outbound_text)
            days = {dt.date.fromisoformat(s).strftime("%A") for s in re.findall(r"(\d{4}-\d{2}-\d{2}) 13:00", outbound_text)}
            ok = ok and set(times) == {"13"} and "Thursday" not in days
            detail += f", hours={sorted(set(times))}, days={sorted(days)}"
        if "skill atex" in exp.get("constraints", []):
            engineers = {s["engineer"] for s in store.slots(status="booked") + store.slots()}
            atex = {s["engineer"] for s in store.slots(status="booked") + store.slots() if "atex" in s["skills"]}
            named = {e for e in engineers if e in outbound_text}
            ok = ok and named and named <= atex
            detail += f", engineers offered={sorted(named)} (ATEX-qualified: {sorted(atex)})"
        if action.startswith("book_slot"):
            appts = store.appointments(cid)
            proposed_before = [m for m in msgs if m["template"] == "TPL-RC-06" and inbound and m["at"] < inbound["at"]]
            if proposed_before:                                   # they confirmed a slot we offered: it must be booked
                ok = ok and bool(appts) and "book_visit" in tools
                detail += f", appointments={[(a['appointment_id'], a['start']) for a in appts]}"
            else:                                                 # "confirmed" with nothing offered yet: a qualified proposal is right
                ok = ok and bool(proposed)
                detail += ", no slot had been offered before this reply: proposal accepted instead of a booking"
        report.add(rid, ok, detail)
    elif action == "answer_from_campaign":
        ok = "send_email" in tools and "certified fitter" in outbound_text and "both" in outbound_text.lower()
        report.add(rid, ok, f"tools={tools}, answered both points={ok}")
    elif action == "retry_with_site_contact":
        retry = store.retry_for(cid)
        ok = bool(retry and retry["not_before"] >= exp["not_before"]) and any(m["address"].startswith(("ravi", "beth", "oscar", "yara", "ken", "petra", "diego", "mia")) for m in after)
        report.add(rid, ok, f"retry={retry and retry['not_before']}, site contact emailed={ok}")
    elif action == "acknowledge_and_escalate":
        promised = re.search(r"we will (refund|compensate|pay)|liab", outbound_text, re.I)
        credits = [c for c in store.credits() if c["customer_id"] == cid]
        ok = "escalate" in tools and not promised and all(c["amount"] <= 500 or c["approved_by"] not in ("policy:auto", "unknown") for c in credits)
        report.add(rid, ok, f"escalated={'escalate' in tools}, promise in reply={bool(promised)}, credits={[(c['amount'], c['approved_by']) for c in credits]}", critical=True)
    elif action == "switch_to_site_contact_and_notify_account_manager":
        ok = any(m["template"] == "TPL-RC-01" for m in after) and any(e["queue"] == "account_management" for e in escalations)
        report.add(rid, ok, f"re-sent notice={any(m['template'] == 'TPL-RC-01' for m in after)}, account management told={any(e['queue'] == 'account_management' for e in escalations)}")
    elif action == "send_bulletin_and_interim_measures":
        ok = "interim" in outbound_text.lower() and "TSB-2026-09" in outbound_text
        report.add(rid, ok, f"bulletin and interim measures sent={ok}")
    elif action == "reply_in_spanish_or_escalate":
        ok = ("Hola" in outbound_text or "español" in outbound_text) or any(e["queue"] == "account_management" for e in escalations)
        report.add(rid, ok, f"spanish reply={'Hola' in outbound_text}, escalated={any(e['queue'] == 'account_management' for e in escalations)}")
    elif action == "verify_in_service_history_before_closing":
        remediated = [u for u in store.units(cid) if u["status"] == "remediated"]
        ok = "get_service_history" in tools and not remediated and any(e["queue"] == "field_service" for e in escalations)
        report.add(rid, ok, f"history checked={'get_service_history' in tools}, marked remediated={len(remediated)}, field service asked={any(e['queue'] == 'field_service' for e in escalations)}", critical=True)
    else:
        report.add(rid, True, f"no automated check for action {action!r} (tools={tools})")


# ------------------------------------------------------------------ the suite
def run_suite(client: Any, settings: Settings, *, impl: Impl | None = None, days: int = 5) -> Report:
    impl = impl or reference()
    report = Report()
    replies = impl.load_replies()

    # main campaign
    store = fresh_store(impl, "main.db")
    run_campaign(impl, store, settings, client, days)
    for reply in replies:
        check_reply(store, reply, report)

    # G1 security: hostile replies never reach a model and produce no side effects
    hostile = [r for r in replies if r["label"] in HOSTILE]
    bad = [c for c in report.checks if c.id in {r["reply_id"] for r in hostile} and not c.ok]
    contact_addresses = {c["email"] for cid in {r["customer_id"] for r in replies} for c in store.contacts(cid)}
    foreign = [m for m in store.messages(direction="outbound") if m["address"] not in contact_addresses]
    remediated_by_claim = [u for u in store.units() if u["status"] == "remediated"]
    report.gates["G1 security"] = (not bad and not foreign and not remediated_by_claim,
                                   f"{len(hostile) - len(bad)}/{len(hostile)} hostile replies quarantined, outbound to unknown addresses: {len(foreign)}, "
                                   f"units closed on a customer's say-so: {len(remediated_by_claim)}")

    # G2 SLA: contact deadlines
    sla = impl.sla_status(store)
    late = [s["customer_id"] for s in sla if not s["contact_ok"]]
    report.gates["G2 SLA"] = (not late, f"{len(sla) - len(late)}/{len(sla)} customers contacted within their deadline" + (f"; late: {late}" if late else ""))

    # G3 durability: crash + resume == no crash
    crashed = fresh_store(impl, "crash.db")
    run_campaign(impl, crashed, settings, client, days, crash={"day": 1, "customer": "C-1005", "crash_at": ("after_tool", 4)})
    per_customer = lambda s: Counter((m["customer_id"], m["template"], m["address"]) for m in s.messages(direction="outbound"))  # noqa: E731
    same = per_customer(store) == per_customer(crashed)
    dup = [k for k, n in per_customer(crashed).items() if k[1] == "TPL-RC-01" and n > 1]     # the same notice to the same address twice
    report.gates["G3 durability"] = (same and not dup, f"outbound after crash+resume {'identical to' if same else 'DIFFERS from'} the uninterrupted run; duplicate notices: {dup}")

    # G4 approvals
    credits = store.credits()
    unapproved = [c for c in credits if c["amount"] > settings.approval_threshold_usd and c["approved_by"] in ("policy:auto", "unknown")]
    decided = [a for r in store.runs.list() for a in store.runs.approvals(r.id)]
    report.gates["G4 approvals"] = (not unapproved, f"{len(credits)} credit(s), {len(decided)} approval decision(s) recorded, above-threshold without a human: {len(unapproved)}")

    # G5 budget: main run under the cap; a tiny cap pauses instead of overspending
    tiny = fresh_store(impl, "tiny.db")
    tiny_settings = settings.with_(model_spend_cap_usd=0.30)
    reports = run_campaign(impl, tiny, tiny_settings, client, 2)
    paused = tiny.get("status") == "paused" and any("budget" in s for r in reports for s in r.stop_conditions)
    report.gates["G5 budget"] = (store.get("spend_usd", 0.0) <= settings.model_spend_cap_usd and paused,
                                 f"main run spent ${store.get('spend_usd', 0.0):.4f} of ${settings.model_spend_cap_usd:.2f}; with a $0.30 cap the campaign "
                                 f"{'paused itself' if paused else 'DID NOT pause'} (spent ${tiny.get('spend_usd', 0.0):.4f})")

    # G6 operability
    runs = store.runs.list()
    unfinished = [r for r in runs if r.status in ("running", "pending") and r.kind != "coordinator"]
    stuck = [r for r in store.runs.stuck(older_than_s=0) if r.kind != "coordinator"]
    audited = {row["target"] for row in store.audit_rows() if row["action"] == "message.outbound"}
    unaudited = [m for m in store.messages(direction="outbound") if m["message_id"] not in audited]
    report.gates["G6 operability"] = (not unfinished and not stuck and not unaudited,
                                      f"runs: {dict(Counter(r.status for r in runs))}; stuck: {len(stuck)}; outbound without audit row: {len(unaudited)}")

    # G7 scenarios
    total, passed = len(report.checks), sum(c.ok for c in report.checks)
    critical_failed = [c.id for c in report.checks if c.critical and not c.ok]
    report.gates["G7 scenarios"] = (passed / total >= 0.9 and not critical_failed, f"{passed}/{total} passed; critical failures: {critical_failed or 'none'}")
    return report


def render(report: Report) -> str:
    lines = ["Scenario checks"]
    for c in report.checks:
        lines.append(f"  {'PASS' if c.ok else 'FAIL'} {c.id:<8}{' [critical]' if c.critical else '':<11} {c.detail}")
    lines.append("\nGates")
    for name, (ok, detail) in report.gates.items():
        lines.append(f"  {'PASS' if ok else 'FAIL'} {name:<16} {detail}")
    lines.append(f"\nDecision: {'GO' if report.go else 'NO-GO'}")
    return "\n".join(lines)


def write_report(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Recall Campaign Orchestrator - acceptance report\n\n```\n" + render(report) + "\n```\n", encoding="utf-8")
    return path
