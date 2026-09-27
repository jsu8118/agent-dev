"""Access to the course dataset (the fictional company Kestrel Pumps & Controls)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .config import DATA_DIR, runs_dir

OPS_DB = DATA_DIR / "kestrel_ops.db"


def data_path(*parts: str) -> Path:
    path = DATA_DIR.joinpath(*parts)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run `python data/generate_data.py` to (re)build the dataset")
    return path


def read_text(*parts: str) -> str:
    return data_path(*parts).read_text(encoding="utf-8")


def load_json(*parts: str) -> Any:
    return json.loads(read_text(*parts))


def load_jsonl(*parts: str) -> list[dict]:
    return [json.loads(line) for line in read_text(*parts).splitlines() if line.strip()]


def ops_db(*, readonly: bool = True) -> sqlite3.Connection:
    """Open the operations database. Read-only by default: agents should not mutate the master copy."""
    path = data_path("kestrel_ops.db")
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def memory_db() -> sqlite3.Connection:
    """A private, writable in-memory copy of the ops DB: isolated per connection, nothing left on disk."""
    source = ops_db()
    try:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        source.backup(conn)
    finally:
        source.close()
    conn.row_factory = sqlite3.Row
    return conn


def scratch_db(name: str = "kestrel_ops_scratch.db") -> sqlite3.Connection:
    """A fresh writable copy of the ops DB under .runs/ - for labs whose tools write (refunds, RMAs...)."""
    target = runs_dir("db") / name
    shutil.copyfile(data_path("kestrel_ops.db"), target)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
