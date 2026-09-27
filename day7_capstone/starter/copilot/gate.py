"""M2 - the routing gate and its deterministic handlers.

Routes (see day7_capstone/README.md, milestone M2):
    safety    screen safety hit OR triage says safety_incident/P1 -> P1 escalation to field_service + SOP reply
    security  screen security flag -> escalate to security; neutral reply (none for impersonating domains)
    human     triage unavailable (None) -> holding reply + support_manager queue
    agent     everything else; `review=True` when triage.requires_human (draft held for a person)

Precedence matters: think about an email that is BOTH a hazardous leak and a prompt injection.
All actions go through SupportDesk.run(...) so they are audited exactly like the agent's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kestrel.support_tools import SupportDesk

from .screen import ScreenResult
from .triage import InboundEmail, TicketTriage


@dataclass
class RouteDecision:
    route: str                               # safety | security | human | agent
    reasons: list[str] = field(default_factory=list)
    review: bool = False
    also_flag_security: bool = False


def decide_route(screen: ScreenResult, triage: TicketTriage | None) -> RouteDecision:
    """TODO(M2): implement the table above. Record WHY in `reasons` (you'll need it in the review queue)."""
    raise NotImplementedError("M2: decide_route")


def handle_safety(desk: SupportDesk, email: InboundEmail, screen: ScreenResult, triage: TicketTriage | None,
                  decision: RouteDecision) -> str:
    """TODO(M2): escalate_to_human(field_service, P1) via desk.run(...); return the SOP-SUP-007 s.2 reply
    (keep people clear, isolate and de-energize / lockout-tagout, no repair advice, 1-hour callback, reference).
    Also escalate to security when decision.also_flag_security."""
    raise NotImplementedError("M2: handle_safety")


def handle_security(desk: SupportDesk, email: InboundEmail, screen: ScreenResult) -> str:
    """TODO(M2): escalate to security (P2) with the flags; return a neutral reply, or "" for impersonating
    domains (replying would confirm the address to a phisher)."""
    raise NotImplementedError("M2: handle_security")


def handle_human(desk: SupportDesk, email: InboundEmail, reason: str) -> str:
    """TODO(M2): escalate to support_manager (P3); return a holding reply promising a reply within 1 business day."""
    raise NotImplementedError("M2: handle_human")
