"""Step 5 of the pipeline: the quality-hold detector (links support to Day 4's quality investigation).

Two supplier lots are on quality hold (config.QUALITY_HOLDS). Units built with them have already
shipped. When a customer reports a symptom on one of those units, Quality engineering must hear
about it the same day: it is field evidence for the open incident and may trigger a recall.

Who decides? Deterministic code, from facts:

* the units come from serials in the email, orders the sender owns, and the (order, SKU) pairs
  the agent *successfully* looked up (the tools already verified identity for those);
* the lot comes from `build_records`, never from the model;
* a symptom must be reported (triage category or symptom keywords), so "where is my order?"
  from an affected customer doesn't page Quality.

The alert is internal. The customer reply never mentions the hold: SOP-SUP-007 section 4 forbids
speculating about root cause before inspection (output_guard.py enforces it).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from labkit import runs_dir

from .config import QUALITY_HOLDS
from .screen import ScreenResult
from .triage import InboundEmail, TicketTriage

SYMPTOM = re.compile(r"\b(?:leak\w*|weep\w*|drip\w*|crack\w*|fail\w*|fault\w*|f\d{2}\b|trip\w*|noise|noisy|vibrat\w*|"
                     r"overheat\w*|seiz\w*|won'?t (?:start|restart)|reset\w*|error)", re.I)
SYMPTOM_CATEGORIES = {"warranty_claim", "technical_support", "safety_incident"}
FAMILY = re.compile(r"\b(KP-\d{3}|KC-\d)\b")


@dataclass
class QualityAlert:
    lot: str
    part: str
    supplier: str
    incident: str
    risk: str
    ticket_id: str
    customer_id: str
    from_email: str
    serials: list[str] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)     # how each unit was identified
    symptom: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _requester_customer_id(db: sqlite3.Connection, email: str) -> str | None:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    row = db.execute("SELECT customer_id FROM customers WHERE email_domain = ?", (domain,)).fetchone()
    return row[0] if row else None


def _units(db: sqlite3.Connection, where: str, params: tuple) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT b.serial_number, b.sku, b.order_id, b.seal_lot, b.board_lot, o.customer_id "
        f"FROM build_records b JOIN orders o USING (order_id) WHERE {where}", params).fetchall()


def detect_quality_signals(email: InboundEmail, screen: ScreenResult, triage: TicketTriage | None,
                           tool_calls: list[dict], db: sqlite3.Connection) -> list[QualityAlert]:
    text = email.as_text()
    symptom = SYMPTOM.search(text)
    if not symptom and not (triage and triage.category in SYMPTOM_CATEGORIES):
        return []
    me = _requester_customer_id(db, email.from_email)
    families = set(FAMILY.findall(text))
    found: dict[str, tuple[sqlite3.Row, str]] = {}      # serial -> (row, evidence)

    # 1. Serial numbers written in the email (only units of the sender's own account count).
    for serial in screen.serials:
        for row in _units(db, "b.serial_number = ?", (serial,)):
            if row["customer_id"] == me:
                found.setdefault(row["serial_number"], (row, "serial quoted in email"))
    # 2. (order, SKU) pairs the agent successfully looked up - the tools verified the requester for these.
    for call in tool_calls:
        if call["name"] in ("check_warranty", "create_rma") and not call["is_error"]:
            args = call.get("input") or {}
            for row in _units(db, "b.order_id = ? AND b.sku = ?", (args.get("order_id", ""),
                                                                   str(args.get("sku", "")).upper())):
                found.setdefault(row["serial_number"], (row, f"{call['name']} on {row['order_id']}"))
    # 3. Orders named in the email that belong to the sender, narrowed to the product family mentioned.
    for order_id in screen.order_ids:
        for row in _units(db, "b.order_id = ?", (order_id,)):
            if row["customer_id"] == me and (not families or any(row["sku"].startswith(f) for f in families)):
                found.setdefault(row["serial_number"], (row, f"order {order_id} named in email"))

    alerts: dict[str, QualityAlert] = {}
    for serial, (row, evidence) in sorted(found.items()):
        for lot in (row["seal_lot"], row["board_lot"]):
            if lot in QUALITY_HOLDS:
                hold = QUALITY_HOLDS[lot]
                alert = alerts.setdefault(lot, QualityAlert(
                    lot=lot, part=hold["part"], supplier=hold["supplier"], incident=hold["incident"],
                    risk=hold["risk"], ticket_id=email.ticket_id, customer_id=row["customer_id"],
                    from_email=email.from_email,
                    symptom=(symptom.group(0) if symptom else triage.category if triage else "")))
                alert.serials.append(serial)
                if row["order_id"] not in alert.order_ids:
                    alert.order_ids.append(row["order_id"])
                alert.evidence.append(f"{serial}: {evidence}")
    return list(alerts.values())


def publish_alerts(alerts: list[QualityAlert], outbox: Path | None = None) -> Path | None:
    """Append alerts to Quality's inbox (a JSONL outbox here; a ticket in the QMS in production)."""
    if not alerts:
        return None
    outbox = outbox or runs_dir("capstone") / "quality_alerts.jsonl"
    with outbox.open("a", encoding="utf-8") as fh:
        for alert in alerts:
            fh.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                 **alert.to_dict()}) + "\n")
    return outbox
