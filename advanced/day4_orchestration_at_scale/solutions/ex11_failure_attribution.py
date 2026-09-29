"""Solution to exercise 11 - failure attribution from traces.

Start from the unit's outcome and walk backwards, asking one question per hop: did the unit reach a worker at all
(the dispatch record), did the worker's tools work for it (error results on its own calls), and did the booking come
from a tool result that belongs to this unit (the search's parameters against the unit's deadline)? The first "no"
names the component and the step. Everything is read from the logs; the answer key is only used to grade.

Run: python advanced/day4_orchestration_at_scale/solutions/ex11_failure_attribution.py
"""
# test: expect=3/3 attributed correctly

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "labs"))
sys.path.insert(0, str(HERE.parents[1] / "exercises"))

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import get_client, header, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402
import ex11_failure_attribution as starter  # noqa: E402


def calls_of(store: RunStore, run_id: str) -> list[dict]:
    """The run's tool calls in order, with the turn that issued them and their results."""
    calls, index, turn = [], {}, 0
    for e in store.events(run_id):
        if e["type"] == "model.response":
            turn = e["turn"]
        elif e["type"] == "tool.started":
            index[e["tool_use_id"]] = len(calls)
            calls.append({"turn": turn, "name": e["name"], "input": e["input"], "result": None, "is_error": False})
        elif e["type"] == "tool.result" and e["tool_use_id"] in index:
            calls[index[e["tool_use_id"]]].update(result=e["content"], is_error=e.get("is_error", False))
    return calls


def attribute(serial: str, store: RunStore, dispatch: dict[str, list[str]]) -> dict:
    # 1. Did the unit reach a worker?
    task = next((t for t, serials_ in dispatch.items() if serial in serials_), None)
    if task is None:
        near = max(dispatch, key=lambda t: len(dispatch[t]))
        return {"agent": "coordinator", "turn": None, "category": "dropped at dispatch",
                "evidence": f"no task in the dispatch record lists {serial}; {near} carried {len(dispatch[near])} serials"}
    run_id = f"unit:{task}"
    calls = calls_of(store, run_id)
    unit = next((json.loads(c["result"]) for c in calls if c["name"] == "get_unit" and c["input"].get("serial") == serial
                 and not c["is_error"]), None)
    if unit is None:
        return {"agent": run_id, "turn": None, "category": "other", "evidence": "the worker never read the unit"}
    # 2. Did the worker's tools work for this unit?
    searches = [c for c in calls if c["name"] == "find_engineer_slots" and c["input"].get("region") == unit["region"]
                and c["input"].get("skill") == unit["skill_required"]]
    if searches and all(c["is_error"] for c in searches):
        first = searches[0]
        return {"agent": run_id, "turn": first["turn"], "category": "tool outage",
                "evidence": f"all {len(searches)} find_engineer_slots call(s) for {unit['region']}/{unit['skill_required']} "
                            f"failed: {json.loads(first['result']).get('error')}"}
    # 3. Did the booking come from a tool result that belongs to this unit?
    bookings = [c for c in calls if c["name"] == "book_slot" and c["input"].get("serial") == serial and not c["is_error"]]
    if bookings:
        booking = bookings[-1]
        slot = booking["input"]["slot_id"]
        source = [c for c in calls[:calls.index(booking)] if c["name"] == "find_engineer_slots" and not c["is_error"]
                  and slot in (c["result"] or "")]
        if source and source[-1]["input"].get("not_after") != unit["remedy_by"]:
            s = source[-1]
            return {"agent": run_id, "turn": booking["turn"], "category": "context contamination",
                    "evidence": f"book_slot({slot}) took a slot from the turn-{s['turn']} search with not_after="
                                f"{s['input']['not_after']}, made for another unit; this unit's remedy_by is {unit['remedy_by']}"}
    return {"agent": run_id, "turn": None, "category": "other", "evidence": "read the full log"}


def main() -> None:
    header("Exercise 11 - failure attribution from traces (solution)")
    store, desk, plans = starter.run_swarm(get_client())
    scored = d4.score([p for ps in plans.values() for p in ps], desk)
    failed = [r for r in scored["rows"] if r["problems"] or r["status"] != "scheduled"]

    step(1, "Attribution")
    right = 0
    for r in failed:
        verdict = attribute(r["serial"], store, starter.DISPATCH)
        ok = verdict["category"] == starter.ANSWER_KEY.get(r["serial"])
        right += ok
        print(f"  {r['serial']} ({'; '.join(r['problems']) or r['status']})")
        print(f"    -> {verdict['category']} [{'right' if ok else 'WRONG'}] at {verdict['agent']}, turn {verdict['turn']}")
        print(wrap(f"evidence: {verdict['evidence']}", "       "))
    print(f"  {right}/{len(failed)} attributed correctly")

    step(2, "What an end-to-end check would have seen")
    print(wrap("A single 'did the campaign succeed?' flag reads the coordinator's summary. It would catch nothing here: "
               "the dropped unit never appears in any plan, the outage surfaces as an honest pending_schedule, and the "
               "contaminated booking is 'scheduled'. Only a per-unit check (every unit of the campaign, not every plan "
               "returned) finds all three, and only the traces say which agent and which step to fix: the coordinator's "
               "batching, the EU scheduling dependency, the worker's context.", "  "))


if __name__ == "__main__":
    main()
