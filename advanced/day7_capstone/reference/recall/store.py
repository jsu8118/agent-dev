"""The campaign's durable state.

Two kinds of state live here, in one SQLite file so a crash cannot separate them:
  * the **runs** (event logs, leases, effects, approvals) - `advanced.lib.durable.RunStore`;
  * the **campaign record** - units, customers, outbound/inbound messages, engineer slots, parts, credits,
    escalations, retries and the audit log - plain tables with primary keys that make every write idempotent
    (a message id, an appointment id, a reservation reference), so a replayed step cannot double-book a slot
    or send an email twice.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from advanced.lib.durable import RunStore

from .config import RECALL_DATA, CAMPAIGN_START

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (serial TEXT PRIMARY KEY, customer_id TEXT, sku TEXT, lot TEXT, remedy TEXT, risk_class TEXT,
    region TEXT, site_id TEXT, status TEXT, contacted_at TEXT, appointment_id TEXT, remediated_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, tier TEXT, region TEXT, timezone TEXT, language TEXT,
    account_manager TEXT, risk_class TEXT, status TEXT, first_contact_at TEXT, last_outbound_at TEXT, last_inbound_at TEXT,
    reminders INTEGER DEFAULT 0, hold_reason TEXT);
CREATE TABLE IF NOT EXISTS contacts (contact_id TEXT PRIMARY KEY, customer_id TEXT, name TEXT, email TEXT, role TEXT, channel TEXT,
    active INTEGER DEFAULT 1, note TEXT);
CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY, direction TEXT, customer_id TEXT, address TEXT, template TEXT,
    subject TEXT, body TEXT, at TEXT, in_reply_to TEXT, label TEXT, run_id TEXT, held INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS slots (slot_id TEXT PRIMARY KEY, engineer_id TEXT, engineer TEXT, region TEXT, skills TEXT, start TEXT,
    end TEXT, status TEXT, appointment_id TEXT);
CREATE TABLE IF NOT EXISTS appointments (appointment_id TEXT PRIMARY KEY, customer_id TEXT, slot_id TEXT, engineer_id TEXT,
    start TEXT, serials TEXT, remedy TEXT, status TEXT, created_at TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS parts (sku TEXT, warehouse TEXT, on_hand INTEGER, reserved INTEGER DEFAULT 0, PRIMARY KEY (sku, warehouse));
CREATE TABLE IF NOT EXISTS reservations (reference TEXT PRIMARY KEY, sku TEXT, warehouse TEXT, qty INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS credits (credit_id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, reason TEXT, approved_by TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS escalations (escalation_id TEXT PRIMARY KEY, customer_id TEXT, queue TEXT, priority TEXT, summary TEXT, at TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS retries (customer_id TEXT PRIMARY KEY, not_before TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS campaign (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, target TEXT, details TEXT);
"""


class CampaignStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.runs = RunStore(self.path)                       # same file: one crash domain
        self._local = threading.local()
        self._lock = threading.RLock()
        with self.conn() as conn:
            conn.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ campaign key/value
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn().execute("SELECT value FROM campaign WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.conn().execute("INSERT INTO campaign (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (key, json.dumps(value)))

    @property
    def today(self) -> dt.date:
        return dt.date.fromisoformat(self.get("today", CAMPAIGN_START.isoformat()))

    def now(self, hour: int = 9) -> str:
        day = self.today
        return dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")

    def audit(self, actor: str, action: str, target: str, details: Any = None) -> None:
        self.conn().execute("INSERT INTO audit (ts, actor, action, target, details) VALUES (?,?,?,?,?)",
                            (self.now(), actor, action, target, json.dumps(details, default=str)[:2000]))

    # ------------------------------------------------------------------ loading the dataset (idempotent)
    def load(self, data_dir: Path = RECALL_DATA) -> "CampaignStore":
        if self.get("loaded"):
            return self
        campaign = json.loads((data_dir / "campaign.json").read_text(encoding="utf-8"))
        units = json.loads((data_dir / "affected_units.json").read_text(encoding="utf-8"))["units"]
        contacts = json.loads((data_dir / "contacts.json").read_text(encoding="utf-8"))["contacts"]
        engineers = json.loads((data_dir / "engineers.json").read_text(encoding="utf-8"))["engineers"]
        parts = json.loads((data_dir / "parts.json").read_text(encoding="utf-8"))["kits"]
        conn = self.conn()
        with self._lock:
            self.set("campaign", campaign)
            self.set("status", "active")
            self.set("paused_lots", [])
            self.set("paused_remedies", [])
            self.set("today", CAMPAIGN_START.isoformat())
            for u in units:
                conn.execute("INSERT OR IGNORE INTO units (serial, customer_id, sku, lot, remedy, risk_class, region, site_id, status) "
                             "VALUES (?,?,?,?,?,?,?,?,'pending')",
                             (u["serial_number"], u["customer_id"], u["sku"], u["lot"], u["remedy"], u["risk_class"], u["region"], u["site_id"]))
            for c in contacts:
                risk = max((u["risk_class"] for u in units if u["customer_id"] == c["customer_id"]),
                           key=lambda r: {"safety": 3, "production": 2, "standard": 1}[r])
                region = next(u["region"] for u in units if u["customer_id"] == c["customer_id"])
                conn.execute("INSERT OR IGNORE INTO customers (customer_id, name, tier, region, timezone, language, account_manager, risk_class, status) "
                             "VALUES (?,?,?,?,?,?,?,?,'pending')",
                             (c["customer_id"], c["customer"], c["tier"], region, c["timezone"], c["language"], c["account_manager"], risk))
                for who in ("primary", "site"):
                    p = c[who]
                    conn.execute("INSERT OR IGNORE INTO contacts (contact_id, customer_id, name, email, role, channel, note) VALUES (?,?,?,?,?,?,?)",
                                 (p["contact_id"], c["customer_id"], p["name"], p["email"], p["role"], p["channel"], c.get("notes", "") if who == "primary" else ""))
            for e in engineers:
                for s in e["slots"]:
                    conn.execute("INSERT OR IGNORE INTO slots (slot_id, engineer_id, engineer, region, skills, start, end, status) VALUES (?,?,?,?,?,?,?,?)",
                                 (s["slot_id"], e["engineer_id"], e["name"], e["region"], json.dumps(e["skills"]), s["start"], s["end"], s["status"]))
            for k in parts:
                for wh, n in k["stock"].items():
                    conn.execute("INSERT OR IGNORE INTO parts (sku, warehouse, on_hand) VALUES (?,?,?)", (k["sku"], wh, n))
            self.set("kits", parts)
            self.set("loaded", True)
        return self

    # ------------------------------------------------------------------ reads
    def campaign(self) -> dict:
        return self.get("campaign") or {}

    def units(self, customer_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM units WHERE 1=1", []
        if customer_id:
            sql, args = sql + " AND customer_id = ?", args + [customer_id]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        return [dict(r) for r in self.conn().execute(sql + " ORDER BY serial", args)]

    def customers(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM customers", []
        if status:
            sql, args = sql + " WHERE status = ?", [status]
        return [dict(r) for r in self.conn().execute(sql + " ORDER BY customer_id", args)]

    def customer(self, customer_id: str) -> dict | None:
        row = self.conn().execute("SELECT * FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
        return dict(row) if row else None

    def contacts(self, customer_id: str) -> list[dict]:
        return [dict(r) for r in self.conn().execute("SELECT * FROM contacts WHERE customer_id = ? ORDER BY contact_id", (customer_id,))]

    def messages(self, customer_id: str | None = None, direction: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM messages WHERE 1=1", []
        if customer_id:
            sql, args = sql + " AND customer_id = ?", args + [customer_id]
        if direction:
            sql, args = sql + " AND direction = ?", args + [direction]
        return [dict(r) for r in self.conn().execute(sql + " ORDER BY at, message_id", args)]

    def slots(self, region: str | None = None, skill: str | None = None, status: str = "free",
              not_before: str | None = None, not_after: str | None = None) -> list[dict]:
        rows = [dict(r) for r in self.conn().execute("SELECT * FROM slots WHERE status = ? ORDER BY start", (status,))]
        out = []
        for r in rows:
            r["skills"] = json.loads(r["skills"])
            if region and r["region"] != region:
                continue
            if skill and skill not in r["skills"]:
                continue
            if not_before and r["start"] < not_before:
                continue
            if not_after and r["start"] > not_after:
                continue
            out.append(r)
        return out

    def appointments(self, customer_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM appointments", []
        if customer_id:
            sql, args = sql + " WHERE customer_id = ?", [customer_id]
        rows = [dict(r) for r in self.conn().execute(sql + " ORDER BY start", args)]
        for r in rows:
            r["serials"] = json.loads(r["serials"])
        return rows

    def parts(self) -> list[dict]:
        return [dict(r) for r in self.conn().execute("SELECT * FROM parts ORDER BY sku, warehouse")]

    def escalations(self) -> list[dict]:
        return [dict(r) for r in self.conn().execute("SELECT * FROM escalations ORDER BY at")]

    def credits(self) -> list[dict]:
        return [dict(r) for r in self.conn().execute("SELECT * FROM credits ORDER BY at")]

    def audit_rows(self, target: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM audit", []
        if target:
            sql, args = sql + " WHERE target = ? OR details LIKE ?", [target, f"%{target}%"]
        return [dict(r) for r in self.conn().execute(sql + " ORDER BY id", args)]

    # ------------------------------------------------------------------ writes (all keyed, all audited)
    def record_message(self, message_id: str, *, direction: str, customer_id: str, address: str, template: str | None,
                       subject: str, body: str, in_reply_to: str | None = None, label: str = "", run_id: str = "",
                       held: bool = False, actor: str = "system") -> bool:
        """Returns True if the message was new (False = duplicate id: already recorded)."""
        with self._lock:
            cur = self.conn().execute(
                "INSERT OR IGNORE INTO messages (message_id, direction, customer_id, address, template, subject, body, at, in_reply_to, "
                "label, run_id, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, direction, customer_id, address, template, subject, body, self.now(10), in_reply_to, label, run_id, int(held)))
            if cur.rowcount == 0:
                return False
            col = "last_outbound_at" if direction == "outbound" else "last_inbound_at"
            self.conn().execute(f"UPDATE customers SET {col} = ? WHERE customer_id = ?", (self.now(10), customer_id))
            if direction == "outbound" and not held:
                self.conn().execute("UPDATE customers SET first_contact_at = COALESCE(first_contact_at, ?), status = CASE WHEN status = 'pending' "
                                    "THEN 'contacted' ELSE status END WHERE customer_id = ?", (self.now(10), customer_id))
                self.conn().execute("UPDATE units SET status = 'contacted', contacted_at = COALESCE(contacted_at, ?) WHERE customer_id = ? "
                                    "AND status = 'pending'", (self.now(10), customer_id))
            self.audit(actor, f"message.{direction}", message_id, {"customer_id": customer_id, "address": address, "template": template,
                                                                    "held": held, "label": label})
            return True

    def set_customer_status(self, customer_id: str, status: str, *, reason: str = "", actor: str = "system") -> None:
        self.conn().execute("UPDATE customers SET status = ?, hold_reason = ? WHERE customer_id = ?", (status, reason, customer_id))
        self.audit(actor, "customer.status", customer_id, {"status": status, "reason": reason})

    def set_unit_status(self, serial: str, status: str, *, note: str = "", actor: str = "system") -> None:
        cols = ", remediated_at = ?" if status == "remediated" else ""
        args: list = [status, note] + ([self.now(16)] if status == "remediated" else []) + [serial]
        self.conn().execute(f"UPDATE units SET status = ?, note = ?{cols} WHERE serial = ?", args)
        self.audit(actor, "unit.status", serial, {"status": status, "note": note})

    def book(self, appointment_id: str, *, customer_id: str, slot_id: str, serials: list[str], remedy: str, run_id: str = "",
             actor: str = "system") -> dict:
        """Book a free slot for these serials; idempotent on appointment_id. Raises ValueError when the slot is taken."""
        with self._lock:
            conn = self.conn()
            existing = conn.execute("SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
            if existing:
                return {**dict(existing), "serials": json.loads(existing["serials"]), "duplicate": True}
            slot = conn.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
            if slot is None:
                raise ValueError(f"unknown slot {slot_id}")
            if slot["status"] != "free":
                raise ValueError(f"slot {slot_id} is {slot['status']}")
            conn.execute("UPDATE slots SET status = 'booked', appointment_id = ? WHERE slot_id = ? AND status = 'free'", (appointment_id, slot_id))
            conn.execute("INSERT INTO appointments (appointment_id, customer_id, slot_id, engineer_id, start, serials, remedy, status, created_at, run_id) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", (appointment_id, customer_id, slot_id, slot["engineer_id"], slot["start"],
                                                          json.dumps(serials), remedy, "booked", self.now(11), run_id))
            for serial in serials:
                conn.execute("UPDATE units SET status = 'scheduled', appointment_id = ? WHERE serial = ? AND status IN ('pending', 'contacted')",
                             (appointment_id, serial))
            conn.execute("UPDATE customers SET status = 'scheduled' WHERE customer_id = ? AND status IN ('pending', 'contacted')", (customer_id,))
            self.audit(actor, "appointment.booked", appointment_id, {"customer_id": customer_id, "slot_id": slot_id, "serials": serials})
            return {"appointment_id": appointment_id, "customer_id": customer_id, "slot_id": slot_id, "engineer_id": slot["engineer_id"],
                    "engineer": slot["engineer"], "start": slot["start"], "serials": serials, "remedy": remedy, "status": "booked"}

    def reserve(self, reference: str, *, sku: str, warehouse: str, qty: int, actor: str = "system") -> dict:
        """Reserve parts; idempotent on reference. Raises ValueError when stock is short."""
        with self._lock:
            conn = self.conn()
            existing = conn.execute("SELECT * FROM reservations WHERE reference = ?", (reference,)).fetchone()
            if existing:
                return {**dict(existing), "duplicate": True}
            row = conn.execute("SELECT on_hand, reserved FROM parts WHERE sku = ? AND warehouse = ?", (sku, warehouse)).fetchone()
            if row is None:
                raise ValueError(f"{sku} is not stocked at {warehouse}")
            if row["on_hand"] - row["reserved"] < qty:
                raise ValueError(f"only {row['on_hand'] - row['reserved']} x {sku} available at {warehouse}")
            conn.execute("UPDATE parts SET reserved = reserved + ? WHERE sku = ? AND warehouse = ?", (qty, sku, warehouse))
            conn.execute("INSERT INTO reservations (reference, sku, warehouse, qty, at) VALUES (?,?,?,?,?)", (reference, sku, warehouse, qty, self.now(11)))
            self.audit(actor, "parts.reserved", reference, {"sku": sku, "warehouse": warehouse, "qty": qty})
            return {"reference": reference, "sku": sku, "warehouse": warehouse, "qty": qty}

    def credit(self, credit_id: str, *, customer_id: str, amount: float, reason: str, approved_by: str, actor: str = "system") -> dict:
        with self._lock:
            cur = self.conn().execute("INSERT OR IGNORE INTO credits (credit_id, customer_id, amount, reason, approved_by, at) VALUES (?,?,?,?,?,?)",
                                      (credit_id, customer_id, amount, reason, approved_by, self.now(12)))
            if cur.rowcount:
                self.audit(actor, "credit.issued", credit_id, {"customer_id": customer_id, "amount": amount, "approved_by": approved_by})
            return {"credit_id": credit_id, "customer_id": customer_id, "amount": amount, "approved_by": approved_by, "duplicate": cur.rowcount == 0}

    def escalate(self, escalation_id: str, *, customer_id: str, queue: str, priority: str, summary: str, run_id: str = "",
                 actor: str = "system") -> dict:
        with self._lock:
            cur = self.conn().execute("INSERT OR IGNORE INTO escalations (escalation_id, customer_id, queue, priority, summary, at, run_id) "
                                      "VALUES (?,?,?,?,?,?,?)", (escalation_id, customer_id, queue, priority, summary, self.now(12), run_id))
            if cur.rowcount:
                self.audit(actor, "escalation.created", escalation_id, {"customer_id": customer_id, "queue": queue, "priority": priority})
            return {"escalation_id": escalation_id, "queue": queue, "priority": priority, "duplicate": cur.rowcount == 0}

    def schedule_retry(self, customer_id: str, not_before: str, reason: str, actor: str = "system") -> None:
        self.conn().execute("INSERT INTO retries (customer_id, not_before, reason) VALUES (?,?,?) ON CONFLICT(customer_id) DO UPDATE SET "
                            "not_before = excluded.not_before, reason = excluded.reason", (customer_id, not_before, reason))
        self.audit(actor, "retry.scheduled", customer_id, {"not_before": not_before, "reason": reason})

    def retry_for(self, customer_id: str) -> dict | None:
        row = self.conn().execute("SELECT * FROM retries WHERE customer_id = ?", (customer_id,)).fetchone()
        return dict(row) if row else None

    def deactivate_contact(self, contact_id: str, note: str, actor: str = "system") -> None:
        self.conn().execute("UPDATE contacts SET active = 0, note = ? WHERE contact_id = ?", (note, contact_id))
        self.audit(actor, "contact.deactivated", contact_id, {"note": note})

    # ------------------------------------------------------------------ dashboard
    def summary(self) -> dict:
        conn = self.conn()
        unit_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM units GROUP BY status")}
        cust_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM customers GROUP BY status")}
        out = conn.execute("SELECT COUNT(*) FROM messages WHERE direction = 'outbound' AND held = 0").fetchone()[0]
        held = conn.execute("SELECT COUNT(*) FROM messages WHERE direction = 'outbound' AND held = 1").fetchone()[0]
        inbound = conn.execute("SELECT COUNT(*) FROM messages WHERE direction = 'inbound'").fetchone()[0]
        quarantined = conn.execute("SELECT COUNT(*) FROM messages WHERE direction = 'inbound' AND label LIKE 'quarantined:%'").fetchone()[0]
        return {"today": self.today.isoformat(), "status": self.get("status"), "paused_lots": self.get("paused_lots"),
                "paused_remedies": self.get("paused_remedies"), "units": unit_status, "customers": cust_status,
                "outbound_sent": out, "outbound_held": held, "inbound": inbound, "inbound_quarantined": quarantined,
                "appointments": conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0],
                "escalations": conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0],
                "credits_usd": conn.execute("SELECT COALESCE(SUM(amount), 0) FROM credits").fetchone()[0],
                "pending_approvals": sum(1 for r in self.runs.list(status="waiting_approval"))}
