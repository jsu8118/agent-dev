"""Advanced capstone starter - check your progress, milestone by milestone.

    python run_starter_check.py                    # progress report for YOUR package (starter/recall)
    python run_starter_check.py --only M2          # iterate on one milestone
    python run_starter_check.py --strict           # exit 1 unless M1-M6 all pass (use it in your CI)
    python run_starter_check.py --impl reference   # the same checks against the reference solution

M1-M5 are focused checks with specific feedback; M6 runs the shared acceptance suite (reference/recall/evals.py:
three campaigns and seven gates) through your package. M7 (design document and operations memo) is reviewed by a
person, so it is only listed. Everything runs in mock mode; with an API key M3-M6 run against Claude.
"""
# test: expect=Milestone progress
# test: timeout=300

from __future__ import annotations

import argparse
import importlib
import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                          # day7_capstone/: `starter.recall` and `reference.recall`

from advanced.lib.durable import ApprovalRequired, Crash, ToolContext   # noqa: E402
from labkit import get_client, header, runs_dir, step                    # noqa: E402
from reference.recall import evals as acceptance                          # noqa: E402  (the shared suite)

MILESTONES = {
    "M1": "Inbound screening (security.py)",
    "M2": "Scoped, idempotent, guarded tools (tools.py)",
    "M3": "Priority plan and durable outreach (orchestrator.py)",
    "M4": "Reply handling: quarantine, stop conditions, holds (orchestrator.py)",
    "M5": "Budget, loop detection, circuit breaker, approvals, parts (budget.py, orchestrator.py)",
    "M6": "Acceptance suite: GO on every gate",
}
EXPECTED_ORDER = ["C-1002", "C-1014", "C-1001", "C-1005", "C-1012", "C-1016", "C-1025"]


class Impl:
    def __init__(self, base: str) -> None:
        self.base = base
        for name in ("config", "security", "tools", "store", "agents", "orchestrator", "budget"):
            setattr(self, name, importlib.import_module(f"{base}.{name}"))
        self.acceptance = acceptance.Impl.load(base)


def fresh(impl: Impl, name: str):
    base = runs_dir("advanced_capstone", "starter_check", impl.base.split(".")[0])
    for p in base.glob(name + "*"):
        p.unlink()
    return impl.store.CampaignStore(base / name).load()


# ------------------------------------------------------------------------------------ checks
def m1(impl: Impl, client) -> list[str]:
    problems = []
    replies = impl.orchestrator.load_replies()
    store = fresh(impl, "m1.db")
    kinds = {"fraud": ("fraud_redirect", "spoofed_sender"), "injection": ("injection",), "data_request": ("data_request",),
             "phishing": ("phishing",), "spoofed_internal": ("spoofed_internal",)}
    for r in replies:
        s = impl.security.screen_inbound(r, store.contacts(r["customer_id"]))
        if r["label"] in kinds:
            if not s.quarantine:
                problems.append(f"{r['reply_id']} ({r['label']}) must be quarantined; got {s.label}")
            elif not any(k in s.label for k in kinds[r["label"]]):
                problems.append(f"{r['reply_id']} quarantined for the wrong reason: {s.label} (expected one of {kinds[r['label']]})")
        elif r["label"] in ("safety_event", "legal"):
            if s.quarantine or r["label"] not in s.flags:
                problems.append(f"{r['reply_id']} must reach the agent with the flag {r['label']!r}; got {s.label}")
        else:
            if s.quarantine:
                problems.append(f"{r['reply_id']} is benign ({r['intent']}) but was quarantined: {s.label}")
            if r["intent"] == "out_of_office" and "out_of_office" not in s.flags:
                problems.append(f"{r['reply_id']}: an automatic reply should carry the out_of_office flag")
            if r["intent"] == "compensation_demand" and "compensation" not in s.flags:
                problems.append(f"{r['reply_id']}: a compensation demand should carry the compensation flag")
    lookalike = impl.security.sender_status("dana.whitfield@bluewater-utilites.example", {"bluewater-utilities.example"})
    if lookalike != "lookalike":
        problems.append(f"sender_status: 'bluewater-utilites.example' (one letter off) should be 'lookalike', got {lookalike!r}")
    return problems


