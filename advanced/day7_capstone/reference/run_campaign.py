"""Capstone reference - run the Recall Campaign Orchestrator for a number of business days.

    python run_campaign.py --fresh --days 5                  # the campaign from the issue date, day by day
    python run_campaign.py --fresh --days 2 --crash          # day 1 dies mid-outreach; run again (without --fresh) to resume
    python run_campaign.py --days 3                          # continue an existing campaign (state is in .runs/)
    python run_campaign.py --fresh --days 5 --decide approve # a support manager approves pending credits at the end of each day
    python run_campaign.py --fresh --days 5 --cap 0.40       # a tiny model budget: watch the campaign pause itself
    python run_campaign.py --fresh --days 5 --shadow         # every outbound email held for review (rollout stage 1)

Each day prints what the coordinator did: outreach runs (most urgent customers first), replies screened and
handled (or quarantined), reminders, stop conditions, approvals waiting, and the spend. The final dashboard is
the operations view: units and customers by status, SLA compliance, escalations by queue, and the audit trail
size. In mock mode the agents are labkit's rule-based stand-ins; run live for real behaviour.
"""
# test: args=--fresh --days 5 --decide approve --db campaign_test.db
# test: expect=Campaign dashboard
# test: expect=SLA status
# test: timeout=240

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from advanced.lib.durable import Crash                       # noqa: E402
from labkit import LEDGER, get_client, header, is_mock, runs_dir, step, wrap   # noqa: E402
from recall import agents                                     # noqa: E402
from recall.budget import SwarmBudget                         # noqa: E402
from recall.config import DEFAULT                             # noqa: E402
from recall.orchestrator import Orchestrator, sla_status      # noqa: E402
from recall.store import CampaignStore                        # noqa: E402


def print_report(r) -> None:
    step(r.day, f"Business day {r.day} ({r.date})" + ("  [campaign paused]" if r.paused else ""))
    if r.resumed:
        print("  resumed runs  : " + ", ".join(r.resumed))
    print("  outreach      : " + (", ".join(r.outreach) or "-"))
    for e in r.replies:
        tail = {"quarantined": f"QUARANTINED -> {e.get('escalation')}  ({'; '.join(e['reasons'])})",
                "routed_to_hold_owner": f"routed to hold owner -> {e.get('escalation')}",
                "agent": f"{e.get('status')}  tools={e.get('tools')}  {e.get('summary', '')}"}[e["action"]]
        print(f"  reply {e['reply_id']} {e['customer_id']} [{e['label']}]: {tail}")
        if e.get("approval_id"):
            print(f"        waiting for approval {e['approval_id']}  (python run_ops.py --approve {e['approval_id']} --by <name>)")
    if r.reminders:
        print("  reminders     : " + ", ".join(r.reminders))
    for s in r.stop_conditions:
        print("  STOP CONDITION: " + s)
    for t in r.guard_trips:
        print("  guard tripped : " + str(t))
    print(f"  spend so far  : ${r.spend_usd:.4f}")


