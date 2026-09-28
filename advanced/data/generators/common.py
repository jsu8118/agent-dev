"""Shared constants and helpers for the advanced course's datasets.

Everything here builds on the base course's Kestrel Pumps & Controls world (data/): the same customers,
products, orders and build records, read from data/kestrel_ops.db so the two datasets never disagree.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

ADV_DATA_DIR = Path(__file__).resolve().parents[1]              # advanced/data
BASE_DATA_DIR = ADV_DATA_DIR.parents[1] / "data"                  # data/ (base course)
AS_OF = dt.date(2026, 9, 15)
SEED = 20260922


def ops_db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{BASE_DATA_DIR / 'kestrel_ops.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def iso(day: dt.date, hour: int = 9, minute: int = 0) -> str:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