def m2(impl: Impl, client) -> list[str]:
    problems = []
    store = fresh(impl, "m2.db")
    settings = impl.config.DEFAULT
    desk = impl.tools.CampaignDesk(store, settings, role="inbound", customer_id="C-1005", actor="check", run_id="check")
    r = desk.execute("get_contacts", {"customer_id": "C-1001"})
    if not (isinstance(r, dict) and "error" in r):
        problems.append("a run scoped to C-1005 read C-1001's contacts: _scope must refuse other customers")
    r = desk.execute("send_email", {"contact_id": "CT-1001-1", "template": "TPL-RC-05", "subject": "x", "body": "hello"})
    if not (isinstance(r, dict) and "error" in r):
        problems.append("send_email delivered to another customer's contact: _contact must allow only this customer's active contacts")
    ctx = ToolContext(run_id="check", store=store.runs, tool_use_id="t1", idempotency_key="check:t1")
    first = desk.execute("send_email", {"contact_id": "CT-1005-1", "template": "TPL-RC-05", "subject": "x", "body": "hello"}, ctx)
    second = desk.execute("send_email", {"contact_id": "CT-1005-1", "template": "TPL-RC-05", "subject": "x", "body": "hello"}, ctx)
    sent = store.messages("C-1005", "outbound")
    if len(sent) != 1 or first != second:
        problems.append(f"the same step delivered {len(sent)} time(s) / returned different results: delivery must be at-most-once per idempotency key")
    effect = store.runs.effect_begin("check:t1:send", "check", "send_email")
    if not (effect and effect.get("status") == "done"):
        problems.append("no 'done' effect record for the send: a resumed run in another process must find it and replay instead of re-sending")
    eu = next(s for s in store.slots(region="EU"))
    r = desk.execute("book_visit", {"slot_id": eu["slot_id"], "serials": ["KP250-2608-0004"], "contact_id": "CT-1005-1"})
    if not (isinstance(r, dict) and "region" in str(r.get("error", ""))):
        problems.append(f"booking an EU slot for a US-EAST site must be refused with a region error; got {r}")
    desk12 = impl.tools.CampaignDesk(store, settings, role="inbound", customer_id="C-1012", actor="check", run_id="check2")
    no_fw = next(s for s in store.slots(region="US-EAST") if "controller_firmware" not in s["skills"])
    r = desk12.execute("book_visit", {"slot_id": no_fw["slot_id"], "serials": ["KC2-2608-0001"], "contact_id": "CT-1012-1"})
    if not (isinstance(r, dict) and "skill" in str(r.get("error", ""))):
        problems.append(f"booking an engineer without controller_firmware for a KC-2 board swap must be refused; got {r}")
    r = desk.execute("mark_remediated", {"serial": "KP250-2608-0004", "evidence": "the customer said so"})
    if not (isinstance(r, dict) and "error" in r):
        problems.append("mark_remediated without a completed appointment must be refused")
    r = desk.execute("pause_campaign", {"scope": "all", "reason": "x"})
    if not (isinstance(r, dict) and "error" in r):
        problems.append("the inbound role must not be able to call pause_campaign (role allow-list)")
    return problems


