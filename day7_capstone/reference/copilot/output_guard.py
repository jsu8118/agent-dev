"""Step 6 of the pipeline: deterministic checks on the reply before it is sent (Day 6, guardrails layer 3).

The agent's tools already stop most harmful *actions*. This guard checks the *words*: anything that
must never reach a customer, whatever the model was told or tricked into. A failed check doesn't
delete the draft; it holds it for a human (disposition "review"), so a false positive costs a
minute of someone's time, not a lost reply.

Every check here is a rule you can state in one sentence and test in isolation. Checks that need
judgement (tone, helpfulness, correctness) belong in the eval suite, not in a send-time gate.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass

from .config import QUALITY_HOLDS
from .screen import KESTREL_DOMAIN, ORDER_RE

EMAIL_RE = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
INTERNAL_QUALITY = re.compile(r"\b(?:INC-P\d-\d{4}|QH-\d{4}|SCAR-\d{4})\b|quality hold|defective (?:lot|batch)|"
                              r"\brecall\b|" + "|".join(re.escape(lot) for lot in QUALITY_HOLDS), re.I)
LIABILITY = re.compile(r"\b(?:we are|we're|kestrel is) (?:liable|responsible for the (?:damage|failure|loss))|"
                       r"\bour fault\b|\bwe accept (?:full )?(?:responsibility|liability)\b|"
                       r"\bwe (?:will|shall) compensate\b|\bwe guarantee\b|\bguaranteed (?:refund|replacement)\b", re.I)
MAX_CHARS = 2500


@dataclass
class GuardIssue:
    check: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _requester_orders(db: sqlite3.Connection, from_email: str) -> set[str]:
    domain = from_email.rsplit("@", 1)[-1].lower() if "@" in from_email else ""
    return {r[0] for r in db.execute("SELECT o.order_id FROM orders o JOIN customers c USING (customer_id) "
                                     "WHERE c.email_domain = ?", (domain,))}


def review_reply(reply: str, *, from_email: str, email_text: str, tool_calls: list[dict],
                 db: sqlite3.Connection) -> list[GuardIssue]:
    issues: list[GuardIssue] = []
    if not reply.strip():
        return [GuardIssue("empty_reply", "the agent produced no reply")]

    # 1. No other customer's orders: allowed = the sender's own orders, orders they quoted, and orders the tools
    #    returned successfully (the tools verify identity, e.g. with order ID + PO number).
    allowed = _requester_orders(db, from_email) | set(ORDER_RE.findall(email_text))
    allowed |= {str((c.get("input") or {}).get("order_id", "")).upper() for c in tool_calls if not c["is_error"]}
    foreign = sorted(set(ORDER_RE.findall(reply)) - allowed)
    if foreign:
        issues.append(GuardIssue("foreign_order_ids", f"reply mentions orders the sender doesn't own: {foreign}"))

    # 2. No third-party email addresses (the sender's own domain and Kestrel's are fine).
    sender_domain = from_email.rsplit("@", 1)[-1].lower()
    others = sorted({m.group(0) for m in EMAIL_RE.finditer(reply)
                     if m.group(1).lower() not in (sender_domain, KESTREL_DOMAIN)})
    if others:
        issues.append(GuardIssue("third_party_contact", f"reply contains other email addresses: {others}"))

    # 3. No payment data (IBANs, card-like numbers) in outbound mail.
    if IBAN_RE.search(reply) or CARD_RE.search(reply):
        issues.append(GuardIssue("payment_data", "reply contains an IBAN or card-like number"))

    # 4. No internal quality information before inspection (SOP-SUP-007 section 4).
    match = INTERNAL_QUALITY.search(reply)
    if match:
        issues.append(GuardIssue("internal_quality_info", f"reply mentions '{match.group(0)}'"))

    # 5. No admissions of liability or promises beyond policy (SOP-SUP-007 section 4).
    match = LIABILITY.search(reply)
    if match:
        issues.append(GuardIssue("liability_or_promise", f"reply says '{match.group(0)}'"))

    # 6. Replies are emails, not essays.
    if len(reply) > MAX_CHARS:
        issues.append(GuardIssue("too_long", f"{len(reply)} characters (limit {MAX_CHARS})"))
    return issues
