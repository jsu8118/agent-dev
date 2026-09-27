"""M4 - the quality-hold detector.

Two supplier lots are on hold (config.QUALITY_HOLDS; incident reports in data/quality/incident_reports).
When a customer reports a SYMPTOM on a unit built with a held lot, raise a QualityAlert for Quality.

Rules of the game (see README, milestone M4):
* units come from build_records (serial_number, sku, order_id, seal_lot, board_lot), never from the model;
* count a unit only if the sender owns it, or the agent successfully looked it up (check_warranty,
  create_rma) - the tools verified identity for those;
* no symptom, no alert: "where is my order?" from an affected customer must not page Quality;
* the alert is internal - the customer reply must never mention the hold.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from labkit import runs_dir

from .screen import ScreenResult
from .triage import InboundEmail, TicketTriage


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
    evidence: list[str] = field(default_factory=list)
    symptom: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def detect_quality_signals(email: InboundEmail, screen: ScreenResult, triage: TicketTriage | None,
                           tool_calls: list[dict], db: sqlite3.Connection) -> list[QualityAlert]:
    """TODO(M4): return one alert per held lot found. `tool_calls` items look like
    {"name": "check_warranty", "input": {"order_id": ..., "sku": ...}, "is_error": False, "output": {...}}."""
    raise NotImplementedError("M4: detect_quality_signals")


def publish_alerts(alerts: list[QualityAlert], outbox: Path | None = None) -> Path | None:
    """Provided: append alerts to Quality's inbox (a JSONL file here; a QMS ticket in production)."""
    if not alerts:
        return None
    outbox = outbox or runs_dir("capstone_starter") / "quality_alerts.jsonl"
    with outbox.open("a", encoding="utf-8") as fh:
        for alert in alerts:
            fh.write(json.dumps(alert.to_dict()) + "\n")
    return outbox