def m3(impl: Impl, client) -> list[str]:
    problems = []
    store = fresh(impl, "m3.db")
    orch = impl.orchestrator.Orchestrator(store, impl.config.DEFAULT, client, replies=[])
    order = [c["customer_id"] for c in orch.plan()]
    if order != EXPECTED_ORDER:
        problems.append(f"plan() order {order} != {EXPECTED_ORDER} (risk class, then tier, then id)")
    store.schedule_retry("C-1025", "2026-12-01", "test")
    if "C-1025" in [c["customer_id"] for c in orch.plan()]:
        problems.append("plan() must skip a customer whose retry date is in the future")
    report = orch.run_day(1)
    if len(report.outreach) != 6 or not all(o.endswith("completed") for o in report.outreach):
        problems.append(f"day 1 should complete outreach for the 6 plannable customers; got {report.outreach}")
    crashed = fresh(impl, "m3_crash.db")
    orch2 = impl.orchestrator.Orchestrator(crashed, impl.config.DEFAULT, client, replies=[])
    try:
        orch2.run_day(1, crash={"customer": "C-1005", "crash_at": ("after_tool", 4)})
        problems.append("the simulated crash did not propagate: run_day must let Crash escape (the process is dead)")
    except Crash:
        pass
    report = orch2.run_day(1)
    if not report.resumed:
        problems.append("after a crash, run_day must resume the interrupted run first (report.resumed is empty)")
    from collections import Counter
    counts = Counter((m["customer_id"], m["address"]) for m in crashed.messages(direction="outbound") if m["template"] == "TPL-RC-01")
    dup = [k for k, n in counts.items() if n > 1]
    if dup or len(counts) != 7:
        problems.append(f"after crash + resume every customer must have exactly one notice; duplicates={dup}, notices={len(counts)}")
    return problems


def m4(impl: Impl, client) -> list[str]:
    problems = []
    store = fresh(impl, "m4.db")
    acceptance.run_campaign(impl.acceptance, store, impl.config.DEFAULT, client, 4)
    replies = {r["reply_id"]: r for r in impl.orchestrator.load_replies()}
    for rid, r in replies.items():
        if r["label"] in acceptance.HOSTILE:
            msg = next((m for m in store.messages(r["customer_id"], "inbound") if m["message_id"] == rid), None)
            if not (msg and msg["label"].startswith("quarantined")):
                problems.append(f"{rid} ({r['label']}) was not logged as quarantined")
            try:
                store.runs.get(f"inbound:{rid}")
                problems.append(f"{rid} ({r['label']}) reached the agent: a quarantined reply must never start a run")
            except KeyError:
                pass
            if not any(e["queue"] == "security" and rid in e["summary"] for e in store.escalations()):
                problems.append(f"{rid}: no security escalation")
    if "PS-2608-B" not in store.get("paused_lots", []):
        problems.append("RPL-018 (injury report) must pause lot PS-2608-B (stop condition, in code)")
    if not any(e["queue"] == "quality" and e["priority"] == "P1" for e in store.escalations()):
        problems.append("RPL-018 must raise a P1 quality escalation")
    c = store.customer("C-1012")
    if not c["status"].startswith("held"):
        problems.append(f"RPL-019 (legal notice) must put C-1012 on hold; status is {c['status']}")
    if any(u["status"] == "remediated" for u in store.units("C-1012")):
        problems.append("RPL-012: a unit was marked remediated on the customer's say-so")
    return problems


