"""Build kestrel_ops.db: customers, products, inventory, orders, shipments, invoices, RMAs, build records.

Random filler orders make the data realistic; *anchor* orders are hand-specified scenarios
that tickets, labs, and eval sets refer to by key (e.g. "late_customs").  Order IDs are
assigned after sorting by date, so older orders always have lower IDs.
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from pathlib import Path

from .common import (AS_OF, CARRIERS, CUSTOMER_BY_ID, CUSTOMERS, INTL_COUNTRIES, PRODUCT_BY_SKU, PRODUCTS,
                     TIER_DISCOUNT, WAREHOUSES, d)

SCHEMA = """
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT, tier TEXT CHECK (tier IN ('strategic','key','standard')),
    country TEXT, contact_name TEXT, contact_email TEXT, email_domain TEXT, phone TEXT, account_manager TEXT,
    credit_limit_usd REAL, created_at TEXT
);
CREATE TABLE products (
    sku TEXT PRIMARY KEY, name TEXT, product_line TEXT, family TEXT, list_price_usd REAL,
    warranty_months INTEGER, lead_time_days INTEGER, weight_kg REAL, configurable INTEGER, returnable INTEGER
);
CREATE TABLE inventory (
    sku TEXT, warehouse TEXT, on_hand INTEGER, reserved INTEGER, reorder_point INTEGER,
    PRIMARY KEY (sku, warehouse)
);
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(customer_id), order_date TEXT,
    status TEXT CHECK (status IN ('pending','confirmed','in_production','shipped','delivered','cancelled','on_hold')),
    promised_date TEXT, customer_po TEXT, ship_to_country TEXT, total_usd REAL, notes TEXT
);
CREATE TABLE order_lines (
    order_id TEXT REFERENCES orders(order_id), line_no INTEGER, sku TEXT REFERENCES products(sku), qty INTEGER,
    unit_price_usd REAL, line_total_usd REAL, configured INTEGER DEFAULT 0, special_order INTEGER DEFAULT 0,
    PRIMARY KEY (order_id, line_no)
);
CREATE TABLE shipments (
    shipment_id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(order_id), warehouse TEXT, carrier TEXT,
    tracking_number TEXT, ship_date TEXT, eta_date TEXT, delivered_date TEXT,
    status TEXT CHECK (status IN ('in_transit','delivered','exception')), exception_reason TEXT
);
CREATE TABLE invoices (
    invoice_id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(order_id), customer_id TEXT, issue_date TEXT,
    due_date TEXT, amount_usd REAL, paid_amount_usd REAL,
    status TEXT CHECK (status IN ('open','paid','overdue','disputed','credited')), notes TEXT
);
CREATE TABLE rmas (
    rma_id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(order_id), sku TEXT, qty INTEGER,
    reason_code TEXT CHECK (reason_code IN ('defective','wrong_item','damaged_in_transit','no_longer_needed','warranty_claim')),
    status TEXT CHECK (status IN ('requested','approved','received','refunded','rejected','replaced')),
    requested_at TEXT, received_at TEXT, refund_due_usd REAL, notes TEXT
);
CREATE TABLE build_records (
    serial_number TEXT PRIMARY KEY, sku TEXT, order_id TEXT, build_date TEXT, plant TEXT,
    seal_lot TEXT, board_lot TEXT, test_result TEXT
);
CREATE TABLE refunds (
    refund_id TEXT PRIMARY KEY, order_id TEXT, rma_id TEXT, amount_usd REAL, reason TEXT,
    approved_by TEXT, status TEXT, created_at TEXT
);
CREATE TABLE escalations (
    escalation_id TEXT PRIMARY KEY, customer_id TEXT, order_id TEXT, queue TEXT, priority TEXT,
    reason TEXT, created_at TEXT, status TEXT
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, target TEXT, details TEXT
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_shipments_order ON shipments(order_id);
CREATE INDEX idx_invoices_order ON invoices(order_id);
CREATE INDEX idx_rmas_order ON rmas(order_id);
"""

# --------------------------------------------------------------------------- anchor scenarios
# lines: (sku, qty) or (sku, qty, {"configured": 1})
ANCHORS: dict[str, dict] = {
    "late_customs": dict(customer="C-1007", order_date="2026-08-20", lines=[("KP-250-S", 2)], promised="2026-09-08",
                         po="APM-PO-5531", status="shipped",
                         shipment=dict(warehouse="WH-EU", carrier="intl", ship="2026-09-02", eta="2026-09-09",
                                       status="exception",
                                       exception="Customs hold at Dublin Port: consignee EORI number missing on the "
                                                 "commercial invoice (carrier requested it on 2026-09-09)")),
    "late_weather": dict(customer="C-1003", order_date="2026-08-31",
                         lines=[("MS-250", 6), ("BRG-6309", 12), ("CPL-250", 2)], promised="2026-09-09",
                         po="SRM-44871", status="shipped",
                         shipment=dict(warehouse="WH-WEST", carrier="intl", ship="2026-09-03", eta="2026-09-17",
                                       status="exception",
                                       exception="Highway 16 closed due to wildfire; carrier rerouting via Prince "
                                                 "George. Original ETA 2026-09-10, revised ETA 2026-09-17")),
    "in_transit_ontime": dict(customer="C-1004", order_date="2026-09-08", lines=[("KV-50-F", 6)],
                              promised="2026-09-17", po="GV-2026-118", status="shipped",
                              shipment=dict(warehouse="WH-WEST", carrier="freight", ship="2026-09-11",
                                            eta="2026-09-16", status="in_transit")),
    "return_ok": dict(customer="C-1005", order_date="2026-08-22", lines=[("MS-250", 10)], promised="2026-08-29",
                      po="HF-77310", status="delivered",
                      shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2026-08-25", eta="2026-08-28",
                                    delivered="2026-08-28", status="delivered")),
    "return_too_late": dict(customer="C-1008", order_date="2026-07-10", lines=[("BRG-6312", 8)],
                            promised="2026-07-17", po="DPP-11902", status="delivered",
                            shipment=dict(warehouse="WH-EAST", carrier="parcel", ship="2026-07-14",
                                          eta="2026-07-17", delivered="2026-07-17", status="delivered")),
    "configured_controller": dict(customer="C-1012", order_date="2026-08-05", lines=[("KC-2", 2, {"configured": 1})],
                                  promised="2026-08-19", po="LDC-PO-3390", status="delivered",
                                  shipment=dict(warehouse="WH-EAST", carrier="parcel", ship="2026-08-14",
                                                eta="2026-08-18", delivered="2026-08-18", status="delivered")),
    "damaged_transit": dict(customer="C-1009", order_date="2026-09-04", lines=[("KV-20-B", 12)],
                            promised="2026-09-11", po="RB-0931", status="delivered",
                            shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2026-09-08",
                                          eta="2026-09-11", delivered="2026-09-11", status="delivered")),
    "wrong_item": dict(customer="C-1010", order_date="2026-09-01", lines=[("IMP-250-D", 2)], promised="2026-09-11",
                       po="ISF-2026-0457", status="delivered",
                       shipment=dict(warehouse="WH-EAST", carrier="parcel", ship="2026-09-08", eta="2026-09-10",
                                     delivered="2026-09-10", status="delivered")),
    "warranty_ok": dict(customer="C-1001", order_date="2026-07-28", lines=[("KP-250-S", 2)], promised="2026-08-14",
                        po="BMU-26-0718", status="delivered",
                        shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2026-08-12",
                                      eta="2026-08-14", delivered="2026-08-14", status="delivered")),
    "warranty_dry_run": dict(customer="C-1023", order_date="2025-10-27", lines=[("KP-100-S", 1), ("KC-1", 1)],
                             promised="2025-11-05", po="CCD-1027", status="delivered",
                             shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2025-11-03",
                                           eta="2025-11-06", delivered="2025-11-06", status="delivered")),
    "warranty_expired": dict(customer="C-1015", order_date="2024-05-28", lines=[("KP-100-S", 1)],
                             promised="2024-06-12", po="EPD-0528", status="delivered",
                             shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2024-06-10",
                                           eta="2024-06-13", delivered="2024-06-13", status="delivered")),
    "refund_small": dict(customer="C-1016", order_date="2026-08-10", lines=[("FLT-SUC-100", 3)],
                         promised="2026-08-14", po="KMC-8812", status="delivered",
                         shipment=dict(warehouse="WH-EAST", carrier="parcel", ship="2026-08-12", eta="2026-08-14",
                                       delivered="2026-08-14", status="delivered"),
                         invoice=dict(status="paid"),
                         rma=dict(sku="FLT-SUC-100", qty=3, reason="no_longer_needed", status="received",
                                  requested="2026-08-20", received="2026-09-10", restocking=True,
                                  notes="Unused, original packaging. Inspection passed 2026-09-10.")),
    "refund_manager": dict(customer="C-1014", order_date="2026-07-01", lines=[("KP-250-X", 1)],
                           promised="2026-08-07", po="MOS-55120", status="delivered",
                           shipment=dict(warehouse="WH-WEST", carrier="freight", ship="2026-08-06",
                                         eta="2026-08-11", delivered="2026-08-11", status="delivered"),
                           rma=dict(sku="KP-250-X", qty=1, reason="no_longer_needed", status="received",
                                    requested="2026-08-18", received="2026-09-08", restocking=True,
                                    notes="Customer ordered ATEX variant by mistake. Unit unused; inspection passed.")),
    "refund_director": dict(customer="C-1011", order_date="2026-08-03", lines=[("KP-400-S", 1)],
                            promised="2026-09-18", po="PDP-7781", status="cancelled",
                            invoice=dict(status="paid", prepaid=True,
                                         notes="Prepayment received 2026-08-07. Order cancelled by customer "
                                               "2026-09-01 before shipment; full refund due.")),
    "billing_duplicate": dict(customer="C-1006", order_date="2026-08-03", lines=[("KV-50-F", 4)],
                              promised="2026-08-07", po="NG-PO-99231", status="delivered",
                              shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2026-08-05",
                                            eta="2026-08-07", delivered="2026-08-07", status="delivered"),
                              invoice=dict(status="paid", paid_multiplier=2.0,
                                           notes="Two identical payments received (2026-09-02 and 2026-09-05).")),
    "billing_overdue": dict(customer="C-1019", order_date="2026-06-25", lines=[("MS-400", 4), ("BRG-6312", 8)],
                            promised="2026-07-01", po="GBWD-2026-061", status="delivered",
                            shipment=dict(warehouse="WH-WEST", carrier="parcel", ship="2026-06-29",
                                          eta="2026-07-01", delivered="2026-07-01", status="delivered"),
                            invoice=dict(status="overdue")),
    "in_production": dict(customer="C-1018", order_date="2026-08-24", lines=[("KP-400-S", 1)],
                          promised="2026-10-12", po="PCS-4410", status="in_production"),
    "cancelled": dict(customer="C-1021", order_date="2026-08-15", lines=[("KC-1", 2)], promised="2026-08-22",
                      po="WHG-3102", status="cancelled", notes="Cancelled at customer request on 2026-08-17."),
    "hazard_pump": dict(customer="C-1002", order_date="2026-04-02", lines=[("KP-250-X", 2)], promised="2026-05-18",
                        po="CCW-26-0402", status="delivered",
                        shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2026-05-15",
                                      eta="2026-05-20", delivered="2026-05-20", status="delivered")),
    "critical_pumps": dict(customer="C-1019", order_date="2025-10-20", lines=[("KP-400-S", 2)],
                           promised="2025-12-08", po="GBWD-2025-190", status="delivered",
                           shipment=dict(warehouse="WH-WEST", carrier="freight", ship="2025-12-05",
                                         eta="2025-12-10", delivered="2025-12-10", status="delivered")),
    "vibration_pump": dict(customer="C-1022", order_date="2026-01-12", lines=[("KP-400-S", 1)],
                           promised="2026-02-27", po="TOE-26-004", status="delivered",
                           shipment=dict(warehouse="WH-EU", carrier="intl", ship="2026-02-23", eta="2026-03-02",
                                         delivered="2026-03-02", status="delivered")),
    "pending_strategic": dict(customer="C-1020", order_date="2026-09-10",
                              lines=[("KP-250-S", 4), ("KC-2", 4, {"configured": 1}), ("SVC-INSTALL", 2)],
                              promised="2026-09-22", po="OSF-PO-12007", status="confirmed"),
    "shipped_tracking": dict(customer="C-1013", order_date="2026-09-07", lines=[("VS-10", 8), ("PT-40", 4)],
                             promised="2026-09-16", po="CSL-7730", status="shipped",
                             shipment=dict(warehouse="WH-EU", carrier="intl", ship="2026-09-09", eta="2026-09-16",
                                           status="in_transit")),
    "address_change": dict(customer="C-1024", order_date="2026-09-11", lines=[("KV-80-G", 2)],
                           promised="2026-09-18", po="VFP-2291", status="confirmed"),
    "spanish_delivered": dict(customer="C-1017", order_date="2026-08-20", lines=[("KV-20-B", 10), ("KV-50-F", 2)],
                              promised="2026-09-01", po="SLT-0820", status="delivered",
                              shipment=dict(warehouse="WH-WEST", carrier="intl", ship="2026-08-24",
                                            eta="2026-09-01", delivered="2026-09-01", status="delivered")),
    "field_seal_fail_1": dict(customer="C-1005", order_date="2026-07-30", lines=[("KP-250-S", 1)],
                              promised="2026-08-13", po="HF-77102", status="delivered",
                              shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2026-08-10",
                                            eta="2026-08-13", delivered="2026-08-13", status="delivered"),
                              rma=dict(sku="KP-250-S", qty=1, reason="defective", status="approved",
                                       requested="2026-09-09", notes="Seal leaking after ~200 operating hours; "
                                       "customer reports clean water service, no dry-run events logged.")),
    "field_seal_fail_2": dict(customer="C-1016", order_date="2026-08-04", lines=[("KP-250-S", 1)],
                              promised="2026-08-18", po="KMC-8790", status="delivered",
                              shipment=dict(warehouse="WH-EAST", carrier="freight", ship="2026-08-17",
                                            eta="2026-08-19", delivered="2026-08-19", status="delivered"),
                              rma=dict(sku="KP-250-S", qty=1, reason="defective", status="requested",
                                       requested="2026-09-12", notes="Weep at seal gland after commissioning; "
                                       "installer reports correct priming.")),
}


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _unit_price(sku: str, qty: int, tier: str) -> float:
    price = PRODUCT_BY_SKU[sku][4] * (1 - TIER_DISCOUNT[tier])
    if qty >= 10 and PRODUCT_BY_SKU[sku][2] != "service":
        price *= 0.95                                    # volume discount
    return _money(price)


def _business_days_after(start: dt.date, n: int) -> dt.date:
    day = start
    while n > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _carrier_for(country: str, weight: float) -> str:
    if country != "US":
        return CARRIERS["intl"]
    return CARRIERS["parcel"] if weight < 30 else CARRIERS["freight"]


def _tracking(rng: random.Random, carrier: str) -> str:
    prefix = {"NorthLine Freight": "NLF", "SwiftParcel": "SWP", "BlueRiver Logistics": "BRL"}[carrier]
    return f"{prefix}{rng.randint(10**9, 10**10 - 1)}"


def _random_orders(rng: random.Random, n: int) -> list[dict]:
    tiers_weight = {"strategic": 5, "key": 3, "standard": 1.5}
    customers = [c for c in CUSTOMERS]
    weights = [tiers_weight[c[3]] for c in customers]
    sku_pool = ([p[0] for p in PRODUCTS if p[2] == "spare_part"] * 4 + [p[0] for p in PRODUCTS if p[2] == "valve"] * 3
                + [p[0] for p in PRODUCTS if p[2] == "sensor"] * 2 + ["KP-100-S", "KP-250-S", "KP-250-S", "KP-250-X",
                                                                     "KP-400-S", "KP-600-M", "KC-1", "KC-1", "KC-2"])
    start, end = d("2026-01-05"), d("2026-09-14")
    orders = []
    for _ in range(n):
        cust = rng.choices(customers, weights)[0]
        order_date = start + dt.timedelta(days=rng.randint(0, (end - start).days))
        if order_date.weekday() >= 5:
            order_date -= dt.timedelta(days=order_date.weekday() - 4)
        lines = []
        for sku in rng.sample(sku_pool, rng.randint(1, 3)):
            if any(line[0] == sku for line in lines):
                continue
            line_kind = PRODUCT_BY_SKU[sku][2]
            qty = {"pump": rng.randint(1, 3), "valve": rng.randint(2, 16), "controller": rng.randint(1, 3),
                   "sensor": rng.randint(2, 12), "spare_part": rng.randint(2, 24), "service": 1}[line_kind]
            configured = 1 if line_kind == "controller" and rng.random() < 0.6 else 0
            lines.append((sku, qty, {"configured": configured}))
        if any(PRODUCT_BY_SKU[line[0]][2] == "pump" for line in lines) and rng.random() < 0.25:
            lines.append(("SVC-INSTALL", rng.randint(1, 2), {}))
        orders.append({"customer": cust[0], "order_date": order_date.isoformat(), "lines": lines, "random": True})
    return orders


def _derive_random_status(rng: random.Random, order: dict) -> None:
    """Fill promised date, status and shipment for a random order, relative to AS_OF."""
    order_date = d(order["order_date"])
    lead = max(PRODUCT_BY_SKU[line[0]][6] for line in order["lines"])
    ship_date = _business_days_after(order_date, rng.randint(1, 2) + (lead if lead > 5 else 0))
    country = CUSTOMER_BY_ID[order["customer"]][4]
    transit = rng.randint(6, 11) if country in INTL_COUNTRIES else rng.randint(2, 4)
    order["promised"] = _business_days_after(ship_date, transit).isoformat()
    roll = rng.random()
    if roll < 0.03:
        order["status"] = "cancelled"
        return
    if roll < 0.05 and ship_date > AS_OF - dt.timedelta(days=20):
        order["status"] = "on_hold"
        order["notes"] = "Credit hold: account over credit limit."
        return
    if ship_date > AS_OF:
        order["status"] = "in_production" if lead > 5 else ("pending" if (AS_OF - order_date).days <= 1 else "confirmed")
        return
    weight = sum(PRODUCT_BY_SKU[line[0]][7] * line[1] for line in order["lines"])
    carrier_kind = "intl" if country in INTL_COUNTRIES else ("parcel" if weight < 30 else "freight")
    eta = _business_days_after(ship_date, transit)
    if weight == 0:                         # services only
        order["status"] = "delivered"
        return
    shipment = dict(warehouse="WH-EU" if country in {"IE", "GB", "NO", "ES"} else rng.choice(["WH-EAST", "WH-WEST"]),
                    carrier=carrier_kind, ship=ship_date.isoformat(), eta=eta.isoformat())
    if eta >= AS_OF:
        shipment["status"] = "in_transit"
        order["status"] = "shipped"
    else:
        delivered = eta + dt.timedelta(days=rng.choice([0, 0, 0, 1]))
        shipment.update(status="delivered", delivered=min(delivered, AS_OF - dt.timedelta(days=1)).isoformat())
        order["status"] = "delivered"
    order["shipment"] = shipment


def seal_lot_for(build: dt.date) -> str:
    if build < d("2026-06-01"):
        return "PS-2603-C"
    if build < d("2026-08-05"):
        return "PS-2606-A"
    if build <= d("2026-08-19"):
        return "PS-2608-B"
    return "PS-2609-A"


def board_lot_for(sku: str, build: dt.date) -> str | None:
    if sku == "KC-2":
        if build < d("2026-08-01"):
            return "VD-2606-B"
        if build <= d("2026-08-19"):
            return "VD-2607-C"
        return "VD-2608-A"
    if sku == "KC-1":
        return "VD-2605-K1" if build < d("2026-07-01") else "VD-2607-K1"
    return None


def build(rng: random.Random, out_path: Path) -> dict:
    orders = _random_orders(rng, 285)
    for order in orders:
        _derive_random_status(rng, order)
    for key, spec in ANCHORS.items():
        orders.append({**spec, "anchor": key})
    orders.sort(key=lambda o: (o["order_date"], o.get("anchor", ""), o["customer"]))

    anchors: dict[str, dict] = {}
    rows = {name: [] for name in ("orders", "order_lines", "shipments", "invoices", "rmas", "build_records")}
    ship_seq, inv_seq, rma_seq = 50001, 90001, 7001
    serial_seq: dict[str, int] = {}

    for idx, order in enumerate(orders, start=10001):
        order_id = f"SO-{idx}"
        cust = CUSTOMER_BY_ID[order["customer"]]
        tier, country = cust[3], cust[4]
        lines_out, total = [], 0.0
        for line_no, line in enumerate(order["lines"], start=1):
            sku, qty = line[0], line[1]
            flags = line[2] if len(line) > 2 else {}
            unit = _unit_price(sku, qty, tier)
            line_total = _money(unit * qty)
            total += line_total
            lines_out.append((order_id, line_no, sku, qty, unit, line_total, flags.get("configured", 0),
                              flags.get("special_order", 0)))
        rows["order_lines"].extend(lines_out)
        po = order.get("po") or f"{cust[1].split()[0][:3].upper()}-{rng.randint(1000, 99999)}"
        rows["orders"].append((order_id, cust[0], order["order_date"], order["status"], order.get("promised"),
                               po, country, _money(total), order.get("notes")))
        record = {"order_id": order_id, "customer_id": cust[0], "po": po, "total": _money(total),
                  "promised": order.get("promised"), "status": order["status"],
                  "lines": [(l[2], l[3], l[4], l[5]) for l in lines_out]}

        shipment = order.get("shipment")
        if shipment:
            weight = sum(PRODUCT_BY_SKU[line[0]][7] * line[1] for line in order["lines"])
            carrier = CARRIERS[shipment["carrier"]] if shipment["carrier"] in CARRIERS else shipment["carrier"]
            if shipment["carrier"] == "freight" and country in INTL_COUNTRIES:
                carrier = CARRIERS["intl"]
            del weight
            shipment_id = f"SH-{ship_seq}"
            ship_seq += 1
            tracking = _tracking(rng, carrier)
            rows["shipments"].append((shipment_id, order_id, shipment["warehouse"], carrier, tracking,
                                      shipment["ship"], shipment["eta"], shipment.get("delivered"),
                                      shipment["status"], shipment.get("exception")))
            record.update(shipment_id=shipment_id, tracking=tracking, carrier=carrier, ship_date=shipment["ship"],
                          eta=shipment["eta"], delivered=shipment.get("delivered"))

        invoice_spec = order.get("invoice", {})
        if shipment or invoice_spec:
            issue = d(shipment["ship"]) if shipment else d(order["order_date"]) + dt.timedelta(days=2)
            due = issue + dt.timedelta(days=30)
            amount = _money(total)
            status = invoice_spec.get("status")
            paid = 0.0
            if status is None:
                if due < AS_OF:
                    r = rng.random()
                    status = "paid" if r < 0.9 else ("overdue" if r < 0.97 else "disputed")
                else:
                    status = "paid" if rng.random() < 0.4 else "open"
            if status == "paid":
                paid = _money(amount * invoice_spec.get("paid_multiplier", 1.0))
            invoice_id = f"AR-{inv_seq}"
            inv_seq += 1
            rows["invoices"].append((invoice_id, order_id, cust[0], issue.isoformat(), due.isoformat(), amount, paid,
                                     status, invoice_spec.get("notes")))
            record.update(invoice_id=invoice_id, invoice_amount=amount, invoice_due=due.isoformat(),
                          invoice_status=status, invoice_paid=paid)

        rma = order.get("rma")
        if rma:
            rma_id = f"RMA-{rma_seq}"
            rma_seq += 1
            line = next(l for l in lines_out if l[2] == rma["sku"])
            value = _money(line[4] * rma["qty"])
            refund_due = _money(value * (0.85 if rma.get("restocking") else 1.0)) if rma["status"] == "received" else None
            rows["rmas"].append((rma_id, order_id, rma["sku"], rma["qty"], rma["reason"], rma["status"],
                                 rma["requested"], rma.get("received"), refund_due, rma.get("notes")))
            record["rma_id"] = rma_id
            record["refund_due"] = refund_due

        # build records for pumps and controllers that left the factory
        if shipment:
            for line in lines_out:
                sku, qty = line[2], line[3]
                line_kind = PRODUCT_BY_SKU[sku][2]
                if line_kind not in ("pump", "controller"):
                    continue
                build_date = d(shipment["ship"]) - dt.timedelta(days=3)
                family = sku.replace("-", "")[:5]
                serials = []
                for _ in range(qty):
                    key = f"{family}-{build_date:%y%m}"
                    serial_seq[key] = serial_seq.get(key, 0) + 1
                    serial = f"{key}-{serial_seq[key]:04d}"
                    serials.append(serial)
                    plant = "P2" if line_kind == "pump" else "P3"
                    rows["build_records"].append((serial, sku, order_id, build_date.isoformat(), plant,
                                                  seal_lot_for(build_date) if line_kind == "pump" else None,
                                                  board_lot_for(sku, build_date), "pass"))
                record.setdefault("serials", {})[sku] = serials

        if "anchor" in order:
            anchors[order["anchor"]] = record

    # random RMAs on older delivered orders
    delivered = [o for o in rows["orders"] if o[3] == "delivered" and o[0] not in {a["order_id"] for a in anchors.values()}]
    for order_row in rng.sample(delivered, 18):
        line = next(l for l in rows["order_lines"] if l[0] == order_row[0])
        reason = rng.choice(["defective", "damaged_in_transit", "no_longer_needed", "warranty_claim", "wrong_item"])
        status = rng.choice(["refunded", "replaced", "rejected", "replaced", "refunded"])
        requested = d(order_row[2]) + dt.timedelta(days=rng.randint(12, 40))
        if requested >= AS_OF:
            requested = AS_OF - dt.timedelta(days=3)
        rows["rmas"].append((f"RMA-{rma_seq}", order_row[0], line[2], max(1, line[3] // 2), reason, status,
                             requested.isoformat(), (requested + dt.timedelta(days=9)).isoformat(), None,
                             None))
        rma_seq += 1

    # write the database
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (c[0], c[1], c[2], c[3], c[4], c[5], f"{c[5].split()[0].lower()}.{c[5].split()[-1].lower().replace(chr(39), '')}@{c[6]}",
         c[6], f"+1-555-{1000 + i:04d}", c[7], {"strategic": 500000, "key": 250000, "standard": 75000}[c[3]],
         f"20{10 + i % 14:02d}-0{1 + i % 9}-15")
        for i, c in enumerate(CUSTOMERS)])
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8], 0 if p[2] == "service" else 1) for p in PRODUCTS])
    inventory = []
    for p in PRODUCTS:
        if p[2] == "service":
            continue
        for wh in WAREHOUSES:
            base = {"pump": 4, "valve": 40, "controller": 12, "sensor": 30, "spare_part": 120}[p[2]]
            on_hand = max(0, int(base * rng.uniform(0.2, 1.6)))
            if p[0] in ("KP-400-S", "KP-600-M", "KP-250-X"):
                on_hand = 0                  # built to order
            if p[0] == "MS-250":
                on_hand = 0 if wh == "WH-EAST" else on_hand    # quality hold on lot PS-2608-B drained east stock
            inventory.append((p[0], wh, on_hand, min(on_hand, int(on_hand * rng.uniform(0, 0.3))), base // 4))
    conn.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", inventory)
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", rows["orders"])
    conn.executemany("INSERT INTO order_lines VALUES (?,?,?,?,?,?,?,?)", rows["order_lines"])
    conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?,?)", rows["shipments"])
    conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?)", rows["invoices"])
    conn.executemany("INSERT INTO rmas VALUES (?,?,?,?,?,?,?,?,?,?)", rows["rmas"])
    conn.executemany("INSERT INTO build_records VALUES (?,?,?,?,?,?,?,?)", rows["build_records"])
    conn.commit()
    conn.close()
    return anchors
