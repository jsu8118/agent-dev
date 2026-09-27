"""Regenerate every generated artifact of the course dataset (deterministic, seeded).

    python data/generate_data.py

Hand-written documents (policies, manuals, incident reports, runbooks, triage guidelines)
are committed as-is; this script (re)builds the database, tickets, telemetry, AP documents,
service logs, and evaluation sets, and writes data/anchors.json - the map from scenario
keys (e.g. "late_customs") to the concrete IDs the labs and tests use.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generators import evals, finance, maintenance, ops_db, ops_logs, support  # noqa: E402
from generators.common import AS_OF, DATA_DIR, SEED  # noqa: E402


def main() -> None:
    db_path = DATA_DIR / "kestrel_ops.db"
    anchors = ops_db.build(random.Random(SEED), db_path)
    conn = sqlite3.connect(db_path)
    emails = dict(conn.execute("SELECT customer_id, contact_email FROM customers"))
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("customers", "products", "orders", "order_lines", "shipments", "invoices", "rmas",
                        "build_records")}
    conn.close()

    tickets = support.build(anchors, emails, DATA_DIR / "support")
    maintenance.build(random.Random(SEED + 1), DATA_DIR / "maintenance")
    finance.build(DATA_DIR / "finance")
    ops_logs.build(random.Random(SEED + 2), DATA_DIR / "ops_logs")
    evals.build(anchors, emails, DATA_DIR / "evals")
    (DATA_DIR / "anchors.json").write_text(json.dumps({"as_of": AS_OF.isoformat(), "anchors": anchors}, indent=2),
                                           encoding="utf-8")

    print(f"Dataset regenerated (as-of {AS_OF}):")
    for table, n in counts.items():
        print(f"  {table:<14} {n:>5} rows")
    print(f"  tickets        {len(tickets):>5}")
    print(f"  anchors        {len(anchors):>5} (see data/anchors.json)")


if __name__ == "__main__":
    main()
