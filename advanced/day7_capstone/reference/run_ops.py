"""Capstone reference - the operations console for a running campaign.

    python run_ops.py                                   # dashboard: units, customers, SLA, approvals, stuck runs, holds
    python run_ops.py --approvals                       # what is waiting for a person, with the action and the amount
    python run_ops.py --approve apr_xxx --by "lena.ortiz"   # decide, then resume the paused run (any process can do this)
    python run_ops.py --reject apr_xxx --by "lena.ortiz" --note "over policy"
    python run_ops.py --stuck                           # runs whose worker died (expired lease) and how to resume them
    python run_ops.py --resume inbound:RPL-004          # resume one run
    python run_ops.py --replay outreach:C-1001          # the run's event log: every model turn and tool call, in order
    python run_ops.py --forensics RPL-014               # who told the agent what: the reply, its screening, escalations, audit rows
    python run_ops.py --resume-lot PS-2608-B            # lift a stop condition after the quality review

Everything here reads and writes the same SQLite file the orchestrator uses, from a different process: that is
the point of durable runs - a manager's decision or an operator's resume does not need the worker that started
the run to be alive.
"""
# test: args=--db campaign_test.db
# test: expect=Operations console

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from labkit import get_client, header, runs_dir, step, wrap   # noqa: E402
from recall import agents                                    # noqa: E402
from recall.config import DEFAULT                            # noqa: E402
from recall.orchestrator import sla_status                   # noqa: E402
from recall.store import CampaignStore                       # noqa: E402


def approvals(store: CampaignStore) -> list[tuple]:
    out = []
    for run in store.runs.list(status="waiting_approval"):
        for a in store.runs.approvals(run.id):
            if a["status"] == "pending":
                out.append((run.id, a))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="campaign.db")
    parser.add_argument("--approvals", action="store_true")
    parser.add_argument("--approve"); parser.add_argument("--reject"); parser.add_argument("--by", default="operator")
    parser.add_argument("--note", default="")
    parser.add_argument("--stuck", action="store_true")
    parser.add_argument("--resume"); parser.add_argument("--replay"); parser.add_argument("--forensics")
    parser.add_argument("--resume-lot"); parser.add_argument("--resume-remedy"); parser.add_argument("--resume-campaign", action="store_true")
    args = parser.parse_args()
    path = runs_dir("advanced_capstone", "reference") / args.db
    if not path.exists():
        print(f"No campaign state at {path}. Run run_campaign.py --fresh first.")
        return 0
    store = CampaignStore(path).load()
    header("Operations console - RC-2026-03")

    if args.approve or args.reject:
        approval_id = args.approve or args.reject
        run = store.runs.decide(approval_id, approved=bool(args.approve), by=args.by, note=args.note)
        print(f"  {approval_id}: {'approved' if args.approve else 'rejected'} by {args.by}; run {run.id} is {run.status}")
        outcome = agents.resume(store, DEFAULT, get_client(), run.id)
        print(f"  resumed {run.id} -> {outcome.status}: {outcome.reply}")
        return 0
    if args.resume:
        outcome = agents.resume(store, DEFAULT, get_client(), args.resume)
        print(f"  {args.resume} -> {outcome.status}: {outcome.reply}")
        return 0
    if args.resume_lot or args.resume_remedy or args.resume_campaign:
        if args.resume_lot:
            store.set("paused_lots", [l for l in store.get("paused_lots", []) if l != args.resume_lot])
        if args.resume_remedy:
            store.set("paused_remedies", [r for r in store.get("paused_remedies", []) if r != args.resume_remedy])
        if args.resume_campaign:
            store.set("status", "active")
        store.audit(args.by, "campaign.resumed", args.resume_lot or args.resume_remedy or "all", {"note": args.note})
        print(f"  stop condition lifted; paused lots={store.get('paused_lots')} remedies={store.get('paused_remedies')} status={store.get('status')}")
        return 0
    if args.replay:
        step(1, f"Event log of {args.replay}")
        for e in store.runs.events(args.replay):
            body = {k: v for k, v in e.items() if k not in ("seq", "type", "at")}
            if e["type"] == "model.response":
                body = {"turn": e["turn"], "stop_reason": e["stop_reason"],
                        "blocks": [b.get("name") or b.get("type") for b in e["content"]], "input_tokens": e["usage"].get("input_tokens")}
            print(f"  {e['seq']:>4} {e['at'][11:19]} {e['type']:<20} {json.dumps(body, default=str)[:150]}")
        return 0
    if args.forensics:
        step(1, f"Forensics for {args.forensics}")
        msgs = [m for m in store.messages() if m["message_id"] == args.forensics]
        for m in msgs:
            print(f"  inbound from {m['address']} at {m['at']}  label={m['label']}\n" + wrap(m["body"][:400], "    "))
        for e in store.escalations():
            if args.forensics in e["summary"] or e["escalation_id"].endswith(args.forensics):
                print(f"  escalation {e['escalation_id']} -> {e['queue']}/{e['priority']}: {e['summary']}")
        try:
            events = store.runs.events(f"inbound:{args.forensics}")
            print(f"  agent run inbound:{args.forensics}: {[e['type'] for e in events]}")
            for e in events:
                if e["type"] == "tool.started":
                    print(f"    tool {e['name']} {json.dumps(e['input'])[:120]}")
        except KeyError:
            print("  no agent run: the message never reached a model")
        for row in store.audit_rows(args.forensics):
            print(f"  audit {row['ts'][11:19]} {row['actor']:<14} {row['action']:<22} {row['target']} {row['details'][:100]}")
        return 0

    s = store.summary()
    print(f"  as of {s['today']}  status={s['status']}  paused lots={s['paused_lots'] or '-'}  paused remedies={s['paused_remedies'] or '-'}  "
          f"spend=${store.get('spend_usd', 0.0):.4f}")
    print(f"  units={dict(sorted(s['units'].items()))}  customers={dict(sorted(s['customers'].items()))}  appointments={s['appointments']}  "
          f"escalations={s['escalations']}  credits=${s['credits_usd']:.2f}")
    step(1, "Approvals waiting for a person")
    pending = approvals(store)
    for run_id, a in pending:
        print(f"  {a['approval_id']}  run={run_id}  {a['action'].get('summary')}  (approver: {a['action'].get('approver_role')})")
    if not pending:
        print("  none")
    step(2, "Stuck runs (worker died: lease expired while running)")
    stuck = [r for r in store.runs.stuck(older_than_s=0) if r.kind != "coordinator"]
    for r in stuck:
        print(f"  {r.id}  {r.kind}  lease_owner={r.lease_owner}  -> python run_ops.py --resume {r.id}")
    if not stuck:
        print("  none")
    step(3, "SLA status")
    for row in sla_status(store):
        print(f"  {row['customer_id']} {row['risk_class']:<10} {row['status']:<11} contact {'ok' if row['contact_ok'] else 'LATE' if row['contact_overdue'] else 'pending'} "
              f"by {row['contact_deadline']}; remedy {row['units_scheduled']} by {row['remedy_deadline']} {'ok' if row['remedy_ok'] else 'pending'}"
              f"{'  hold: ' + row['hold'] if row['hold'] else ''}")
    step(4, "Escalations by queue")
    for e in store.escalations():
        print(f"  {e['escalation_id']:<22} {e['queue']}/{e['priority']}  {e['customer_id']}  {e['summary'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
