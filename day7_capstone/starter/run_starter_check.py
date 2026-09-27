"""Capstone starter - check your progress, milestone by milestone.

    python run_starter_check.py                 # progress report for YOUR package (starter/copilot)
    python run_starter_check.py --strict        # exit 1 unless M1-M6 all pass (use it in your CI)
    python run_starter_check.py --impl reference  # the same checks against the reference solution

M1-M5 are fast unit-level checks with specific feedback; M6 runs the full acceptance suite (the
same 40 scenarios and gates as reference/run_evals.py) through your handle_email. M7 (design doc
and go-live memo) is reviewed by a person, so it's only listed.
"""
# test: expect=Milestone progress
# test: timeout=240

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                  # day7_capstone/: makes `reference.copilot` importable

from labkit import get_client, header, runs_dir, step  # noqa: E402
from labkit.data import load_jsonl, scratch_db       # noqa: E402
from reference.copilot import evals as acceptance    # noqa: E402  (the shared acceptance suite)

MILESTONES = {
    "M1": "Input screen (security + safety backstop)",
    "M2": "Routing gate and deterministic handlers",
    "M3": "Pipeline: handle_email end to end",
    "M4": "Quality-hold detector",
    "M5": "Output guard",
    "M6": "Acceptance suite: GO on all gates",
}


class Impl:
    def __init__(self, base: str) -> None:
        self.base = base
        for name in ("config", "screen", "gate", "pipeline", "output_guard", "triage"):
            setattr(self, name, importlib.import_module(f"{base}.{name}"))


# ------------------------------------------------------------------------------------ checks
def m1(impl: Impl, client) -> list[str]:
    problems = []
    tickets = load_jsonl("support", "tickets.jsonl")
    labels = {r["ticket_id"]: r for r in load_jsonl("support", "ticket_labels.jsonl")}
    flagged, caught, false_alarms = set(), set(), set()
    for t in tickets:
        s = impl.screen.screen_email(t["from_email"], t["subject"], t["body"])
        if s.security_flags:
            flagged.add(t["ticket_id"])
        if s.safety_hits:
            (caught if labels[t["ticket_id"]]["priority"] == "P1" else false_alarms).add(t["ticket_id"])
    p1 = {tid for tid, l in labels.items() if l["priority"] == "P1"}
    if caught != p1:
        problems.append(f"safety backstop missed P1 tickets {sorted(p1 - caught)}")
    if false_alarms:
        problems.append(f"safety backstop fired on non-P1 tickets {sorted(false_alarms)} (read them: why?)")
    expected = {"T-1208", "T-1507", "T-1703"}
    if flagged != expected:
        missing, extra = sorted(expected - flagged), sorted(flagged - expected)
        problems.append(f"security flags: missing {missing}, unexpected {extra}")
    extra_cases = {c["id"]: c for c in acceptance.load_cases(support=False)}
    c04, c05 = extra_cases["C04"], extra_cases["C05"]
    if not impl.screen.screen_email(c04["from_email"], c04["subject"], c04["message"]).security_flags:
        problems.append("C04: lookalike domain bluewater-utilitles.example not flagged")
    if not impl.screen.screen_email(c05["from_email"], c05["subject"], c05["message"]).safety_hits:
        problems.append("C05: smoke reported in Spanish ('humo') not caught by the backstop")
    s = impl.screen.screen_email("a@x.example", "", "Serial KP250-2608-0002 on SO-10243")
    if s.serials != ["KP250-2608-0002"] or s.order_ids != ["SO-10243"]:
        problems.append(f"identifiers: expected serials/order IDs to be extracted, got {s.serials} {s.order_ids}")
    return problems


