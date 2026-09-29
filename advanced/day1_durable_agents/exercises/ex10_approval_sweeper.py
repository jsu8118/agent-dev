"""Exercise 10 starter - an approval sweeper that two cron hosts can run at the same time.

Lab 05's sweeper escalates approvals nobody picked up after 4 hours and expires the ones nobody decided after
2 days. In production it runs on a schedule on two hosts (for availability), so two sweeps can read the same
state before either writes. Make it safe WITHOUT editing advanced/lib/durable.py:

  * each action (escalate an approval, expire an approval) happens at most once, whichever host gets there first;
  * the page (the notification that wakes the finance director) is keyed, so a retry cannot page twice;
  * an approval a person decided between a sweeper's plan and its apply is left alone - no expiry, no page;
  * expiry is recorded as expiry ("expired: ..."), not as a manager's refusal.

The harness below parks two refund runs, lets two hosts plan and then apply at +4 hours and at +2 days (a
manager approves the second run between the two phases), then dispatches the decided runs. It uses the naive
plan/apply below and prints what that does.
Run: python advanced/day1_durable_agents/exercises/ex10_approval_sweeper.py
Solution: advanced/day1_durable_agents/solutions/ex10_approval_sweeper.py
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import RunStore  # noqa: E402
from kestrel.support_tools import SupportDesk  # noqa: E402
from labkit import get_client, header  # noqa: E402
from labkit.data import memory_db  # noqa: E402

import _day1 as d1  # noqa: E402

ESCALATE_AFTER = dt.timedelta(hours=4)
EXPIRE_AFTER = dt.timedelta(days=2)


@dataclass
class Pager:
    """The paging service (a real side effect). Like any decent notification API it honours an idempotency key."""

    pages: list[dict] = field(default_factory=list)
    by_key: dict[str, dict] = field(default_factory=dict)

    def page(self, to: str, text: str, *, run_id: str, key: str | None = None) -> dict:
        if key and key in self.by_key:
            return self.by_key[key]
        page = {"page_id": f"PG-{len(self.pages) + 1:03d}", "to": to, "text": text, "run_id": run_id}
        self.pages.append(page)
        if key:
            self.by_key[key] = page
        return page

    def find(self, key: str) -> dict | None:
        return self.by_key.get(key)


def plan(store: RunStore, now: dt.datetime) -> list[dict]:
    """The read phase of one sweep: what this host intends to do. (Keep this - both versions share it.)"""
    actions = []
    for run in store.list(status="waiting_approval"):
        escalated = {e["approval_id"] for e in store.events(run.id, types=("approval.escalated",))}
        for a in store.approvals(run.id):
            if a["status"] != "pending":
                continue
            age = now - dt.datetime.fromisoformat(a["requested_at"])
            if age >= EXPIRE_AFTER:
                actions.append({"kind": "expire", "run_id": run.id, "approval_id": a["approval_id"]})
            elif age >= ESCALATE_AFTER and a["approval_id"] not in escalated:
                actions.append({"kind": "escalate", "run_id": run.id, "approval_id": a["approval_id"],
                                "hours": round(age.total_seconds() / 3600)})
    return actions


def apply(store: RunStore, pager: Pager, actions: list[dict], *, host: str) -> list[str]:
    """TODO: the write phase, naive: acts on every planned action, whatever happened since the plan."""
    done = []
    for action in actions:
        if action["kind"] == "escalate":
            store.append(action["run_id"], "approval.escalated", {"approval_id": action["approval_id"],
                                                                  "to": "finance_director", "by": host})
            page = pager.page("finance_director", f"Refund approval waiting {action['hours']} h", run_id=action["run_id"])
        else:
            store.decide(action["approval_id"], approved=False, by=host, note="no decision within 2 days")
            page = pager.page("support_manager", "Refund approval expired; the customer will be told it is overdue",
                              run_id=action["run_id"])
        done.append(f"{action['kind']} {action['run_id']} -> {page['page_id']}")
    return done


# ---------------------------------------------------------------------------- the harness (keep this)
def worker(store, client, db, name: str):
    desk = SupportDesk(d1.APPROVALS_EMAIL, db=db, ticket_ref=d1.APPROVALS_TICKET)
    return d1.support_runner(store, client, execute=d1.gated_executor(desk, db), worker=name, system=d1.APPROVALS_SYSTEM)


def run_campaign(client, apply_fn, store_name: str) -> dict:
    store, db, pager = d1.fresh_store(store_name), memory_db(), Pager()
    for run_id in ("apr-A", "apr-B"):
        run = store.create("support", input=d1.approvals_input(), run_id=run_id)
        worker(store, client, db, "worker-a").run(run.id)
        if not store.approvals(run_id):
            raise SystemExit(f"{run_id} did not park on an approval (live: the model took a different path); rerun")
    requested = max(dt.datetime.fromisoformat(store.approvals(r)[0]["requested_at"]) for r in ("apr-A", "apr-B"))
    log = []

    # +4 hours: both hosts read, then both write
    plan_1, plan_2 = plan(store, requested + ESCALATE_AFTER), plan(store, requested + ESCALATE_AFTER)
    log += [f"+4 h  host-1: {x}" for x in apply_fn(store, pager, plan_1, host="host-1")]
    log += [f"+4 h  host-2: {x}" for x in apply_fn(store, pager, plan_2, host="host-2")]

    # +2 days: host-1 plans; a manager approves apr-B in the UI; host-2 plans; then both apply
    plan_1 = plan(store, requested + EXPIRE_AFTER)
    ui = RunStore(store.path)
    ui.decide([a for a in ui.approvals("apr-B") if a["status"] == "pending"][0]["approval_id"], approved=True,
              by="ops.manager", note="late, but approved")
    plan_2 = plan(store, requested + EXPIRE_AFTER)
    log += [f"+2 d  host-1: {x}" for x in apply_fn(store, pager, plan_1, host="host-1")]
    log += [f"+2 d  host-2: {x}" for x in apply_fn(store, pager, plan_2, host="host-2")]

    replies = {}
    for run_id in ("apr-A", "apr-B"):                                  # lab 05's dispatcher rule
        runner = worker(store, client, db, "worker-b")
        outcome = runner.resume_after_decision(run_id) if d1.decided_but_unanswered(store, run_id) else runner.run(run_id)
        replies[run_id] = outcome
    return {"store": store, "db": db, "pager": pager, "log": log, "replies": replies}


def report(result: dict) -> None:
    store, pager = result["store"], result["pager"]
    for line in result["log"]:
        print(f"  {line}")
    print(f"  pages sent: {[(p['page_id'], p['to'], p['run_id']) for p in pager.pages]}")
    for run_id in ("apr-A", "apr-B"):
        a = store.approvals(run_id)[0]
        outcome = result["replies"][run_id]
        print(f"  {run_id}: approval {a['status']} by {a['decided_by']} ({a['note']}) -> run {outcome.status}: "
              f"{d1.short(outcome.reply, 90)}")
    print(f"  refunds: {d1.refunds(result['db'])}")


def main() -> None:
    client = get_client()
    header("Exercise 10 - an approval sweeper two hosts can run (starter)")
    report(run_campaign(client, apply, "ex10_starter"))
    print("\nTODO: apr-A and apr-B were each escalated twice (4 pages at +4 h), apr-A's expiry paged twice, and the")
    print("      approved apr-B got an 'expired' page it never deserved. Write apply_safely(): one claim per")
    print("      approval and action, a keyed page, a re-check of the approval right before acting, and an")
    print("      'expired:' note. Expected: 3 pages in total (escalate A, escalate B, expire A).")


if __name__ == "__main__":
    main()
