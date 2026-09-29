"""The campaign toolset and its executor.

Every tool the agents can call is defined here with a strict schema and `meta` (risk, pii, approval), the same
shape as the 120-tool catalog.  `CampaignDesk` executes them with the guarantees the orchestrator relies on:

  * **capability scoping** - a desk is created for one role and one customer; tools outside the role's list are
    refused before they run, and every row read or written is filtered to that customer (a run for Harbor Foods
    cannot read Cobalt Chemical's contacts, whatever the model asks);
  * **recipient allow-list** - `send_email` only delivers to that customer's active contacts on file;
  * **idempotent effects** - every write is keyed by the run and the tool_use id (`ToolContext.idempotency_key`),
    so a replayed or retried step never sends twice, books twice or credits twice;
  * **approval gates** - a goodwill credit above the threshold raises `ApprovalRequired`, which parks the run
    until a support manager decides from `run_ops.py`;
  * **audit** - every call lands in the store's audit table with the run and the actor.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from advanced.lib.durable import ApprovalRequired, ToolContext

from . import security
from .config import ROLE_TOOLS, Settings
from .store import CampaignStore

SKILL_FOR_REMEDY = {"seal_kit_replacement": "seal_replacement", "controller_board_replacement": "controller_firmware"}
WAREHOUSE_FOR_REGION = {"US-EAST": "WH-EAST", "US-WEST": "WH-WEST", "EU": "WH-EU", "APAC": "WH-EU"}
TEMPLATES = {"TPL-RC-01": "initial notice", "TPL-RC-02": "schedule confirmation", "TPL-RC-03": "reminder", "TPL-RC-04": "completion",
             "TPL-RC-05": "answer to a question", "TPL-RC-06": "slot proposal"}


class ToolError(Exception):
    pass


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None, *, risk: str = "read",
          pii: bool = False, approval: bool = False) -> dict:
    required = list(properties) if required is None else required
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            "meta": {"risk": risk, "side_effect": risk != "read", "pii": pii, "approval_required": approval}}


S = {"type": "string"}
TOOLS: list[dict] = [
    _tool("get_campaign_brief", "The recall notice: hazard, remedy and interim measures per lot, SLAs, approved templates.", {}),
    _tool("get_contacts", "Active contacts on file for the customer this run is scoped to (the only addresses send_email may use).",
          {"customer_id": {**S, "description": "Customer ID; must be this run's customer."}}, pii=True),
    _tool("list_units", "Affected units of this customer with their remedy, risk class and status.",
          {"customer_id": {**S, "description": "Customer ID; must be this run's customer."}}),
    _tool("get_thread", "Outbound and inbound messages exchanged with this customer so far.",
          {"customer_id": {**S, "description": "Customer ID; must be this run's customer."}}),
    _tool("get_engineer_slots", "Free field-engineer slots matching the region and skill the remedy needs.",
          {"remedy": {**S, "description": "seal_kit_replacement or controller_board_replacement."},
           "extra_skill": {**S, "description": "An additional required skill, e.g. atex for Zone 1/2 sites; empty if none."},
           "not_before": {**S, "description": "ISO date; empty for any."}, "not_after": {**S, "description": "ISO date; empty for any."},
           "afternoons_only": {"type": "boolean", "description": "Only slots starting at 13:00."},
           "mornings_only": {"type": "boolean", "description": "Only slots starting at 08:00."},
           "exclude_weekday": {**S, "description": "Weekday name to exclude (e.g. Thursday) or empty."}},
          ["remedy"]),
    _tool("get_parts_stock", "Stock of the remedy kits per warehouse.", {"remedy": {**S, "description": "The remedy."}}),
    _tool("get_service_history", "Service visits recorded for a serial (booked or completed appointments).", {"serial": S}),
    _tool("send_email", "Send an email to one of this customer's contacts on file. Cannot be unsent.",
          {"contact_id": S, "template": {**S, "description": "TPL-RC-01 notice, -02 confirmation, -03 reminder, -04 completion, -05 answer, -06 slots."},
           "subject": S, "body": {**S, "description": "Plain text. Never change the hazard wording from the brief; never mention other customers."}},
          risk="irreversible", pii=True),
    _tool("propose_slots", "Email the contact a choice of up to three engineer slots for the visit.",
          {"contact_id": S, "slot_ids": {"type": "array", "items": S}, "note": {**S, "description": "One sentence of context for the customer."}},
          risk="irreversible", pii=True),
    _tool("book_visit", "Book an engineer slot for these serials; reserves the parts. Fails if the slot is gone or parts are short.",
          {"slot_id": S, "serials": {"type": "array", "items": S}, "contact_id": {**S, "description": "Contact to send the confirmation to."}},
          risk="write"),
    _tool("request_goodwill_credit", "Offer a goodwill credit. Above the approval threshold the run pauses for a support manager.",
          {"amount_usd": {"type": "number"}, "reason": S}, risk="write", approval=True),
    _tool("schedule_retry", "Try this customer again on a later date (out of office, busy period).",
          {"not_before": {**S, "description": "ISO date."}, "reason": S}, risk="write"),
    _tool("escalate", "Hand this customer to a human queue with a priority and a summary.",
          {"queue": {**S, "description": "quality, field_service, account_management, legal, security."},
           "priority": {**S, "description": "P1..P4."}, "summary": S}, risk="write"),
    _tool("mark_remediated", "Record a unit as remediated. Requires evidence: a completed appointment ID.",
          {"serial": S, "evidence": {**S, "description": "Appointment ID or service record that proves the remedy was applied."}}, risk="write"),
    _tool("get_sla_status", "Days used against the contact and remedy SLAs per customer (orchestrator only).", {}),
    _tool("pause_campaign", "Pause the campaign for a lot or a remedy (a stop condition fired).",
          {"scope": {**S, "description": "lot:<lot> | remedy:<remedy> | all"}, "reason": S}, risk="write", approval=True),
]
TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def tools_for(role: str) -> list[dict]:
    names = ROLE_TOOLS[role]
    return [{k: v for k, v in TOOL_BY_NAME[n].items() if k != "meta"} for n in names]


class CampaignDesk:
    """Executes campaign tools for one role, scoped to one customer, inside one durable run."""

    def __init__(self, store: CampaignStore, settings: Settings, *, role: str, customer_id: str | None, actor: str,
                 run_id: str = "") -> None:
        self.store, self.settings, self.role, self.customer_id, self.actor, self.run_id = store, settings, role, customer_id, actor, run_id
        self.allowed = set(ROLE_TOOLS[role])
        self.calls: list[dict] = []

    # ------------------------------------------------------------------ the DurableRunner executor
    def execute(self, name: str, tool_input: dict, ctx: ToolContext | None = None) -> Any:
        key = ctx.idempotency_key if ctx else f"{self.run_id}:{name}:{len(self.calls)}"
        try:
            if name not in self.allowed:
                raise ToolError(f"{name} is not available to the {self.role} role")
            handler = getattr(self, f"_{name}")
            result = handler(tool_input, key=key, ctx=ctx)
            self.calls.append({"name": name, "input": tool_input, "is_error": False})
            return result
        except ApprovalRequired:
            self.calls.append({"name": name, "input": tool_input, "is_error": False, "paused": True})
            raise
        except (ToolError, ValueError, KeyError, TypeError) as exc:
            self.calls.append({"name": name, "input": tool_input, "is_error": True})
            self.store.audit(self.actor, "tool.refused", name, {"input": tool_input, "error": str(exc)})
            return {"error": str(exc)}

    def run(self, name: str, tool_input: dict, ctx: ToolContext | None = None) -> tuple[str, bool]:
        result = self.execute(name, tool_input, ctx)
        return json.dumps(result, default=str), isinstance(result, dict) and "error" in result

    # ------------------------------------------------------------------ scoping helpers
    def _scope(self, customer_id: str | None) -> str:
        if self.customer_id is None:                    # the orchestrator's desk is not customer-scoped
            if not customer_id:
                raise ToolError("customer_id is required")
            return customer_id
        if customer_id and customer_id != self.customer_id:
            raise ToolError(f"this run is scoped to {self.customer_id}; {customer_id} is out of scope")
        return self.customer_id

    def _customer(self) -> dict:
        c = self.store.customer(self._scope(None))
        if c is None:
            raise ToolError("unknown customer")
        return c

    def _contact(self, contact_id: str) -> dict:
        for c in self.store.contacts(self._scope(None)):
            if c["contact_id"] == contact_id and c["active"]:
                return c
        raise ToolError(f"{contact_id} is not an active contact of {self.customer_id}; send_email only delivers to contacts on file")

    def _id(self, prefix: str, key: str) -> str:
        """A stable id derived from the idempotency key: the same step always produces the same id."""
        return prefix + "_" + hashlib.sha1(key.encode()).hexdigest()[:12]

    # ------------------------------------------------------------------ reads
    def _get_campaign_brief(self, args: dict, *, key: str, ctx: Any) -> dict:
        c = self.store.campaign()
        return {"recall_id": c["recall_id"], "issued": c["issued"], "lots": c["lots"], "priority_rules": c["priority_rules"],
                "templates": TEMPLATES, "interim_measures_required_in_every_notice": True,
                "rules": ["never change the hazard wording", "never name other customers", "no compensation promises",
                          "goodwill credits above $%.0f need approval" % self.settings.approval_threshold_usd]}

    def _get_contacts(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(args.get("customer_id"))
        return {"customer_id": cid, "contacts": [{k: c[k] for k in ("contact_id", "name", "email", "role", "channel", "note")}
                                                 for c in self.store.contacts(cid) if c["active"]]}

    def _list_units(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(args.get("customer_id"))
        return {"customer_id": cid, "units": [{k: u[k] for k in ("serial", "sku", "lot", "remedy", "risk_class", "status", "site_id")}
                                              for u in self.store.units(cid)]}

    def _get_thread(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(args.get("customer_id"))
        return {"messages": [{k: m[k] for k in ("message_id", "direction", "address", "template", "subject", "at", "label")}
                             for m in self.store.messages(cid)]}

    def _get_engineer_slots(self, args: dict, *, key: str, ctx: Any) -> dict:
        customer = self._customer()
        skill = SKILL_FOR_REMEDY.get(args["remedy"])
        if skill is None:
            raise ToolError(f"unknown remedy {args['remedy']!r}")
        slots = self.store.slots(region=customer["region"], skill=skill, not_before=args.get("not_before") or None,
                                 not_after=args.get("not_after") or None)
        if args.get("extra_skill"):
            slots = [s for s in slots if args["extra_skill"] in s["skills"]]
        if args.get("afternoons_only"):
            slots = [s for s in slots if s["start"][11:13] == "13"]
        if args.get("mornings_only"):
            slots = [s for s in slots if s["start"][11:13] == "08"]
        if args.get("exclude_weekday"):
            import datetime as dt
            slots = [s for s in slots if dt.date.fromisoformat(s["start"][:10]).strftime("%A") != args["exclude_weekday"].title()]
        return {"region": customer["region"], "skill": skill, "slots": [{k: s[k] for k in ("slot_id", "engineer", "engineer_id", "start", "end", "skills")}
                                                                         for s in slots[:8]]}

    def _get_parts_stock(self, args: dict, *, key: str, ctx: Any) -> dict:
        kits = [k for k in self.store.get("kits", []) if k["for_remedy"] == args["remedy"]]
        stock = {(p["sku"], p["warehouse"]): p for p in self.store.parts()}
        return {"kits": [{"sku": k["sku"], "fits": k["fits"], "available": {wh: stock[(k["sku"], wh)]["on_hand"] - stock[(k["sku"], wh)]["reserved"]
                                                                              for wh in k["stock"]}} for k in kits]}

    def _get_service_history(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(None) if self.customer_id else None
        visits = [a for a in self.store.appointments(cid) if args["serial"] in a["serials"]]
        return {"serial": args["serial"], "visits": [{k: a[k] for k in ("appointment_id", "start", "engineer_id", "status", "remedy")} for a in visits],
                "note": "Only appointments booked through this campaign are known here; other claims need field-service verification."}

    # ------------------------------------------------------------------ writes
    def _deliver(self, key: str, contact: dict, template: str, subject: str, body: str, *, ctx: Any) -> dict:
        if template not in TEMPLATES:
            raise ToolError(f"unknown template {template!r}")
        cid = self._scope(None)
        clean, issues = security.sanitize_outbound(body, customer=self.store.customer(cid), other_customers=[c["name"] for c in self.store.customers()
                                                                                                             if c["customer_id"] != cid])
        blocking = [i for i in issues if i["severity"] == "block"]
        if blocking:
            raise ToolError("outbound blocked by the output guard: " + "; ".join(i["detail"] for i in blocking))
        message_id = self._id("msg", key)
        held = not self.settings.auto_send
        if ctx is not None:
            with ctx.effect(f"{key}:send") as eff:
                if eff.done:
                    return eff.stored
                self.store.record_message(message_id, direction="outbound", customer_id=cid, address=contact["email"], template=template,
                                          subject=subject, body=clean, run_id=self.run_id, held=held, actor=self.actor)
                return eff.commit({"message_id": message_id, "to": contact["email"], "template": template, "held_for_review": held,
                                   "sanitised": [i["detail"] for i in issues]})
        self.store.record_message(message_id, direction="outbound", customer_id=cid, address=contact["email"], template=template, subject=subject,
                                  body=clean, run_id=self.run_id, held=held, actor=self.actor)
        return {"message_id": message_id, "to": contact["email"], "template": template, "held_for_review": held, "sanitised": [i["detail"] for i in issues]}

    def _send_email(self, args: dict, *, key: str, ctx: Any) -> dict:
        return self._deliver(key, self._contact(args["contact_id"]), args["template"], args["subject"], args["body"], ctx=ctx)

    def _propose_slots(self, args: dict, *, key: str, ctx: Any) -> dict:
        contact = self._contact(args["contact_id"])
        slot_ids = list(args.get("slot_ids") or [])[:3]
        if not slot_ids:
            raise ToolError("propose_slots needs at least one slot_id")
        slots = {s["slot_id"]: s for s in self.store.slots()}
        missing = [s for s in slot_ids if s not in slots]
        if missing:
            raise ToolError(f"slots no longer free: {missing}")
        lines = [f"  {i + 1}. {slots[s]['start'][:16].replace('T', ' ')} UTC with {slots[s]['engineer']}" for i, s in enumerate(slot_ids)]
        body = (f"Hello {contact['name']},\n\n{args.get('note', '').strip()}\nWe can offer these visit slots:\n" + "\n".join(lines) +
                "\n\nReply with the number that suits you and we will confirm the engineer's name for your gate pass.\n\nKestrel recall team")
        self.store.set("proposed:" + self._scope(None), slot_ids)
        result = self._deliver(key, contact, "TPL-RC-06", f"Recall RC-2026-03 - visit slots for {self._customer()['name']}", body, ctx=ctx)
        return {**result, "proposed": slot_ids}

    def _book_visit(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(None)
        customer = self._customer()
        units = {u["serial"]: u for u in self.store.units(cid)}
        serials = list(args.get("serials") or [])
        unknown = [s for s in serials if s not in units]
        if unknown or not serials:
            raise ToolError(f"serials must be this customer's affected units; unknown: {unknown or 'none given'}")
        remedies = {units[s]["remedy"] for s in serials}
        if len(remedies) != 1:
            raise ToolError("one visit covers one remedy; book seal and controller work separately")
        remedy = remedies.pop()
        if remedy in self.store.get("paused_remedies", []) or units[serials[0]]["lot"] in self.store.get("paused_lots", []):
            raise ToolError(f"the campaign is paused for {remedy}: scheduling is on hold")
        slot = next((s for s in self.store.slots() if s["slot_id"] == args["slot_id"]), None)
        if slot is None:
            raise ToolError(f"slot {args['slot_id']} is not free")
        if slot["region"] != customer["region"]:
            raise ToolError(f"slot is in region {slot['region']}, the site is in {customer['region']} (out-of-region visits need approval)")
        if SKILL_FOR_REMEDY[remedy] not in slot["skills"]:
            raise ToolError(f"engineer {slot['engineer']} lacks the skill {SKILL_FOR_REMEDY[remedy]}")
        kit = next(k for k in self.store.get("kits") if k["for_remedy"] == remedy and units[serials[0]]["sku"] in k["fits"])
        appointment_id = self._id("apt", key)
        warehouse = self._warehouse_with(kit["sku"], len(serials), preferred=WAREHOUSE_FOR_REGION[customer["region"]])
        if warehouse is None:
            raise ToolError(f"parts: no warehouse has {len(serials)} x {kit['sku']} available; the orchestrator must pause the remedy or expedite stock")
        self.store.reserve(f"{appointment_id}:parts", sku=kit["sku"], warehouse=warehouse, qty=len(serials), actor=self.actor)
        booked = self.store.book(appointment_id, customer_id=cid, slot_id=slot["slot_id"], serials=serials, remedy=remedy,
                                 run_id=self.run_id, actor=self.actor)
        contact = self._contact(args["contact_id"])
        body = (f"Hello {contact['name']},\n\nConfirmed: {booked['engineer']} will visit on {booked['start'][:16].replace('T', ' ')} UTC to apply the "
                f"{remedy.replace('_', ' ')} on {', '.join(serials)}. Parts ({kit['sku']} x {len(serials)}) are reserved from {warehouse}.\n"
                f"Reference: {appointment_id}.\n\nKestrel recall team")
        self._deliver(key, contact, "TPL-RC-02", f"Recall RC-2026-03 - visit confirmed ({appointment_id})", body, ctx=ctx)
        return {"appointment_id": appointment_id, "engineer": booked["engineer"], "start": booked["start"], "serials": serials,
                "parts": {"sku": kit["sku"], "warehouse": warehouse, "qty": len(serials)}}

    def _warehouse_with(self, sku: str, qty: int, *, preferred: str) -> str | None:
        """The regional warehouse if it has the parts, else any warehouse that does (an inter-site transfer)."""
        stock = {p["warehouse"]: p["on_hand"] - p["reserved"] for p in self.store.parts() if p["sku"] == sku}
        if stock.get(preferred, 0) >= qty:
            return preferred
        return next((wh for wh, n in sorted(stock.items()) if n >= qty), None)

    def _request_goodwill_credit(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(None)
        amount = float(args["amount_usd"])
        if amount <= 0:
            raise ToolError("amount must be positive")
        if amount > self.settings.approval_threshold_usd and not args.get("_approved"):
            raise ApprovalRequired({"summary": f"goodwill credit of ${amount:,.2f} for {cid}: {args['reason']}", "amount_usd": amount,
                                    "approver_role": "support_manager"})
        approver = "policy:auto"
        if args.get("_approved"):
            decided = [a for a in self.store.runs.approvals(self.run_id) if a["status"] == "approved"]
            approver = decided[-1]["decided_by"] if decided else "unknown"
        return self.store.credit(self._id("cr", key), customer_id=cid, amount=amount, reason=args["reason"], approved_by=approver, actor=self.actor)

    def _schedule_retry(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(None)
        self.store.schedule_retry(cid, args["not_before"], args["reason"], actor=self.actor)
        return {"customer_id": cid, "not_before": args["not_before"]}

    def _escalate(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(None) if self.customer_id else (args.get("customer_id") or "campaign")
        if args["queue"] not in ("quality", "field_service", "account_management", "legal", "security"):
            raise ToolError(f"unknown queue {args['queue']!r}")
        return self.store.escalate(self._id("esc", key), customer_id=cid, queue=args["queue"], priority=args["priority"], summary=args["summary"],
                                   run_id=self.run_id, actor=self.actor)

    def _mark_remediated(self, args: dict, *, key: str, ctx: Any) -> dict:
        cid = self._scope(None)
        units = {u["serial"]: u for u in self.store.units(cid)}
        if args["serial"] not in units:
            raise ToolError(f"{args['serial']} is not one of this customer's affected units")
        evidence = args.get("evidence", "")
        completed = [a for a in self.store.appointments(cid) if a["appointment_id"] == evidence and a["status"] == "completed"
                     and args["serial"] in a["serials"]]
        if not completed:
            raise ToolError("no evidence: a unit is remediated only when a completed appointment for it exists (a customer's statement is not evidence; "
                            "escalate to field_service to verify)")
        self.store.set_unit_status(args["serial"], "remediated", note=f"evidence {evidence}", actor=self.actor)
        return {"serial": args["serial"], "status": "remediated", "evidence": evidence}

    def _get_sla_status(self, args: dict, *, key: str, ctx: Any) -> dict:
        from .orchestrator import sla_status
        return {"customers": sla_status(self.store)}

    def _pause_campaign(self, args: dict, *, key: str, ctx: Any) -> dict:
        scope = args["scope"]
        if scope.startswith("lot:"):
            self.store.set("paused_lots", sorted(set(self.store.get("paused_lots", [])) | {scope[4:]}))
        elif scope.startswith("remedy:"):
            self.store.set("paused_remedies", sorted(set(self.store.get("paused_remedies", [])) | {scope[7:]}))
        elif scope == "all":
            self.store.set("status", "paused")
        else:
            raise ToolError("scope must be lot:<lot>, remedy:<remedy> or all")
        self.store.audit(self.actor, "campaign.paused", scope, {"reason": args["reason"]})
        return {"paused": scope, "reason": args["reason"]}
