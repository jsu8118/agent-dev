"""Solution to exercise 10 - an approval sweeper that two hosts can run at the same time.

apply_safely() changes three things about the naive write phase:
1. Claim before acting. Each (approval, action) pair gets one row in the durable effects table
   (key "sweep:<approval>:<action>"); the host that inserts it acts, the other skips. RunStore.effect_begin
   reads before it inserts, so under a true race the loser gets the primary key's IntegrityError instead of
   the row - that means "taken", not "crash".
2. Re-check right before acting. A plan is a snapshot; a manager may have decided since. An approval that is
   no longer pending is left alone - unless it is our own half-finished expiry (a host died between its
   decision and its page), which is finished instead.
3. Key the side effect. The page carries the claim's key, so the pager deduplicates a retry after a crash;
   expiry is written as "expired: ..." and an approval.expired event, not as a person's refusal.
4. Finish what a dead host started. plan() only sees pending approvals, so an expiry a host decided but never
   announced (it died before its page) would be lost; plan_unfinished() finds those and apply_safely() finishes
   them under the same key.

No line of advanced/lib/durable.py changes: the solution only calls RunStore's public methods.
Run: python advanced/day1_durable_agents/solutions/ex10_approval_sweeper.py
"""
# test: expect=5/5 checks passed
# test: expect=skipped: decided by ops.manager
# test: expect=finished a dead host's claim

from __future__ import annotations

import datetime as dt
import importlib.util
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import get_client, header, is_mock, step  # noqa: E402

import _day1 as d1  # noqa: E402  (also puts the Kestrel data on the path for the starter)

STARTER = Path(__file__).resolve().parents[1] / "exercises" / "ex10_approval_sweeper.py"


