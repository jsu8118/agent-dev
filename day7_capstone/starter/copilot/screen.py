"""M1 - the deterministic input screen. Runs before any model call; costs nothing.

Build three detectors (see day7_capstone/README.md, milestone M1):

1. security signals: instructions aimed at an AI, payment-detail changes, impersonating sender domains;
2. a safety backstop for SOP-SUP-007 section 1 (data/company/policies/safety_escalation_sop.md);
3. identifiers (order IDs, serial numbers) for the quality detector (M4).

Target on the 62 labelled tickets (the checker measures it): all 4 P1 tickets caught, zero false
alarms, and security flags on exactly T-1208, T-1507 and T-1703.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KESTREL_DOMAIN = "kestrel-pumps.example"
ORDER_RE = re.compile(r"\bSO-\d{5}\b")
SERIAL_RE = re.compile(r"\b(?:KP\d{3}|KC\d)-\d{4}-\d{4}\b")      # e.g. KP250-2608-0002, KC2-2608-0001

# (regex, label) pairs. Two examples to start from; add what the tickets and the SOP need.
INJECTION = [
    (r"\bignore (?:all |any )?(?:the |your )?(?:previous|prior) (?:instructions|rules)", "instruction override"),
    (r"\bskip (?:the )?(?:identity )?verification\b", "asks to skip verification"),
    # TODO(M1): fake system/admin modes, notes addressed to an AI, "pre-approved", "do not escalate", ...
]
PAYMENT_CHANGE = [
    (r"\bIBAN\b", "IBAN in message"),
    # TODO(M1): "our bank details have changed", "new bank account", routing/sort codes, ...
]

# (name, predicate on the lower-cased text). One example; SOP-SUP-007 s.1 lists six situations.
SAFETY_RULES = [
    ("fire_smoke", lambda t: bool(re.search(r"\b(?:smoke|flames?|sparks?|burning smell)\b", t))),
    # TODO(M1): hazardous/hot fluid leaks, pressure-boundary failures, injuries, ATEX faults,
    #           critical-service outages (drinking water, hospitals, fire protection...), recurring F07/F17.
    # Hint: some rules need TWO conditions (e.g. "zone 1" AND a fault word), or "tripped" would page
    #       an engineer for every ATEX pump question. Hint 2: customers also write in Spanish and German.
]


@dataclass
class ScreenResult:
    security_flags: list[str] = field(default_factory=list)
    safety_hits: list[str] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    serials: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(self.security_flags)

    @property
    def safety(self) -> bool:
        return bool(self.safety_hits)

    def to_dict(self) -> dict:
        return {"security_flags": self.security_flags, "safety_hits": self.safety_hits,
                "order_ids": self.order_ids, "serials": self.serials}


def domain_impersonation(sender_domain: str) -> str | None:
    """Return a description if `sender_domain` imitates a customer's or Kestrel's domain, else None.

    TODO(M1): load the known domains (customers.email_domain in the ops DB, via labkit.data.ops_db) and
    detect lookalikes (one or two letters changed: bluewater-utilitles.example), homoglyphs (rn -> m,
    0 -> o) and cousin domains (orion-semi-helpdesk.example). A legitimate unknown sender (a personal
    mailbox, a prospect) must NOT be flagged: identity checks for those happen in the tools.
    """
    raise NotImplementedError("M1: domain_impersonation")


def screen_email(from_email: str, subject: str, body: str) -> ScreenResult:
    """TODO(M1): apply INJECTION, PAYMENT_CHANGE, domain_impersonation and SAFETY_RULES; extract IDs."""
    raise NotImplementedError("M1: screen_email")
