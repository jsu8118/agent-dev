"""Step 3 of the pipeline: the routing gate and its deterministic handlers.

    safety    SOP-SUP-007 s.1 (screen backstop OR triage P1/safety_incident): P1 escalation to the on-call
              field-service engineer + the SOP's customer instructions. No LLM writes this reply: it must be
              instant, correct every time, and must never contain repair advice.
    security  instructions aimed at an AI, payment-detail changes, impersonating domains: escalate to
              security; neutral reply (or none at all for impersonating domains: don't confirm a phishing
              target). The agent never sees the message, so it cannot be steered by it.
    human     triage unavailable (API error, refusal): holding reply + support-manager queue. Fail safe.
    agent     everything else: the Day 2 support agent, with policy enforced in its tools. When triage says
              `requires_human`, the reply is drafted but held for review instead of sent.

Order matters: safety is checked first because a hazardous leak must page the engineer even when the
message is also suspicious (the security flag is then raised in addition). Everything a handler does
goes through the same SupportDesk tools as the agent, so it is audited the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from kestrel.support_tools import SupportDesk

from .screen import ScreenResult
from .triage import InboundEmail, TicketTriage


@dataclass
class RouteDecision:
    route: str                               # safety | security | human | agent
    reasons: list[str] = field(default_factory=list)
    review: bool = False                     # agent drafts, a human approves before sending
    also_flag_security: bool = False


def decide_route(screen: ScreenResult, triage: TicketTriage | None) -> RouteDecision:
    reasons = [f"screen:{hit}" for hit in screen.safety_hits]
    if triage is not None and (triage.category == "safety_incident" or triage.priority == "P1"):
        reasons.append(f"triage:{triage.category}/{triage.priority}")
    if reasons:
        return RouteDecision("safety", reasons, also_flag_security=screen.suspicious)
    if screen.suspicious:
        return RouteDecision("security", [f"screen:{flag}" for flag in screen.security_flags])
    if triage is None:
        return RouteDecision("human", ["triage unavailable"])
    reasons = [f"triage:{triage.category}/{triage.priority}"]
    if triage.requires_human:
        reasons.append("triage:requires_human")
    return RouteDecision("agent", reasons, review=triage.requires_human)


# ------------------------------------------------------------------------------------ handlers
def _escalate(desk: SupportDesk, queue: str, priority: str, summary: str, order_id: str | None = None) -> dict:
    tool_input = {"queue": queue, "priority": priority, "summary": summary}
    if order_id:
        tool_input["order_id"] = order_id
    content, is_error = desk.run("escalate_to_human", tool_input)
    if is_error:
        raise RuntimeError(f"escalation to {queue} failed: {content}")
    return json.loads(content)


SAFETY_REPLY = {
    "en": ("Thank you for alerting us. We are treating this as a safety priority (P1).\n\n"
           "Please follow your site safety procedures now: keep people clear of the area, and isolate and "
           "de-energize the equipment (lockout/tagout). Do not attempt repairs.\n\n"
           "Our on-call field service engineer will contact you within 1 hour (24/7). Your reference is {ref}."),
    "es": ("Gracias por avisarnos. Tratamos este caso como prioridad de seguridad (P1).\n\n"
           "Siga ahora los procedimientos de seguridad de su planta: mantenga al personal alejado de la zona y aísle "
           "y desenergice el equipo (bloqueo/etiquetado, lockout/tagout). No intente repararlo.\n\n"
           "Nuestro ingeniero de servicio de guardia le contactará en menos de 1 hora (24/7). Su referencia es {ref}."),
    "de": ("Vielen Dank für Ihre Meldung. Wir behandeln dies als Sicherheitsfall mit Priorität P1.\n\n"
           "Bitte befolgen Sie jetzt Ihre Sicherheitsvorschriften vor Ort: Halten Sie Personen fern, trennen und "
           "sichern Sie die Anlage (Lockout/Tagout). Bitte keine Reparaturversuche.\n\n"
           "Unser Bereitschaftsingenieur meldet sich innerhalb von 1 Stunde (24/7). Ihre Referenz: {ref}."),
}
SECURITY_REPLY = ("Thank you for your message. It has been passed to the appropriate team for review, and they will "
                  "contact you if anything further is needed. Your reference is {ref}.")
HOLDING_REPLY = ("Thank you for contacting Kestrel Pumps & Controls. A member of our support team has your message "
                 "and will reply within 1 business day. Your reference is {ref}.")


def handle_safety(desk: SupportDesk, email: InboundEmail, screen: ScreenResult, triage: TicketTriage | None,
                  decision: RouteDecision) -> str:
    summary = (f"SAFETY/CRITICAL ({', '.join(decision.reasons)}). From {email.from_email}. "
               f"Subject: {email.subject}. {' '.join(email.body.split())}")[:1000]
    esc = _escalate(desk, "field_service", "P1", summary, _own_order(desk, screen.order_ids))
    if decision.also_flag_security:
        _escalate(desk, "security", "P2", "Safety report that also contains suspicious content: "
                  + "; ".join(screen.security_flags))
    language = triage.language if triage is not None and triage.language in SAFETY_REPLY else "en"
    return SAFETY_REPLY[language].format(ref=esc["escalation_id"])


def handle_security(desk: SupportDesk, email: InboundEmail, screen: ScreenResult) -> str:
    esc = _escalate(desk, "security", "P2", f"Quarantined email from {email.from_email} (not processed by the "
                    f"assistant). Signals: {'; '.join(screen.security_flags)}. Subject: {email.subject}")
    if any(flag.split()[0] in ("lookalike", "homoglyph", "cousin") for flag in screen.security_flags):
        return ""                      # don't reply to a probable phishing sender
    return SECURITY_REPLY.format(ref=esc["escalation_id"])


def handle_human(desk: SupportDesk, email: InboundEmail, reason: str) -> str:
    esc = _escalate(desk, "support_manager", "P3", f"Automated triage unavailable ({reason}); please handle. "
                    f"From {email.from_email}. Subject: {email.subject}")
    return HOLDING_REPLY.format(ref=esc["escalation_id"])


def _own_order(desk: SupportDesk, order_ids: list[str]) -> str | None:
    """The first quoted order that belongs to the sender (never attach another customer's order)."""
    me = desk.get_customer_profile().get("customer_id")
    for order_id in order_ids:
        row = desk.db.execute("SELECT customer_id FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row and me is not None and row["customer_id"] == me:
            return order_id
    return None
