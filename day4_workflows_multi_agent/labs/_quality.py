"""Quality-investigation helpers for Lab 04: incident reports, the incident index, and a read-only DB tool.

The DB tool shows how to give an LLM SQL without giving it the database:
    * the connection is opened read-only (labkit.data.ops_db: SQLite `mode=ro`);
    * a SQLite *authorizer* callback allows SELECT and reads of five allow-listed tables only -
      enforced inside the database engine, so no clever SQL (sub-queries, CTEs, PRAGMA, ATTACH,
      sqlite_master) gets around it;
    * results are capped (rows and characters) so one careless query cannot flood the context;
    * errors come back as tool errors with a hint, so the model can fix its query.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from labkit.data import data_path, ops_db

ALLOWED_TABLES = ("build_records", "rmas", "orders", "customers", "shipments")
MAX_ROWS = 50
MAX_CHARS = 8_000

# The answer key used to CHECK the lab's output (never shown to the model).
EXPECTED_LOTS = {"PS-2608-B", "VD-2607-C"}
RED_HERRINGS = {"INC-P1-0318": "FC-2608-11 castings contained at P1", "INC-P2-0415": "torque wrench TW-118",
                "INC-P3-0211": "ESD tester at station 4 (KC-1 only)"}


@dataclass
class IncidentReport:
    report_id: str
    plant: str
    date: str
    title: str
    category: str
    severity: str
    text: str


def _field(text: str, name: str) -> str:
    m = re.search(rf"^\|\s*{re.escape(name)}\s*\|\s*(.*?)\s*\|\s*$", text, re.M)
    return m.group(1) if m else ""


def load_reports() -> list[IncidentReport]:
    reports = []
    for path in sorted(Path(data_path("quality", "incident_reports")).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = re.search(r"^# Incident \S+ — (.*)$", text, re.M)
        reports.append(IncidentReport(report_id=path.stem, plant=_field(text, "Plant").split()[0],
                                      date=_field(text, "Date"), title=title.group(1) if title else path.stem,
                                      category=_field(text, "Category"), severity=_field(text, "Severity"), text=text))
    return reports


def incident_index(reports: list[IncidentReport]) -> str:
    """What the orchestrator sees: one line per report - metadata only, no report bodies."""
    lines = ["report_id | plant | date | title | category | severity"]
    lines += [f"{r.report_id} | {r.plant} | {r.date} | {r.title} | {r.category} | {r.severity}" for r in reports]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- the DB tool
QUERY_TOOL = {
    "name": "query_quality_db",
    "description": (
        "Run ONE read-only SQL SELECT against Kestrel's ERP extract (SQLite) to verify a hypothesis - e.g. which "
        "shipped units were built with a given seal or board lot, and whether any came back as RMAs. Call it "
        "whenever a finding depends on field exposure; never assume ERP facts. Tables (only these are readable):\n"
        "- build_records(serial_number, sku, order_id, build_date, plant, seal_lot, board_lot, test_result)\n"
        "- rmas(rma_id, order_id, sku, qty, reason_code, status, requested_at, received_at, refund_due_usd, notes)"
        "  reason_code in defective|wrong_item|damaged_in_transit|no_longer_needed|warranty_claim\n"
        "- orders(order_id, customer_id, order_date, status, promised_date, customer_po, ship_to_country, total_usd,"
        " notes)\n"
        "- customers(customer_id, name, industry, tier, country, contact_name, contact_email, email_domain, phone,"
        " account_manager, credit_limit_usd, created_at)\n"
        "- shipments(shipment_id, order_id, warehouse, carrier, tracking_number, ship_date, eta_date, delivered_date,"
        " status, exception_reason)\n"
        "An RMA links to a unit through (order_id, sku). Results are capped at 50 rows."),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single SELECT statement"},
                       "purpose": {"type": "string", "description": "The hypothesis this query checks"}},
        "required": ["sql", "purpose"],
        "additionalProperties": False,
    },
}


def _authorizer(action: int, arg1, arg2, dbname, source) -> int:
    if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 in ALLOWED_TABLES else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


class QualityDB:
    def __init__(self) -> None:
        self.conn = ops_db(readonly=True)
        self.conn.set_authorizer(_authorizer)
        self.log: list[dict] = []

    def run(self, tool_input: dict) -> tuple[str, bool]:
        """Execute a query tool call. Returns (content, is_error)."""
        sql = str(tool_input.get("sql", "")).strip().rstrip(";")
        try:
            cursor = self.conn.execute(sql)
            rows = cursor.fetchmany(MAX_ROWS + 1)
            columns = [d[0] for d in cursor.description or []]
            payload = {"columns": columns, "rows": [list(r) for r in rows[:MAX_ROWS]],
                       "row_count": min(len(rows), MAX_ROWS), "truncated": len(rows) > MAX_ROWS}
            content, is_error = json.dumps(payload, default=str), False
            if len(content) > MAX_CHARS:
                content = content[:MAX_CHARS] + '..."} (output truncated: select fewer columns or aggregate)'
        except sqlite3.Error as exc:
            content = json.dumps({"error": str(exc), "hint": "Only single SELECT statements over "
                                  f"{', '.join(ALLOWED_TABLES)} are allowed."})
            is_error = True
        self.log.append({"sql": sql, "purpose": tool_input.get("purpose", ""), "is_error": is_error})
        return content, is_error
