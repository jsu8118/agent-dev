"""Tests for the shared Kestrel domain library and the reference support agent."""

import json

import pytest

from kestrel import policy
from kestrel.kb import KnowledgeBase
from kestrel.support_agent import run_support_agent
from kestrel.support_tools import TOOLS, SupportDesk
from labkit import DATA_DIR, get_client
from labkit.data import scratch_db

ANCHORS = json.loads((DATA_DIR / "anchors.json").read_text())["anchors"]


def test_return_window_and_restocking_fee():
    ok = policy.return_eligibility(product_line="spare_part", sku="MS-250", configured=False, special_order=False,
                                   delivered="2026-08-28", unit_price=559.36, qty=4, reason="no_longer_needed")
    assert ok.eligible and ok.details["restocking_fee_usd"] == 335.62
    late = policy.return_eligibility(product_line="spare_part", sku="BRG-6312", configured=False, special_order=False,
                                     delivered="2026-07-17", unit_price=120.0, qty=8, reason="no_longer_needed")
    assert not late.eligible
    damaged = policy.return_eligibility(product_line="valve", sku="KV-20-B", configured=False, special_order=False,
                                        delivered="2026-09-11", unit_price=310.0, qty=3, reason="damaged_in_transit")
    assert damaged.eligible and damaged.details["restocking_fee_usd"] == 0.0


def test_configured_controller_not_returnable():
    d = policy.return_eligibility(product_line="controller", sku="KC-2", configured=True, special_order=False,
                                  delivered="2026-08-18", unit_price=4784.0, qty=1, reason="no_longer_needed")
    assert not d.eligible and "Configured" in d.reasons[0]


def test_warranty_periods():
    assert policy.warranty_status(product_line="pump", sku="KP-100-S", ship_date="2024-06-10", tier="standard").details[
        "warranty_end"] == "2026-06-10"
    strategic = policy.warranty_status(product_line="pump", sku="KP-250-S", ship_date="2026-08-12", tier="strategic")
    assert strategic.eligible and strategic.details["advance_replacement_eligible"]


@pytest.mark.parametrize("amount, approver", [(2500, "agent"), (2500.01, "support_manager"), (10000, "support_manager"),
                                              (10000.01, "finance_director")])
def test_refund_approval_limits(amount, approver):
    assert policy.refund_approver(amount) == approver


def test_tool_schemas_are_strict_and_closed():
    for t in TOOLS:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False
        assert set(t["input_schema"]["required"]) <= set(t["input_schema"]["properties"])


def test_identity_comes_from_channel_not_model():
    order_id = ANCHORS["pending_strategic"]["order_id"]
    stranger = SupportDesk("someone@mailbox.example", db=scratch_db("t_ident.db"))
    content, is_error = stranger.run("get_order", {"order_id": order_id})
    assert is_error and "not verified" in content
    content, is_error = stranger.run("get_order", {"order_id": order_id,
                                                   "customer_po": ANCHORS["pending_strategic"]["po"]})
    assert not is_error


def test_refund_limit_enforced_in_tool():
    a = ANCHORS["refund_manager"]
    desk = SupportDesk("travis.greer@midlandoil.example", db=scratch_db("t_refund.db"))
    content, is_error = desk.run("issue_refund", {"rma_id": a["rma_id"], "amount_usd": a["refund_due"], "reason": "x"})
    assert is_error and "approval" in content
    small = ANCHORS["refund_small"]
    desk2 = SupportDesk("luis.romero@keystone-mech.example", db=scratch_db("t_refund2.db"))
    content, is_error = desk2.run("issue_refund", {"rma_id": small["rma_id"], "amount_usd": small["refund_due"],
                                                   "reason": "x"})
    assert not is_error
    again, is_error = desk2.run("issue_refund", {"rma_id": small["rma_id"], "amount_usd": 1.0, "reason": "x"})
    assert is_error and "already been refunded" in again


def test_knowledge_base_finds_exact_specs():
    kb = KnowledgeBase()
    top = kb.search("KP-250 bearing regreasing interval grease", top_k=3)
    assert any("2,000" in c.text and "15 g" in c.text for _, c in top)
    f05 = kb.search("F05 drive overtemperature", top_k=2)
    assert f05[0][1].doc == "kc1_controller_manual"


def test_reference_agent_passes_eval_set_in_mock_mode():
    client = get_client()
    for line in (DATA_DIR / "evals" / "support_eval_set.jsonl").read_text().splitlines():
        sc = json.loads(line)
        desk = SupportDesk(sc["from_email"], db=scratch_db(f"t_eval_{sc['id']}.db"))
        result = run_support_agent(client, sc["message"], sc["from_email"], desk=desk)
        called = {c["name"] for c in result.tool_calls}
        assert set(sc["expect"]["tools_required"]) <= called, sc["id"]
        for phrase in sc["expect"]["must_include"]:
            assert any(p.lower() in result.reply.lower() for p in phrase.split("|")), (sc["id"], phrase, result.reply)


def test_partial_refund_cannot_dodge_the_approval_limit():
    a = ANCHORS["refund_manager"]                        # refund due $9,188.50: Support Manager territory
    desk = SupportDesk("travis.greer@midlandoil.example", db=scratch_db("t_partial.db"))
    content, is_error = desk.run("issue_refund", {"rma_id": a["rma_id"], "amount_usd": 2400, "reason": "x"})
    assert is_error and "approv" in content
    small = ANCHORS["refund_small"]
    desk2 = SupportDesk("luis.romero@keystone-mech.example", db=scratch_db("t_partial2.db"))
    content, is_error = desk2.run("issue_refund", {"rma_id": small["rma_id"], "amount_usd": 100, "reason": "x"})
    assert is_error and "Partial refunds need a person" in content


def test_not_found_and_not_yours_get_the_same_answer():
    stranger = SupportDesk("someone@mailbox.example", db=scratch_db("t_enum.db"))
    exists = stranger.run("get_order", {"order_id": ANCHORS["pending_strategic"]["order_id"]})
    missing = stranger.run("get_order", {"order_id": "SO-99999"})
    assert exists == missing and exists[1] is True and "not verified" in exists[0]


def test_reference_ids_stay_unique_under_concurrent_writers():
    import sqlite3
    import threading
    scratch_db("t_race.db").close()
    path = DATA_DIR.parent / ".runs" / "db" / "t_race.db"
    ids, lock = [], threading.Lock()

    def worker() -> None:
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        desk = SupportDesk("jorge.medina@greenvalley-coop.example", db=conn)
        for _ in range(5):
            content, is_error = desk.run("escalate_to_human", {"queue": "order_desk", "priority": "P3", "summary": "t"})
            assert not is_error, content
            with lock:
                ids.append(json.loads(content)["escalation_id"])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 20 and len(set(ids)) == 20


def test_default_desk_uses_a_private_database():
    a, b = SupportDesk("x@y.example"), SupportDesk("x@y.example")
    a.db.execute("DELETE FROM escalations")
    a.db.commit()
    assert a.db is not b.db and b.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] > 0


def test_agent_toolset_can_be_narrowed():
    client = get_client()
    read_only = [t for t in TOOLS if t["name"] not in ("create_rma", "issue_refund")]
    result = run_support_agent(client, "We over-ordered MS-250 seal kits on SO-10283. Can we return 4 unopened kits?",
                               "aisha.karim@harborfoods.example", tools=read_only,
                               desk=SupportDesk("aisha.karim@harborfoods.example", db=scratch_db("t_narrow.db")))
    assert "create_rma" not in {c["name"] for c in result.tool_calls}
    assert result.reply