def dashboard(store: CampaignStore) -> None:
    header("Campaign dashboard")
    s = store.summary()
    print(f"  as of {s['today']}   status={s['status']}   paused lots={s['paused_lots'] or '-'}   paused remedies={s['paused_remedies'] or '-'}")
    print(f"  units     : {dict(sorted(s['units'].items()))}")
    print(f"  customers : {dict(sorted(s['customers'].items()))}")
    print(f"  outbound sent={s['outbound_sent']} held={s['outbound_held']}   inbound={s['inbound']} quarantined={s['inbound_quarantined']}   "
          f"appointments={s['appointments']}   credits=${s['credits_usd']:.2f}   pending approvals={s['pending_approvals']}")
    queues = Counter(e["queue"] + "/" + e["priority"] for e in store.escalations())
    print(f"  escalations: {dict(sorted(queues.items())) or '-'}")
    print(f"  audit rows : {len(store.audit_rows())}   runs: {dict(Counter(r.status for r in store.runs.list()))}")
    if s["paused_lots"] or s["paused_remedies"] or s["status"] == "paused":
        print("  NOTE: a stop condition is holding part of the campaign; after the quality review, lift it with "
              "run_ops.py --resume-lot <lot> / --resume-remedy <remedy> / --resume-campaign")
    print("\n  SLA status")
    print(f"  {'customer':<8} {'risk':<10} {'status':<11} {'contact by':<11} {'contacted':<11} {'ok':<4} {'remedy by':<11} {'scheduled':<9} {'visit':<11} ok")
    for row in sla_status(store):
        print(f"  {row['customer_id']:<8} {row['risk_class']:<10} {row['status']:<11} {row['contact_deadline']:<11} {row['contacted'] or '-':<11} "
              f"{'yes' if row['contact_ok'] else ('LATE' if row['contact_overdue'] else 'no'):<4} {row['remedy_deadline']:<11} {row['units_scheduled']:<9} "
              f"{row['visit'] or '-':<11} {'yes' if row['remedy_ok'] else 'no'}{('  hold: ' + row['hold']) if row['hold'] else ''}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--fresh", action="store_true", help="start from the issue date with an empty store")
    parser.add_argument("--crash", action="store_true", help="simulate the worker dying during day 1's outreach")
    parser.add_argument("--decide", choices=["approve", "reject"], help="decide pending approvals at the end of each day (as a manager)")
    parser.add_argument("--cap", type=float, default=DEFAULT.model_spend_cap_usd, help="model spend cap in USD")
    parser.add_argument("--shadow", action="store_true", help="hold every outbound email for review")
    parser.add_argument("--db", default="campaign.db")
    args = parser.parse_args()

    base = runs_dir("advanced_capstone", "reference")
    path = base / args.db
    if args.fresh:
        for p in base.glob(args.db + "*"):
            p.unlink()
    settings = DEFAULT.with_(model_spend_cap_usd=args.cap, auto_send=not args.shadow)
    store = CampaignStore(path).load()
    client = get_client()
    header("Recall Campaign Orchestrator - RC-2026-03" + (" (mock mode: rule-based stand-ins for the agents)" if is_mock() else ""))
    print(wrap(f"{len(store.units())} affected units at {len(store.customers())} customers; budget cap ${args.cap:.2f}; "
               f"{'shadow mode: outbound held' if args.shadow else 'auto-send on'}; state in {path}"))
    orchestrator = Orchestrator(store, settings, client, budget=SwarmBudget(args.cap, carried_usd=store.get("spend_usd", 0.0)))
    start_day = store.get("last_day", 0) + 1
    crash = {"day": 1, "customer": "C-1005", "crash_at": ("after_tool", 4)} if args.crash else None
    for day in range(start_day, start_day + args.days):
        try:
            report = orchestrator.run_day(day, crash=crash if crash and crash["day"] == day else None)
        except Crash as exc:
            print(f"\n  *** worker crashed on day {day}: {exc}")
            print("  *** the notice to C-1005 was already delivered; run this script again (without --fresh) to resume the day:\n"
                  "      the log shows the send, so the resumed run replays it instead of sending twice.")
            dashboard(store)
            return 0
        print_report(report)
        store.set("last_day", day)
        if args.decide:
            for run in store.runs.list(status="waiting_approval"):
                pending = [a for a in store.runs.approvals(run.id) if a["status"] == "pending"]
                for a in pending:
                    store.runs.decide(a["approval_id"], approved=args.decide == "approve", by="lena.ortiz (support manager)",
                                      note="within the campaign's goodwill policy" if args.decide == "approve" else "not this time")
                    outcome = agents.resume(store, settings, client, run.id)
                    print(f"  approval {a['approval_id']} {args.decide}d -> run {run.id} {outcome.status}: {outcome.reply}")
    dashboard(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
