"""Exercise 11 starter - failure attribution from traces.

The harness runs a small swarm with three planted problems and keeps every run's log (the same event logs Day 1's
runtime writes):

  * a worker whose batch mixes deadlines - every US-EAST unit, three risk classes, in one context;
  * a brief that drops a unit - the list of serials sent to that worker was cut short;
  * a scheduling outage in one region - every find_engineer_slots call for it fails.

Write `attribute(serial)` so that, for every unit that is not booked correctly, it names the agent (which run), the
turn, a category and the evidence - using ONLY the dispatch record and the run logs. `ANSWER_KEY` is there to check
you, not to read from. Categories to use: "dropped at dispatch", "tool outage", "context contamination", "other".

Run: python advanced/day4_orchestration_at_scale/exercises/ex11_failure_attribution.py
Solution: advanced/day4_orchestration_at_scale/solutions/ex11_failure_attribution.py
"""

from __future__ import annotations

import json  # noqa: F401 - you will need it to read tool results from the logs
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import get_client, header, step  # noqa: E402

import _day4 as d4  # noqa: E402

# The dispatch record: what the coordinator sent to which worker (task id -> serials in the brief).
DISPATCH = {
    "batch:us-east": ["KC2-2608-0001", "KC2-2608-0002", "KP100-2608-0002", "KP250-2608-0002", "KP250-2608-0003",
                      "KP250-2608-0004", "KP250-2608-0005", "KP250-2608-0006", "KP250-2608-0007"],   # batched by region
    "batch:C-1025": ["KP100-2608-0001"],                                                                # an EU outage
}
ANSWER_KEY = {"KP250-2608-0007": "context contamination", "KP250-2608-0008": "dropped at dispatch",
              "KP100-2608-0001": "tool outage"}


def run_swarm(client) -> tuple[RunStore, d4.RecallDesk, dict[str, list[dict]]]:
    """Run one worker agent per batch with the planted problems; return the store, the desk and the plans per task."""
    def outage(tool_input: dict) -> None:
        if tool_input.get("region") == "EU":
            raise d4.ToolFailure("EU scheduling service unavailable (HTTP 503)")

    store, desk = RunStore(d4.fresh_db("ex11.db")), d4.RecallDesk(faults={"find_engineer_slots": outage})
    plans: dict[str, list[dict]] = {}
    for task_id, serials_ in DISPATCH.items():
        result = d4.run_unit_worker(client, store, desk, serials_, run_id=f"unit:{task_id}", worker="worker-1")
        plans[task_id] = result.plans
    return store, desk, plans


def attribute(serial: str, store: RunStore, dispatch: dict[str, list[str]]) -> dict:
    """TODO: {"agent": run id or "coordinator", "turn": int | None, "category": ..., "evidence": ...}."""
    raise NotImplementedError("attribute(): walk the logs back from the unit's outcome to where it went wrong")


def main() -> None:
    header("Exercise 11 - failure attribution from traces (starter)")
    store, desk, plans = run_swarm(get_client())
    all_plans = [p for ps in plans.values() for p in ps]
    scored = d4.score(all_plans, desk)

    step(1, "The outcome: which units are not booked correctly")
    failed = [r for r in scored["rows"] if r["problems"] or r["status"] != "scheduled"]
    for r in failed:
        print(f"  {r['serial']}: status={r['status']} problems={'; '.join(r['problems']) or '-'}")
    print(f"  {scored['ok']}/11 booked correctly, {len(failed)} to explain")

    step(2, "Your attribution")
    right = 0
    for r in failed:
        try:
            verdict = attribute(r["serial"], store, DISPATCH)
        except NotImplementedError as exc:
            print(f"  TODO: {exc}")
            break
        ok = verdict["category"] == ANSWER_KEY.get(r["serial"])
        right += ok
        print(f"  {r['serial']}: {verdict['category']} ({'right' if ok else 'expected ' + ANSWER_KEY.get(r['serial'], '?')}) "
              f"- {verdict['agent']}, turn {verdict['turn']}: {verdict['evidence']}")
    else:
        print(f"  {right}/{len(failed)} attributed correctly")
    print("  Question: which of these would one end-to-end 'campaign succeeded?' check have caught - and which would it")
    print("  have hidden behind an honest-looking 'pending_schedule'?")


if __name__ == "__main__":
    main()
