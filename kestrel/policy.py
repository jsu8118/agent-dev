"""Kestrel's business rules as deterministic code.

Design principle taught on Day 2: *policy lives in code, not in the prompt.*  The model is
good at understanding the customer and choosing what to do; it should not be the component
that computes a restocking fee or decides whether a refund exceeds an approval limit.  Tools
call these functions, so the rules hold even if the model is confused, jailbroken, or wrong.
Rules mirror data/company/policies/*.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

AS_OF = dt.date(2026, 9, 15)

RETURN_WINDOW_DAYS = 30
DAMAGE_REPORT_DAYS = 5
RESTOCKING_FEE_RATE = 0.15
AGENT_REFUND_LIMIT = 2_500.00
MANAGER_REFUND_LIMIT = 10_000.00
WARRANTY_MONTHS = {"pump": 24, "valve": 18, "controller": 12, "sensor": 12, "spare_part": 6}
RETURN_REASONS = ("no_longer_needed", "wrong_item", "damaged_in_transit", "defective")


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def add_months(day: dt.date, months: int) -> dt.date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30,
            31, 30, 31][month - 1]
    return dt.date(year, month, min(day.day, last))


@dataclass
class Decision:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def return_eligibility(*, product_line: str, sku: str, configured: bool, special_order: bool, delivered: str | None,
                       unit_price: float, qty: int, reason: str, as_of: dt.date = AS_OF) -> Decision:
    """Policy RET-002 for one order line."""
    if reason not in RETURN_REASONS:
        return Decision(False, [f"Unknown reason {reason!r}; use one of {', '.join(RETURN_REASONS)}"])
    if reason == "defective":
        return Decision(False, ["Defective items are handled as warranty claims (Policy WAR-001), not returns. "
                                "Use check_warranty."])
    delivered_on = _date(delivered)
    if delivered_on is None:
        return Decision(False, ["The order has not been delivered yet; nothing to return. (Cancellation before "
                                "shipment is handled by the order desk.)"])
    days = (as_of - delivered_on).days
    reasons: list[str] = []
    if product_line == "service":
        reasons.append("Services (SVC-*) are non-returnable (RET-002 §2).")
    if configured and reason == "no_longer_needed":
        reasons.append("Configured controllers are non-returnable; they can only be handled under warranty (RET-002 §2).")
    if special_order and reason == "no_longer_needed":
        reasons.append("Special-order items are non-returnable (RET-002 §2).")
    if reason == "damaged_in_transit" and days > DAMAGE_REPORT_DAYS:
        reasons.append(f"Transit damage must be reported within {DAMAGE_REPORT_DAYS} days of delivery "
                       f"(delivered {delivered_on}, {days} days ago).")
    if reason == "no_longer_needed" and days > RETURN_WINDOW_DAYS:
        reasons.append(f"Outside the {RETURN_WINDOW_DAYS}-day return window (delivered {delivered_on}, "
                       f"{days} days ago).")
    value = round(unit_price * qty, 2)
    kestrel_fault = reason in ("wrong_item", "damaged_in_transit")
    fee = 0.0 if kestrel_fault else round(value * RESTOCKING_FEE_RATE, 2)
    details = {"sku": sku, "qty": qty, "delivered_date": str(delivered_on), "days_since_delivery": days,
               "line_value_usd": value, "restocking_fee_usd": fee, "refund_after_inspection_usd": round(value - fee, 2),
               "return_by": str(delivered_on + dt.timedelta(days=RETURN_WINDOW_DAYS)),
               "replacement_ships_immediately": kestrel_fault}
    return Decision(not reasons, reasons or ["Eligible under RET-002."], details)


def warranty_status(*, product_line: str, sku: str, ship_date: str | None, tier: str,
                    as_of: dt.date = AS_OF) -> Decision:
    """Policy WAR-001: is the item within its warranty period?"""
    shipped = _date(ship_date)
    if shipped is None:
        return Decision(False, ["Item has not shipped; warranty has not started."])
    if product_line == "service":
        end = shipped + dt.timedelta(days=90)
    else:
        end = add_months(shipped, WARRANTY_MONTHS[product_line])
    in_warranty = as_of <= end
    details = {"sku": sku, "ship_date": str(shipped), "warranty_end": str(end), "in_warranty": in_warranty,
               "advance_replacement_eligible": in_warranty and tier == "strategic",
               "inspection_required": True,
               "exclusions_to_check": ["dry running (controller fault F10, heat-checked seal faces)",
                                       "cavitation from insufficient NPSH at site",
                                       "installation/alignment by non-certified installer",
                                       "non-genuine parts, incompatible fluid, supply voltage outside ±10%"]}
    reasons = [f"In warranty until {end}." if in_warranty else f"Warranty expired on {end}."]
    return Decision(in_warranty, reasons, details)


def refund_approver(amount_usd: float) -> str:
    """Who may approve a refund of this size (RET-002 §6)."""
    if amount_usd <= AGENT_REFUND_LIMIT:
        return "agent"
    if amount_usd <= MANAGER_REFUND_LIMIT:
        return "support_manager"
    return "finance_director"