def m5(impl: Impl, client) -> list[str]:
    problems = []
    settings = impl.config.DEFAULT
    calls = []

    def flaky(name, tool_input, ctx):
        calls.append(name)
        return {"error": "boom"} if name == "bad" else {"ok": True}

    guard = impl.budget.Guard(flaky, settings)
    results = [guard("same", {"a": 1}, None) for _ in range(settings.loop_repeat_limit + 1)]
    if not (isinstance(results[-1], dict) and "loop" in str(results[-1].get("error", ""))):
        problems.append(f"loop detection: the {settings.loop_repeat_limit + 1}th identical call must return a 'loop detected' error")
    guard = impl.budget.Guard(flaky, settings)
    for i in range(settings.circuit_breaker_window):
        guard("bad" if i % 2 == 0 else "good", {"i": i}, None)
    after = guard("good", {"i": 99}, None)
    if not guard.open or not (isinstance(after, dict) and "circuit" in str(after.get("error", ""))):
        problems.append("circuit breaker: after half the window failed, the circuit must open and further calls return a 'circuit open' error")

    def pauser(name, tool_input, ctx):
        raise ApprovalRequired({"summary": "x"})
    try:
        impl.budget.Guard(pauser, settings)("credit", {}, None)
        problems.append("Guard must let ApprovalRequired propagate (a paused run is not a failure)")
    except ApprovalRequired:
        pass
    budget = impl.budget.SwarmBudget(0.30, carried_usd=0.31)
    try:
        budget.check(reserve_usd=0.6)
        problems.append("SwarmBudget.check must raise BudgetExceeded once the cap is reached")
    except impl.budget.BudgetExceeded:
        pass
    tiny = fresh(impl, "m5_tiny.db")
    reports = acceptance.run_campaign(impl.acceptance, tiny, settings.with_(model_spend_cap_usd=0.30), client, 2)
    if tiny.get("status") != "paused" or not any("budget" in s for r in reports for s in r.stop_conditions):
        problems.append("with a $0.30 cap the campaign must pause itself with a budget stop condition")
    store = fresh(impl, "m5_appr.db")
    orch = impl.orchestrator.Orchestrator(store, settings, client)
    orch.run_day(1); orch.run_day(2)
    waiting = store.runs.list(status="waiting_approval")
    if not waiting:
        problems.append("RPL-004's $750 goodwill credit must leave its run in waiting_approval (threshold $500)")
    else:
        run = waiting[0]
        a = [x for x in store.runs.approvals(run.id) if x["status"] == "pending"][0]
        store.runs.decide(a["approval_id"], approved=True, by="check:manager")
        outcome = impl.agents.resume(store, settings, client, run.id)
        credits = store.credits()
        if outcome.status != "completed" or not credits or credits[-1]["approved_by"] != "check:manager":
            problems.append(f"after approval the run must resume to completion and the credit must record the approver; got {outcome.status}, {credits}")
    store.conn().execute("UPDATE parts SET on_hand = 0 WHERE sku = 'KC-2-PSB'")
    report = impl.orchestrator.DayReport(day=9, date="2026-09-28")
    orch._stop_conditions(report)
    if "controller_board_replacement" not in store.get("paused_remedies", []):
        problems.append("parts at zero with units remaining must pause that remedy (_stop_conditions)")
    return problems


def m6(impl: Impl, client) -> list[str]:
    report = acceptance.run_suite(client, impl.config.DEFAULT, impl=impl.acceptance)
    problems = [f"{name}: {detail}" for name, (ok, detail) in report.gates.items() if not ok]
    problems += [f"{c.id}: {c.detail}" for c in report.checks if not c.ok]
    return problems


CHECKS: dict[str, Callable] = {"M1": m1, "M2": m2, "M3": m3, "M4": m4, "M5": m5, "M6": m6}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--impl", default="starter", choices=["starter", "reference"])
    parser.add_argument("--only", default=None)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    impl = Impl(f"{args.impl}.recall")
    client = get_client()
    header(f"Milestone progress - {args.impl}/recall")
    passed = 0
    for mid, title in MILESTONES.items():
        if args.only and args.only != mid:
            continue
        step(mid, title)
        try:
            problems = CHECKS[mid](impl, client)
        except Exception as exc:                                  # noqa: BLE001 - report, don't abort the other milestones
            problems = [f"crashed: {type(exc).__name__}: {exc}"]
            traceback.print_exc(limit=3)
        if problems:
            print(f"  FAIL ({len(problems)} problem{'s' if len(problems) > 1 else ''})")
            for p in problems[:12]:
                print(f"    - {p}")
        else:
            passed += 1
            print("  PASS")
    print("\n  M7: design document and operations memo - reviewed by a person (see README.md)")
    total = 1 if args.only else len(MILESTONES)
    print(f"\n{passed}/{total} milestones pass")
    return 0 if (passed == total or not args.strict) else 1


if __name__ == "__main__":
    sys.exit(main())
