"""The durable coordinator: plans the campaign, dispatches agent runs, processes replies, sends reminders, and
enforces the stop conditions and the budget - one business day at a time.

Everything it does is resumable: the plan is recomputed from the store, every agent run has a deterministic id
and is idempotent to re-dispatch, replies are keyed by their id, reminders by customer and count.  Kill the
process anywhere and call `run_day` again: completed work is skipped, interrupted runs are resumed, nothing is
sent twice.  The coordinator itself is a run in the same store, so its day-by-day decisions are in the log.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from advanced.lib.durable import Crash

from . import agents, security
from .budget import BudgetExceeded, SwarmBudget
from .config import CAMPAIGN_START, RECALL_DATA, Settings, add_business_days, business_days_between
from .store import CampaignStore
from .tools import CampaignDesk

RISK_ORDER = {"safety": 0, "production": 1, "standard": 2}
TIER_ORDER = {"strategic": 0, "key": 1, "standard": 2}
PER_RUN_RESERVE_USD = 0.60           # what one run may cost at most (max_turns x max_tokens at list price, rounded up)


@dataclass
class DayReport:
    day: int
    date: str
    outreach: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    replies: list[dict] = field(default_factory=list)
    reminders: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    approvals_pending: list[str] = field(default_factory=list)
    guard_trips: list[dict] = field(default_factory=list)
    spend_usd: float = 0.0
    paused: bool = False


def day_date(day: int) -> dt.date:
    return add_business_days(CAMPAIGN_START, day - 1)


def load_replies(path=RECALL_DATA / "inbound_replies.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8")]


def sla_status(store: CampaignStore) -> list[dict]:
    rules = {r["risk_class"]: r for r in store.campaign()["priority_rules"]}
    out = []
    for c in store.customers():
        rule = rules[c["risk_class"]]
        contact_deadline = add_business_days(CAMPAIGN_START, rule["contact_within_business_days"])
        remedy_deadline = add_business_days(CAMPAIGN_START, rule["remedy_within_business_days"])
        contacted = dt.date.fromisoformat(c["first_contact_at"][:10]) if c["first_contact_at"] else None
        units = store.units(c["customer_id"])
        scheduled = [u for u in units if u["status"] in ("scheduled", "remediated")]
        appts = store.appointments(c["customer_id"])
        latest_visit = max((a["start"][:10] for a in appts), default=None)
        out.append({"customer_id": c["customer_id"], "risk_class": c["risk_class"], "status": c["status"],
                    "contact_deadline": contact_deadline.isoformat(), "contacted": contacted.isoformat() if contacted else None,
                    "contact_ok": bool(contacted and contacted <= contact_deadline),
                    "contact_overdue": not contacted and store.today > contact_deadline,
                    "remedy_deadline": remedy_deadline.isoformat(), "units_scheduled": f"{len(scheduled)}/{len(units)}",
                    "visit": latest_visit, "remedy_ok": bool(scheduled) and len(scheduled) == len(units) and (latest_visit or "") <= remedy_deadline.isoformat(),
                    "hold": c["hold_reason"] or ""})
    return out


class Orchestrator:
    def __init__(self, store: CampaignStore, settings: Settings, client: Any, replies: list[dict] | None = None,
                 budget: SwarmBudget | None = None) -> None:
        self.store, self.settings, self.client = store, settings, client
        self.replies = replies if replies is not None else load_replies()
        self.budget = budget or SwarmBudget(settings.model_spend_cap_usd, carried_usd=store.get("spend_usd", 0.0))
        self.desk = CampaignDesk(store, settings, role="orchestrator", customer_id=None, actor="orchestrator")
        if not any(r.id == "coordinator" for r in store.runs.list(kind="coordinator")):
            store.runs.create("coordinator", input={"message": "campaign RC-2026-03"}, run_id="coordinator")
            store.runs.set_status("coordinator", "running")           # the coordinator is a long-lived run: its log is the campaign diary

    # ------------------------------------------------------------------ planning
    def plan(self) -> list[dict]:
        """Customers still to contact, most urgent first: risk class, then tier, then customer id (stable)."""
        todo = []
        for c in self.store.customers(status="pending"):
            retry = self.store.retry_for(c["customer_id"])
            if retry and retry["not_before"] > self.store.today.isoformat():
                continue
            todo.append(c)
        return sorted(todo, key=lambda c: (RISK_ORDER[c["risk_class"]], TIER_ORDER[c["tier"]], c["customer_id"]))

    # ------------------------------------------------------------------ one business day
    def run_day(self, day: int, *, crash: dict | None = None) -> DayReport:
        date = day_date(day)
        self.store.set("today", date.isoformat())
        report = DayReport(day=day, date=date.isoformat())
        self.store.runs.append("coordinator", "campaign.day_started", {"day": day, "date": date.isoformat()})
        if self.store.get("status") == "paused":
            report.paused = True
            report.stop_conditions.append("campaign paused: nothing dispatched")
            return self._finish(report)

        # 1. anything a crashed worker left behind is resumed first (leases have expired by now)
        for run in self.store.runs.list():
            if run.kind in ("outreach", "inbound") and run.status in ("running", "pending"):
                outcome = agents.resume(self.store, self.settings, self.client, run.id)
                report.resumed.append(f"{run.id} -> {outcome.status}")

        # 2. outreach, most urgent customers first, inside the budget
        for i, c in enumerate(self.plan()):
            if not self._budget_ok(report):
                break
            crash_at = crash.get("crash_at") if crash and crash.get("customer") == c["customer_id"] else None
            outcome, guard = agents.run_outreach(self.store, self.settings, self.client, c["customer_id"], crash_at=crash_at)
            report.outreach.append(f"{c['customer_id']} -> {outcome.status}")
            report.guard_trips.extend(guard.tripped)

        # 3. replies received up to today, in order; each is screened, logged, and either quarantined or handled by the agent
        for reply in sorted(self.replies, key=lambda r: (r["received_at"], r["reply_id"])):
            if reply["received_at"][:10] > date.isoformat():
                continue
            run_id = f"inbound:{reply['reply_id']}"
            try:
                if self.store.runs.get(run_id).status in ("completed", "failed", "cancelled"):
                    continue
            except KeyError:
                pass
            if any(m["message_id"] == reply["reply_id"] and m["label"].startswith("quarantined") for m in self.store.messages(reply["customer_id"], "inbound")):
                continue
            if not self._budget_ok(report):
                break
            report.replies.append(self._handle_reply(reply, report))

        # 4. reminders for customers who have not answered
        for c in self.store.customers(status="contacted"):
            last_out = c["last_outbound_at"][:10] if c["last_outbound_at"] else None
            if c["last_inbound_at"] or not last_out or c["reminders"] >= 2 or self.store.retry_for(c["customer_id"]):
                continue
            if business_days_between(dt.date.fromisoformat(last_out), date) >= self.settings.reminder_after_business_days:
                self._send_reminder(c)
                report.reminders.append(c["customer_id"])

        # 5. stop conditions that depend on the day's outcome
        self._stop_conditions(report)
        report.approvals_pending = [r.id for r in self.store.runs.list(status="waiting_approval")]
        return self._finish(report)

    def _finish(self, report: DayReport) -> DayReport:
        report.spend_usd = round(self.budget.checkpoint(), 4)
        self.store.set("spend_usd", report.spend_usd)
        self.store.runs.append("coordinator", "campaign.day_finished", {"day": report.day, "outreach": report.outreach, "replies": len(report.replies),
                                                                        "reminders": report.reminders, "stop_conditions": report.stop_conditions,
                                                                        "spend_usd": report.spend_usd})
        return report

    def _budget_ok(self, report: DayReport) -> bool:
        try:
            self.budget.check(reserve_usd=PER_RUN_RESERVE_USD)
            return True
        except BudgetExceeded as exc:
            if "budget" not in " ".join(report.stop_conditions):
                self.store.set("status", "paused")
                self.store.audit("orchestrator", "campaign.paused", "all", {"reason": str(exc)})
                report.stop_conditions.append(f"budget: {exc}")
                self.desk.execute("escalate", {"queue": "quality", "priority": "P2", "summary": f"Campaign paused: {exc}"})
            report.paused = True
            return False

    # ------------------------------------------------------------------ replies
    def _handle_reply(self, reply: dict, report: DayReport) -> dict:
        cid = reply["customer_id"]
        contacts = self.store.contacts(cid)
        screen = security.screen_inbound(reply, contacts)
        self.store.record_message(reply["reply_id"], direction="inbound", customer_id=cid, address=reply["from"], template=None,
                                  subject=reply["subject"], body=reply["body"], label=screen.label, actor="security")
        entry = {"reply_id": reply["reply_id"], "customer_id": cid, "label": screen.label, "flags": screen.flags, "reasons": screen.reasons}
        if screen.quarantine:
            kinds = screen.label.split(":", 1)[1]
            priority = "P2" if any(k in kinds for k in ("fraud_redirect", "phishing", "spoofed_sender", "spoofed_internal")) else "P3"
            esc = self.store.escalate(f"esc_q_{reply['reply_id']}", customer_id=cid, queue="security", priority=priority,
                                      summary=f"Quarantined reply {reply['reply_id']} ({kinds}): " + "; ".join(screen.reasons), actor="security")
            entry.update({"action": "quarantined", "escalation": esc["escalation_id"], "model_calls": 0})
            return entry
        if "safety_event" in screen.flags:                                       # a stop condition, in code, before any model call
            lots = sorted({u["lot"] for u in self.store.units(cid)})
            for lot in lots:
                self.desk.execute("pause_campaign", {"scope": f"lot:{lot}", "reason": f"{reply['reply_id']}: injury / fluid release reported by {cid}"})
            self.store.escalate(f"esc_s_{reply['reply_id']}", customer_id=cid, queue="quality", priority="P1",
                                summary=f"STOP: {cid} reports an injury or fluid release ({reply['reply_id']}); lots {lots} paused", actor="orchestrator")
            report.stop_conditions.append(f"safety event from {cid}: paused lots {lots}")
        customer = self.store.customer(cid)
        if customer["status"].startswith("held"):
            esc = self.store.escalate(f"esc_h_{reply['reply_id']}", customer_id=cid, queue="legal", priority="P3",
                                      summary=f"Reply {reply['reply_id']} received while the customer is on hold ({customer['hold_reason']})", actor="orchestrator")
            entry.update({"action": "routed_to_hold_owner", "escalation": esc["escalation_id"], "model_calls": 0})
            return entry
        calls_before = self._calls()
        outcome, guard = agents.run_inbound(self.store, self.settings, self.client, reply, screen)
        entry.update({"action": "agent", "run": outcome.run_id, "status": outcome.status, "summary": outcome.reply,
                      "tools": [c["name"] for c in guard_calls(guard)], "model_calls": self._calls() - calls_before})
        report.guard_trips.extend(guard.tripped)
        if "legal" in screen.flags:
            self.store.set_customer_status(cid, "held:legal", reason="communications through counsel", actor="orchestrator")
        if outcome.status == "waiting_approval":
            entry["approval_id"] = outcome.approval_id
        return entry

    def _calls(self) -> int:
        from labkit import LEDGER
        return LEDGER.total_calls

    # ------------------------------------------------------------------ reminders and stop conditions
    def _send_reminder(self, customer: dict) -> None:
        cid = customer["customer_id"]
        n = customer["reminders"] + 1
        contact = next((c for c in self.store.contacts(cid) if c["active"] and c["role"] == "purchasing"), None) or self.store.contacts(cid)[0]
        units = ", ".join(u["serial"] for u in self.store.units(cid))
        body = (f"Hello {contact['name']},\n\nA reminder about recall RC-2026-03: units {units} at your site still need the free replacement "
                f"visit. Please reply with dates when an engineer can access them, or tell us who to coordinate with.\n\nKestrel recall team")
        message_id = f"msg_reminder_{cid}_{n}"
        if self.store.record_message(message_id, direction="outbound", customer_id=cid, address=contact["email"], template="TPL-RC-03",
                                     subject=f"Reminder {n}: recall RC-2026-03", body=body, held=not self.settings.auto_send, actor="orchestrator"):
            self.store.conn().execute("UPDATE customers SET reminders = ? WHERE customer_id = ?", (n, cid))

    def _stop_conditions(self, report: DayReport) -> None:
        stock = {}
        for p in self.store.parts():
            stock[p["sku"]] = stock.get(p["sku"], 0) + p["on_hand"] - p["reserved"]
        for kit in self.store.get("kits", []):
            remedy = kit["for_remedy"]
            remaining = [u for u in self.store.units() if u["remedy"] == remedy and u["status"] in ("pending", "contacted")]
            if stock.get(kit["sku"], 0) == 0 and remaining and remedy not in self.store.get("paused_remedies", []):
                self.desk.execute("pause_campaign", {"scope": f"remedy:{remedy}", "reason": f"{kit['sku']} stock is zero with {len(remaining)} units left"})
                self.desk.execute("escalate", {"queue": "field_service", "priority": "P2",
                                               "summary": f"{kit['sku']} out of stock; {len(remaining)} units of {remedy} cannot be scheduled"})
                report.stop_conditions.append(f"parts: {kit['sku']} exhausted, {remedy} paused")


def guard_calls(guard) -> list[dict]:
    execute = guard.execute
    desk = getattr(execute, "__self__", None)
    return desk.calls if desk is not None else []


def run_days(orchestrator: Orchestrator, days: int, *, crash: dict | None = None) -> list[DayReport]:
    """Run the campaign for `days` business days; a simulated crash aborts that day, and the next call resumes it."""
    reports = []
    for day in range(1, days + 1):
        try:
            reports.append(orchestrator.run_day(day, crash=crash if crash and crash.get("day") == day else None))
        except Crash:
            raise
    return reports
