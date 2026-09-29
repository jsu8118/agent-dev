"""Advanced capstone (Day 7) checks beyond "the scripts run": the reference passes every milestone strictly, the
security layer and the scoped desk hold on their own, and a crash mid-outreach never duplicates a notice."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "advanced" / "day7_capstone"))

from advanced.lib.durable import Crash, ToolContext          # noqa: E402
from labkit import get_client, runs_dir                       # noqa: E402
from reference.recall import security                         # noqa: E402
from reference.recall.config import DEFAULT                   # noqa: E402
from reference.recall.orchestrator import Orchestrator, load_replies   # noqa: E402
from reference.recall.store import CampaignStore              # noqa: E402
from reference.recall.tools import CampaignDesk               # noqa: E402


def _store(name: str) -> CampaignStore:
    base = runs_dir("advanced_capstone", "tests")
    for p in base.glob(name + "*"):
        p.unlink()
    return CampaignStore(base / name).load()


def test_reference_passes_every_milestone_strictly() -> None:
    env = {**os.environ, "LABKIT_MODE": "mock", "LABKIT_QUIET": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    script = ROOT / "advanced" / "day7_capstone" / "starter" / "run_starter_check.py"
    proc = subprocess.run([sys.executable, str(script), "--impl", "reference", "--strict"], cwd=script.parent, env=env,
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "6/6 milestones pass" in proc.stdout


@pytest.mark.parametrize("label", ["fraud", "injection", "data_request", "phishing", "spoofed_internal"])
def test_every_hostile_reply_is_quarantined_before_any_model_call(label: str) -> None:
    store = _store("sec.db")
    for reply in load_replies():
        if reply["label"] != label:
            continue
        screen = security.screen_inbound(reply, store.contacts(reply["customer_id"]))
        assert screen.quarantine, (reply["reply_id"], screen)


def test_benign_replies_and_flags() -> None:
    store = _store("sec2.db")
    flags = {}
    for reply in load_replies():
        screen = security.screen_inbound(reply, store.contacts(reply["customer_id"]))
        if reply["label"] == "benign":
            assert not screen.quarantine, (reply["reply_id"], screen)
        flags[reply["reply_id"]] = screen.flags
    assert "safety_event" in flags["RPL-018"] and "legal" in flags["RPL-019"] and "out_of_office" in flags["RPL-003"]
    assert security.sender_status("x@harborfood.example", {"harborfoods.example"}) == "lookalike"
    body, issues = security.sanitize_outbound("See https://evil.example/x and we will refund everything. Bluewater Municipal Utilities too.",
                                              customer=None, other_customers=["Bluewater Municipal Utilities"])
    assert "[link removed]" in body and {i["severity"] for i in issues} == {"fixed", "block"}


def test_desk_is_scoped_idempotent_and_role_limited() -> None:
    store = _store("desk.db")
    desk = CampaignDesk(store, DEFAULT, role="inbound", customer_id="C-1005", actor="test", run_id="t")
    assert "error" in desk.execute("get_contacts", {"customer_id": "C-1001"})
    assert "error" in desk.execute("send_email", {"contact_id": "CT-1001-1", "template": "TPL-RC-05", "subject": "s", "body": "b"})
    ctx = ToolContext(run_id="t", store=store.runs, tool_use_id="u1", idempotency_key="t:u1")
    args = {"contact_id": "CT-1005-1", "template": "TPL-RC-05", "subject": "s", "body": "b"}
    assert desk.execute("send_email", args, ctx) == desk.execute("send_email", args, ctx)
    assert len(store.messages("C-1005", "outbound")) == 1
    assert "error" in desk.execute("pause_campaign", {"scope": "all", "reason": "x"})
    assert "error" in desk.execute("mark_remediated", {"serial": "KP250-2608-0004", "evidence": "trust me"})
    blocked = desk.execute("send_email", {**args, "body": "We will refund all downtime."})
    assert "output guard" in blocked["error"]


def test_crash_mid_outreach_then_resume_sends_each_notice_once() -> None:
    client = get_client()
    store = _store("crash.db")
    orch = Orchestrator(store, DEFAULT, client, replies=[])
    with pytest.raises(Crash):
        orch.run_day(1, crash={"customer": "C-1005", "crash_at": ("after_tool", 4)})
    report = orch.run_day(1)
    assert report.resumed == ["outreach:C-1005 -> completed"]
    counts = Counter(m["customer_id"] for m in store.messages(direction="outbound"))
    assert set(counts) == {c["customer_id"] for c in store.customers()} and set(counts.values()) == {1}
    events = [e["type"] for e in store.runs.events("outreach:C-1005")]
    assert events.count("tool.started") == 4 and events.count("model.response") == 5