def m2(impl: Impl, client) -> list[str]:
    problems = []
    S, T, decide = impl.screen.ScreenResult, impl.triage.TicketTriage, impl.gate.decide_route

    def tri(**kw):
        base = dict(category="order_status", priority="P3", product_line="pump", order_id=None, sentiment="neutral",
                    requires_human=False, language="en", summary="x")
        return T(**{**base, **kw})

    table = [
        ("safety hit, no triage", S(safety_hits=["fire_smoke"]), None, "safety", None),
        ("triage P1 only", S(), tri(category="safety_incident", priority="P1", requires_human=True), "safety", None),
        ("security flag", S(security_flags=["note to the AI"]), tri(), "security", None),
        ("safety AND security", S(safety_hits=["hazardous_leak"], security_flags=["x"]), None, "safety", None),
        ("triage unavailable", S(), None, "human", None),
        ("requires_human", S(), tri(requires_human=True), "agent", True),
        ("routine", S(), tri(), "agent", False),
    ]
    for label, screen, triage, route, review in table:
        d = decide(screen, triage)
        if d.route != route or (review is not None and d.review != review):
            problems.append(f"decide_route({label}): got route={d.route} review={d.review}, expected {route}"
                            + (f" review={review}" if review is not None else ""))
    both = decide(S(safety_hits=["hazardous_leak"], security_flags=["x"]), None)
    if not both.also_flag_security:
        problems.append("safety AND security: also_flag_security should be True")

    Email = impl.triage.InboundEmail
    desk = impl.pipeline.CopilotDesk("brian.foster@westfield-health.example", db=scratch_db("starter_m2.db"))
    reply = impl.gate.handle_safety(desk, Email("brian.foster@westfield-health.example", "Smoke", "Smoke from KC-1."),
                                    S(safety_hits=["fire_smoke"]), None, decide(S(safety_hits=["fire_smoke"]), None))
    esc = [c for c in desk.calls if c["name"] == "escalate_to_human" and not c["is_error"]]
    if not esc or esc[0]["input"].get("queue") != "field_service" or esc[0]["input"].get("priority") != "P1":
        problems.append("handle_safety: expected escalate_to_human(queue=field_service, priority=P1) via desk.run")
    for phrase in ("1 hour", "lockout"):
        if phrase not in reply.lower():
            problems.append(f"handle_safety reply should mention '{phrase}' (SOP-SUP-007 s.2)")
    desk = impl.pipeline.CopilotDesk("ap@bluewater-utilitles.example", db=scratch_db("starter_m2b.db"))
    reply = impl.gate.handle_security(desk, Email("ap@bluewater-utilitles.example", "Vendor", "Remittance?"),
                                      S(security_flags=["lookalike domain bluewater-utilitles.example ~ "
                                                        "bluewater-utilities.example"]))
    if reply.strip():
        problems.append("handle_security: no reply should go to an impersonating domain")
    if not any(c["input"].get("queue") == "security" for c in desk.calls if c["name"] == "escalate_to_human"):
        problems.append("handle_security: expected an escalation to the security queue")
    desk = impl.pipeline.CopilotDesk("jorge.medina@greenvalley-coop.example", db=scratch_db("starter_m2c.db"))
    reply = impl.gate.handle_human(desk, Email("jorge.medina@greenvalley-coop.example", "Q", "Question"), "test")
    if "1 business day" not in reply:
        problems.append("handle_human: the holding reply should promise a reply within 1 business day")
    return problems


def _run(impl: Impl, client, case_id: str):
    case = {c["id"]: c for c in acceptance.load_cases()}[case_id]
    email = impl.triage.InboundEmail(case["from_email"], case.get("subject", ""), case["message"], case_id)
    out = impl.pipeline.handle_email(client, email, db=scratch_db(f"starter_{case_id}.db"), publish=False)
    if out.error and out.error.startswith("NotImplementedError"):
        raise NotImplementedError(out.error.split(": ", 1)[-1])
    return out


def m3(impl: Impl, client) -> list[str]:
    problems = []
    for case_id, route, disposition, needle in (("E03", "agent", "sent", "NLF9028150109"),
                                                ("E17", "safety", "sent", "1 hour"),
                                                ("E20", "security", "quarantined", "")):
        out = _run(impl, client, case_id)
        if out.error:
            problems.append(f"{case_id}: pipeline error {out.error}")
            continue
        if (out.route, out.disposition) != (route, disposition):
            problems.append(f"{case_id}: got {out.route}/{out.disposition}, expected {route}/{disposition}")
        if needle and needle not in out.reply:
            problems.append(f"{case_id}: reply should contain {needle!r}")
        if case_id == "E20" and out.llm_calls:
            problems.append("E20: a quarantined email should never reach a model (llm_calls > 0)")
    return problems