def load_starter():
    spec = importlib.util.spec_from_file_location("ex10_starter", STARTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                      # dataclasses and pickling look the module up by name
    spec.loader.exec_module(module)
    return module


starter = load_starter()


def claim(store: RunStore, key: str, run_id: str) -> str:
    """'mine' if this host won the claim, 'done' / 'in flight' if a claim exists, 'taken' if we lost a true race."""
    try:
        previous = store.effect_begin(key, run_id, "sla-sweeper")
    except sqlite3.IntegrityError:
        return "taken"
    if previous is None:
        return "mine"
    return "done" if previous["status"] == "done" else "in flight"


def approval(store: RunStore, run_id: str, approval_id: str) -> dict:
    return next(a for a in store.approvals(run_id) if a["approval_id"] == approval_id)


def ours(row: dict) -> bool:
    return (row["note"] or "").startswith("expired:")


class HostDied(BaseException):
    """A sweeper host killed mid-action (a BaseException, like durable.Crash: nothing may catch it)."""


def plan_unfinished(store: RunStore) -> list[dict]:
    """Expiries a dead host decided but never announced: an 'expired:' decision with no approval.expired event."""
    actions = []
    for run in store.list():
        announced = {e["approval_id"] for e in store.events(run.id, types=("approval.expired",))}
        for a in store.approvals(run.id):
            if a["status"] == "rejected" and ours(a) and a["approval_id"] not in announced:
                actions.append({"kind": "expire", "run_id": run.id, "approval_id": a["approval_id"]})
    return actions


def apply_safely(store: RunStore, pager, actions: list[dict], *, host: str, die_before_page: bool = False) -> list[str]:
    done = []
    for action in actions:
        kind, run_id, approval_id = action["kind"], action["run_id"], action["approval_id"]
        key = f"sweep:{approval_id}:{kind}"
        state = claim(store, key, run_id)
        if state in ("done", "taken"):
            done.append(f"{kind} {run_id} -> skipped: another host has it ({state})")
            continue
        current = approval(store, run_id, approval_id)
        if current["status"] != "pending" and not (kind == "expire" and ours(current)):
            store.effect_finish(key, {"skipped": f"decided by {current['decided_by']}"})
            done.append(f"{kind} {run_id} -> skipped: decided by {current['decided_by']} since the plan")
            continue
        if kind == "expire":
            if current["status"] == "pending":
                store.decide(approval_id, approved=False, by=host,
                             note=f"expired: no decision within {starter.EXPIRE_AFTER.days} days")
                if not ours(approval(store, run_id, approval_id)):      # a person decided between check and act
                    store.effect_finish(key, {"skipped": "decided by a person"})
                    done.append(f"{kind} {run_id} -> skipped: decided by a person meanwhile")
                    continue
            if die_before_page:
                raise HostDied(f"{host} died after expiring {run_id}, before its page")
            if not any(e["approval_id"] == approval_id for e in store.events(run_id, types=("approval.expired",))):
                store.append(run_id, "approval.expired", {"approval_id": approval_id, "by": host})
            page = pager.page("support_manager", "Refund approval expired; the customer will be told it is overdue",
                              run_id=run_id, key=key)
        else:
            if not any(e["approval_id"] == approval_id for e in store.events(run_id, types=("approval.escalated",))):
                store.append(run_id, "approval.escalated", {"approval_id": approval_id, "to": "finance_director",
                                                            "by": host})
            page = pager.page("finance_director", f"Refund approval waiting {action['hours']} h", run_id=run_id, key=key)
        store.effect_finish(key, page)
        done.append(f"{kind} {run_id} -> {page['page_id']}" + (" (finished a dead host's claim)" if state == "in flight" else ""))
    return done


def dead_host_scenario(client) -> bool:
    store, db, pager = d1.fresh_store("ex10_dead_host"), starter.memory_db(), starter.Pager()
    run = store.create("support", input=d1.approvals_input(), run_id="apr-C")
    starter.worker(store, client, db, "worker-a").run(run.id)
    later = dt.datetime.fromisoformat(store.approvals("apr-C")[0]["requested_at"]) + starter.EXPIRE_AFTER
    try:
        apply_safely(store, pager, starter.plan(store, later), host="host-1", die_before_page=True)
    except HostDied as exc:
        print(f"  host-1: {exc}")
    print(f"  pending approvals the next plan() sees: {len(starter.plan(store, later))}; "
          f"unfinished expiries: {len(plan_unfinished(store))}")
    for line in apply_safely(store, pager, starter.plan(store, later) + plan_unfinished(store), host="host-2"):
        print(f"  host-2: {line}")
    for line in apply_safely(store, pager, starter.plan(store, later) + plan_unfinished(store), host="host-1"):
        print(f"  host-1: {line}")
    print(f"  pages sent: {[(p['page_id'], p['to'], p['run_id']) for p in pager.pages]}")
    return [(p["to"], p["run_id"]) for p in pager.pages] == [("support_manager", "apr-C")]


def main() -> None:
    client = get_client()
    header("Exercise 10 - an approval sweeper two hosts can run (solution)")
    if is_mock():
        print("[mock] the stand-in model drives the refund runs; the two hosts are simulated in one process (both plan, then both apply), the claims and the pager are real code.")

    step(1, "The naive sweeper (the starter's apply)")
    naive = starter.run_campaign(client, starter.apply, "ex10_naive")
    print(f"  pages sent: {len(naive['pager'].pages)}")

    step(2, "apply_safely: claim, re-check, keyed page")
    result = starter.run_campaign(client, apply_safely, "ex10_safe")
    starter.report(result)

    step(3, "A host dies between its decision and its page; the next sweep finishes the job")
    crash = dead_host_scenario(client)

    store, pages = result["store"], result["pager"].pages
    a, b = approval(store, "apr-A", store.approvals("apr-A")[0]["approval_id"]), store.approvals("apr-B")[0]
    checks = {
        "3 pages: escalate A, escalate B, expire A": [(p["to"], p["run_id"]) for p in pages] == [
            ("finance_director", "apr-A"), ("finance_director", "apr-B"), ("support_manager", "apr-A")],
        "apr-A expired, not refused": a["status"] == "rejected" and ours(a),
        "apr-B keeps the manager's approval and is refunded": b["status"] == "approved" and len(d1.refunds(result["db"])) == 1,
        "apr-A's customer hears 'overdue' with an escalation": "escalated" in result["replies"]["apr-A"].reply,
        "a dead host's expiry is paged exactly once by the next sweep": crash,
    }
    print()
    for name, ok in checks.items():
        print(f"  check: {name}: {'OK' if ok else 'FAILED'}")
    print(f"\n{sum(checks.values())}/{len(checks)} checks passed (the naive sweeper sent {len(naive['pager'].pages)} pages)")


if __name__ == "__main__":
    main()
