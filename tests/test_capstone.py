"""Capstone (Day 7) checks beyond "the scripts run": the reference must pass every milestone strictly,
and the service's idempotency store must hold under concurrent duplicate deliveries."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "day7_capstone"))


def test_reference_passes_every_milestone_strictly() -> None:
    env = {**os.environ, "LABKIT_MODE": "mock", "LABKIT_QUIET": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    script = ROOT / "day7_capstone" / "starter" / "run_starter_check.py"
    proc = subprocess.run([sys.executable, str(script), "--impl", "reference", "--strict"], cwd=script.parent,
                          env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "6/6 milestones pass" in proc.stdout


def test_idempotency_store_runs_a_duplicate_once() -> None:
    from reference.copilot.service import OutcomeStore

    store, runs, results = OutcomeStore(), [], []

    def deliver() -> None:
        first, stored = store.claim("gw-42")
        if first:
            runs.append(1)
            time.sleep(0.2)                       # the first delivery is still processing...
            store.finish("gw-42", {"ticket_id": "gw-42"})
            results.append("processed")
        else:
            results.append("duplicate" if stored else "missing")

    threads = [threading.Thread(target=deliver) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1
    assert sorted(results) == ["duplicate"] * 4 + ["processed"]


def test_abandoned_claim_can_be_retried() -> None:
    from reference.copilot.service import OutcomeStore

    store = OutcomeStore()
    assert store.claim("gw-7") == (True, None)
    store.abandon("gw-7")                         # processing crashed before an outcome existed
    assert store.claim("gw-7") == (True, None)