def m4(impl: Impl, client) -> list[str]:
    problems = []
    for case_id, lots in (("E09", ["PS-2608-B"]), ("E30", ["VD-2607-C"]), ("C02", ["PS-2608-B"]), ("C03", []),
                          ("E03", [])):
        out = _run(impl, client, case_id)
        got = sorted({a["lot"] for a in out.quality_alerts})
        if out.error:
            problems.append(f"{case_id}: pipeline error {out.error}")
        elif got != lots:
            problems.append(f"{case_id}: quality alerts {got}, expected {lots}")
    return problems


def m5(impl: Impl, client) -> list[str]:
    problems = []
    db = scratch_db("starter_m5.db")
    samples = [
        ("foreign_order_ids", "luis.romero@keystone-mech.example", "Order SO-10306 for Orion ships on the 22nd."),
        ("third_party_contact", "luis.romero@keystone-mech.example", "Please write to mei.chen@orion-semi.example."),
        ("payment_data", "luis.romero@keystone-mech.example", "Pay to IBAN DE89 3704 0044 0532 0130 00."),
        ("internal_quality_info", "travis.greer@midlandoil.example", "Your seal is from lot PS-2608-B, on quality hold."),
        ("liability_or_promise", "travis.greer@midlandoil.example", "We accept full responsibility for the damage."),
    ]
    for check, sender, reply in samples:
        issues = impl.output_guard.review_reply(reply, from_email=sender, email_text="", tool_calls=[], db=db)
        if check not in {i.check for i in issues}:
            problems.append(f"review_reply should raise '{check}' for: {reply!r}")
    clean = impl.output_guard.review_reply("Your return is approved under RMA-7023 for order SO-10272.",
                                           from_email="travis.greer@midlandoil.example", email_text="",
                                           tool_calls=[], db=db)
    if clean:
        problems.append(f"a clean reply was flagged: {[i.check for i in clean]} (false positives cost reviewers' time)")
    return problems


def m6(impl: Impl, client) -> list[str]:
    results = acceptance.run_suite(client, acceptance.load_cases(), config=impl.config.DEFAULT,
                                   handle=impl.pipeline.handle_email)
    unfinished = [c["detail"] for r in results for c in r.failed_checks
                  if c["name"] == "no_pipeline_error" and c["detail"].startswith("NotImplementedError")]
    if unfinished:
        raise NotImplementedError(f"{len(unfinished)} scenarios hit unfinished code, e.g. {unfinished[0]}")
    rows = acceptance.acceptance(results, impl.config.DEFAULT)
    md, _ = acceptance.write_report(results, rows, runs_dir("capstone_starter"))
    failed = [f"{r['criterion']}: {r['value']} (target {r['target']})" for r in rows if r["passed"] is False]
    if failed:
        failed.append(f"full report: {md}")
    return failed


CHECKS: dict[str, Callable] = {"M1": m1, "M2": m2, "M3": m3, "M4": m4, "M5": m5, "M6": m6}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--impl", choices=["starter", "reference"], default="starter")
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every milestone passes")
    parser.add_argument("--only", nargs="*", help="run only these milestones, e.g. --only M1 M2")
    args = parser.parse_args()

    impl = Impl("copilot" if args.impl == "starter" else "reference.copilot")
    client = get_client()
    header(f"Capstone milestone check - {args.impl} implementation ({impl.base})")
    status: dict[str, tuple[str, list[str]]] = {}
    for key, check in CHECKS.items():
        if args.only and key not in args.only:
            continue
        try:
            problems = check(impl, client)
            status[key] = ("PASS" if not problems else "FAIL", problems)
        except NotImplementedError as exc:
            status[key] = ("TODO", [f"not implemented yet ({exc})"])
        except Exception as exc:                                     # a bug in your code: show where
            tb = traceback.extract_tb(exc.__traceback__)[-1]
            status[key] = ("ERROR", [f"{type(exc).__name__}: {exc} (at {Path(tb.filename).name}:{tb.lineno})"])

    step("Result", "Milestone progress")
    for key, (verdict, problems) in status.items():
        print(f"  {key} {MILESTONES[key]:<44} {verdict}")
        for p in problems[:8]:
            print(f"       - {p}")
    print(f"  M7 {'Design doc + go-live memo (reviewed by a person)':<44} see README: deliverables")
    done = sum(v == "PASS" for v, _ in status.values())
    print(f"\n{done}/{len(status)} milestones pass.")
    if args.strict and done < len(status):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
