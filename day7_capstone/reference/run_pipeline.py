"""Capstone reference - run the Service Desk Copilot on inbound tickets and show what it did.

    python run_pipeline.py                     # a sample of 9 tickets covering every route
    python run_pipeline.py --ticket T-1301     # one ticket, with its full trace tree
    python run_pipeline.py --all               # all 62 tickets of the support inbox
    python run_pipeline.py --shadow            # rollout stage 1: every agent reply is held for review

Each ticket prints the screen result, triage, route, actions (tool calls), quality alerts, the
reply (or why it was held) and cost/latency. The summary is the operations view: route mix,
human workload (review + escalations), alerts, and cost against the $0.40/ticket budget.
In mock mode the tickets are answered by labkit's rule-based stand-ins; run live for real behaviour.
"""
# test: expect=Route mix
# test: timeout=180

from __future__ import annotations

import argparse
import dataclasses
from collections import Counter

from copilot.config import DEFAULT
from copilot.evals import p95
from copilot.pipeline import handle_email
from copilot.triage import InboundEmail
from labkit import get_client, header, is_mock, step, wrap
from labkit.data import load_jsonl, scratch_db
from labkit.tracing import Tracer

SAMPLE = ["T-1001", "T-1301", "T-1304", "T-1207", "T-1006", "T-1007", "T-1208", "T-1703", "T-1801"]


def show(ticket: dict, outcome) -> None:
    t = outcome.triage or {}
    s = outcome.screen
    print(f"\n{ticket['ticket_id']}  {ticket['from_email']}  \"{ticket['subject']}\"")
    print(f"  screen   : safety={s['safety_hits'] or '-'}  security={s['security_flags'] or '-'}  "
          f"serials={s['serials'] or '-'}")
    print(f"  triage   : " + (f"{t['category']}/{t['priority']}  requires_human={t['requires_human']}  "
                              f"lang={t['language']}" if t else "(not run: quarantined, or unavailable)"))
    print(f"  route    : {outcome.route} -> {outcome.disposition.upper()}   because {', '.join(outcome.reasons)}")
    calls = [c["name"] + ("(x)" if c["is_error"] else "") for c in outcome.tool_calls]
    print(f"  actions  : {', '.join(calls) or '-'}")
    for e in outcome.escalations:
        print(f"  escalated: {e['escalation_id']} -> {e['queue']} {e['priority']}")
    for a in outcome.quality_alerts:
        print(f"  QUALITY  : lot {a['lot']} ({a['incident']}) serials={a['serials']} symptom='{a['symptom']}'")
    for g in outcome.guard_issues:
        print(f"  guard    : {g['check']}: {g['detail']}")
    if outcome.error:
        print(f"  ERROR    : {outcome.error}")
    print("  reply    :" + ("\n" + wrap(outcome.reply, "    ") if outcome.reply else " (none sent)"))
    print(f"  cost     : ${outcome.cost_usd:.4f}  latency {outcome.latency_s:.2f}s  llm_calls={outcome.llm_calls}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticket", action="append", help="ticket ID (repeatable)")
    parser.add_argument("--all", action="store_true", help="process the whole inbox (62 tickets)")
    parser.add_argument("--shadow", action="store_true", help="shadow mode: hold every agent reply for review")
    args = parser.parse_args()
    config = dataclasses.replace(DEFAULT, auto_send=False) if args.shadow else DEFAULT

    inbox = {t["ticket_id"]: t for t in load_jsonl("support", "tickets.jsonl")}
    ids = list(inbox) if args.all else (args.ticket or SAMPLE)
    client = get_client()
    db = scratch_db("capstone_run.db")        # one working copy of the ERP for the whole run, like production

    header("Kestrel Service Desk Copilot - processing the inbox")
    print(f"triage: {config.triage_model} (effort={config.triage_effort})   agent: {config.agent_model}   "
          f"tickets: {len(ids)}   auto_send: {config.auto_send}")
    outcomes, tracers = [], {}
    for ticket_id in ids:
        ticket = inbox[ticket_id]
        tracer = Tracer("copilot")
        outcome = handle_email(client, InboundEmail(ticket["from_email"], ticket["subject"], ticket["body"],
                                                    ticket_id), config=config, db=db, tracer=tracer)
        outcomes.append(outcome)
        tracers[ticket_id] = tracer
        show(ticket, outcome)

    step("Summary", "Route mix, human workload, quality alerts, cost")
    n = len(outcomes)
    print("Route mix       : " + ", ".join(f"{k}={v}" for k, v in Counter(o.route for o in outcomes).most_common()))
    print("Dispositions    : " + ", ".join(f"{k}={v}" for k, v in Counter(o.disposition for o in outcomes).most_common()))
    queues = Counter(f"{e['queue']}/{e['priority']}" for o in outcomes for e in o.escalations)
    print("Escalations     : " + (", ".join(f"{k}={v}" for k, v in queues.most_common()) or "none"))
    human = sum(1 for o in outcomes if o.disposition != "sent" or o.escalations)
    print(f"Human touch     : {human}/{n} tickets ({human / n:.0%}) need a person (review, quarantine or escalation)")
    alerts = [a for o in outcomes for a in o.quality_alerts]
    print(f"Quality alerts  : {len(alerts)} " + (f"({', '.join(sorted({a['lot'] for a in alerts}))})" if alerts else ""))
    mean = sum(o.cost_usd for o in outcomes) / n
    print(f"Cost            : total ${sum(o.cost_usd for o in outcomes):.4f}, mean ${mean:.4f}/ticket "
          f"(budget ${DEFAULT.max_cost_per_ticket_usd:.2f}){'  [simulated]' if is_mock() else ''}")
    print(f"Latency         : p95 {p95([o.latency_s for o in outcomes]):.2f}s (budget {DEFAULT.max_p95_latency_s:.0f}s)"
          f"{'  [mock: not meaningful]' if is_mock() else ''}")

    shown = ids[0] if args.ticket else ("T-1301" if "T-1301" in tracers else ids[0])
    step("Trace", f"{shown}: every step, LLM call and tool call, with tokens and cost")
    print(tracers[shown].render_tree())
    print(f"(exported to {tracers[shown].export()})")


if __name__ == "__main__":
    main()
