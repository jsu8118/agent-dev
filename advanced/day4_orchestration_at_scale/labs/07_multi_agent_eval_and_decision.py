"""Lab 07 - Evaluating multi-agent systems, and the decision: one agent, a self-hosted swarm or a hosted swarm.

Objective
    Run the same 11-unit task through five configurations - one agent, the first (naive) self-hosted swarm, the
    tuned self-hosted swarm, and the hosted swarm of lab 06 with two parallel planners and with one - and score
    them the way a multi-agent system has to be
    scored: success per unit of work (a code checker on every unit plan plus the desk's state), cost per successful
    unit and the cost multiplication over one agent, coordination tokens and their share, the largest prompt, a
    modelled critical path, and failure attribution from traces for every unit that failed and every incident that
    was repaired. Then print the decision table.

Concepts
    task success per unit of work, cost per successful unit, cost multiplication, coordination overhead (tokens that
    exist only because the work was split), context isolation (largest prompt), critical path, failure attribution
    across agents from traces (durable run logs, session events), trace-based debugging, the single / self-hosted /
    hosted decision

Run
    python advanced/day4_orchestration_at_scale/labs/07_multi_agent_eval_and_decision.py

What to observe
    * Step 1: the scorer is code: expected properties per unit from the dataset and the campaign rules, checked
      against the desk's bookings; no agent grades itself.
    * Step 2: the scoreboard. One agent is cheap as run - the cache carries its long context - but books two safety
      units after their deadline; the naive swarm fixes that at about twice the cost, the difference being mostly
      coordination; the tuned swarm keeps the fix at close to one agent's cost; the hosted swarm with two parallel
      planners pays for its commit conflicts, and with one planner it is the cheapest row.
    * Step 3: attribution: each failure and each repaired incident traced to the agent, the turn and the tool call or
      thread where it started - read from the logs, not from the answer key.
    * Step 4: the decision table, with the numbers above and the ownership questions the numbers cannot answer.
"""
# test: expect=coordination tokens
# test: expect=context contamination
# test: expect=decision

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import DurableRunner, RunStore  # noqa: E402
from labkit import MODEL, get_client, header, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402

lab01 = d4.load_lab("01_durable_coordinator")
lab06 = d4.load_lab("06_managed_coordinator")
SINGLE_RUN = "single:RC-2026-03"


# ------------------------------------------------------------------------------ measuring
@lru_cache(maxsize=None)
def _count(text: str) -> int:
    """Tokens in a piece of text (a brief, a report), counted by the API rather than estimated."""
    if not text.strip():
        return 0
    return get_client().messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": text}]).input_tokens


def run_totals(store: RunStore, run_id: str) -> dict:
    responses = store.events(run_id, types=("model.response",))
    return {"turns": len(responses), "report": int(responses[-1]["usage"].get("output_tokens") or 0) if responses else 0}


def run_single(client) -> dict:
    db = d4.fresh_db("lab07_single.db")
    store, desk = RunStore(db), d4.RecallDesk()
    store.create("single", input={"message": "Plan the recall remedy for every affected unit of campaign RC-2026-03."},
                 run_id=SINGLE_RUN)
    runner = DurableRunner(store, client, model=MODEL, system=d4.single_agent_system(), tools=[d4.LIST_UNITS_TOOL] + d4.WORKER_TOOLS,
                           execute=d4.make_executor(desk), max_turns=60, create_kwargs={"cache_control": {"type": "ephemeral"}})
    outcome = runner.run(SINGLE_RUN)
    meter = d4.Meter()
    meter.add_run("agent", store, SINGLE_RUN)
    total = meter.total()
    return {"name": "one agent", "plans": d4.parse_plans(outcome.reply), "desk": desk, "store": store,
            "requests": total.calls, "tokens": total.input_tokens + total.output_tokens, "cost": total.cost,
            "uncached": total.uncached_cost, "largest": total.largest_prompt, "coordination": 0,
            "latency": d4.run_seconds(store, SINGLE_RUN), "runs": {SINGLE_RUN: "one agent"}}


def run_swarm(client, profile: str) -> dict:
    db = d4.fresh_db(f"lab07_{profile}.db")
    store, queue, desk = RunStore(db), d4.WorkQueue(db), d4.RecallDesk()
    _, dispatcher = lab01.run_campaign(client, store, queue, desk, profile=profile, log=lambda *_: None)
    meter = d4.Meter()
    meter.add_run("coordinator", store, lab01.RUN_ID)
    coordinator = meter.roles["coordinator"]
    briefs = reports = 0
    worker_seconds, runs = [], {lab01.RUN_ID: "coordinator"}
    for task_id, record in dispatcher.records.items():
        meter.add_run("worker", store, record["run_id"])
        totals = run_totals(store, record["run_id"])
        briefs += _count(record["brief"]) * totals["turns"]          # a brief is re-read on every turn of its worker
        reports += totals["report"]                                  # the final turn writes the report
        worker_seconds.append(d4.run_seconds(store, record["run_id"]))
        runs[record["run_id"]] = f"worker {task_id.replace('batch:', '')}"
    total = meter.total()
    plans = [p for r in queue.results() for p in r["payload"]["plans"]]
    return {"name": f"self-hosted swarm, {profile}", "plans": plans, "desk": desk, "store": store, "requests": total.calls,
            "tokens": total.input_tokens + total.output_tokens, "cost": total.cost, "uncached": total.uncached_cost,
            "largest": total.largest_prompt, "coordination": coordinator.input_tokens + coordinator.output_tokens + briefs + reports,
            "coordination_parts": (coordinator.input_tokens + coordinator.output_tokens, briefs, reports),
            "latency": d4.run_seconds(store, lab01.RUN_ID) + max(worker_seconds), "runs": runs, "workers": len(worker_seconds)}


def run_hosted(client, planners: int) -> dict:
    desk = d4.RecallDesk()
    session_id, _, commits, events, _ = lab06.run_hosted(client, desk, planners=planners)
    spend = d4.session_spend(client, session_id)
    timeline = lab06.hosted_timeline(events)
    lead_tokens = sum(d4.prompt_tokens(u) + u["output_tokens"] for u in (d4.usage_tokens(t.usage) for t in
                      client.beta.sessions.threads.list(session_id) if t.agent.name == lab06.LEAD))
    briefs = sum(_count(" ".join(b.text for b in e.content)) * timeline["requests"].get(e.to_session_thread_id, 0)
                 for e in events if e.type == "agent.thread_message_sent" and e.to_agent_name != lab06.LEAD)
    reports = sum(_count(" ".join(b.text for b in e.content))
                  for e in events if e.type == "agent.thread_message_received" and e.from_agent_name != lab06.LEAD)
    tokens = d4.prompt_tokens(spend) + spend["output_tokens"]
    uncached = d4.uncached_cost(spend)
    return {"name": f"hosted swarm, {'one planner' if planners == 1 else f'{planners} planners'}",
            "plans": lab06.final_plans(commits), "desk": desk, "events": events,
            "commits": commits, "requests": spend["requests"], "tokens": tokens, "cost": spend["cost"], "uncached": uncached,
            "largest": spend["largest_prompt"], "coordination": lead_tokens + briefs + reports,
            "coordination_parts": (lead_tokens, briefs, reports), "latency": timeline["critical"], "session": session_id}


# ------------------------------------------------------------------------------ attribution from traces
def trace_calls(store: RunStore, run_id: str) -> list[dict]:
    """The run's tool calls in order, each with the turn that issued it and its result - straight from the log."""
    calls, index, turn = [], {}, 0
    for e in store.events(run_id):
        if e["type"] == "model.response":
            turn = e["turn"]
        elif e["type"] == "tool.started":
            index[e["tool_use_id"]] = len(calls)
            calls.append({"turn": turn, "name": e["name"], "input": e["input"], "result": None, "is_error": False})
        elif e["type"] == "tool.result" and e["tool_use_id"] in index:
            call = calls[index[e["tool_use_id"]]]
            call["result"], call["is_error"] = e["content"], e.get("is_error", False)
    return calls


def attribute_run(store: RunStore, run_id: str, agent: str, serial: str) -> dict:
    """Why did `serial` fail in this run? Walk back from its booking to the search the slot came from."""
    calls = trace_calls(store, run_id)
    unit = next((json.loads(c["result"]) for c in calls if c["name"] == "get_unit" and c["input"].get("serial") == serial
                 and not c["is_error"]), None)
    bookings = [c for c in calls if c["name"] == "book_slot" and c["input"].get("serial") == serial and not c["is_error"]]
    if unit is None or not bookings:
        return {"started": f"{agent}", "evidence": "no successful get_unit / book_slot for this unit in the log",
                "category": "incomplete work"}
    booking = bookings[-1]
    slot = booking["input"]["slot_id"]
    searches = [c for c in calls[:calls.index(booking)] if c["name"] == "find_engineer_slots" and not c["is_error"]
                and slot in (c["result"] or "")]
    if searches and searches[-1]["input"].get("not_after") != unit["remedy_by"]:
        search = searches[-1]
        made_for = next((json.loads(c["result"])["serial"] for c in reversed(calls[:calls.index(search)])
                         if c["name"] == "get_unit" and not c["is_error"]
                         and json.loads(c["result"]).get("remedy_by") == search["input"].get("not_after")), "another unit")
        return {"started": f"{agent}, turn {booking['turn']}",
                "evidence": f"book_slot({slot}) took a slot from the turn-{search['turn']} search with not_after="
                            f"{search['input']['not_after']} (made for {made_for}); this unit's remedy_by is {unit['remedy_by']}",
                "category": "context contamination: a tool result made for another unit was reused"}
    return {"started": f"{agent}, turn {booking['turn']}", "evidence": f"book_slot({slot}) - see the run log",
            "category": "unexplained: read the trace"}


def attribute_hosted(result: dict) -> list[dict]:
    """Every plan the first commit rejected: which planner thread proposed it, which other thread's plan it collided with."""
    events = result["events"]
    planner_threads = [e.session_thread_id for e in events if e.type == "session.thread_created" and e.agent_name == "schedule-planner"]
    label = {t: f"schedule-planner #{i}" for i, t in enumerate(planner_threads, 1)}
    proposals: dict[str, tuple[str, dict]] = {}
    for e in events:
        if e.type == "agent.thread_message_received" and e.from_agent_name == "schedule-planner":
            for line in " ".join(b.text for b in e.content).splitlines():
                if line.strip().startswith("{"):
                    plan = json.loads(line)
                    proposals.setdefault(plan["serial"], (e.from_session_thread_id, plan))
    first = result["commits"].log[0] if result["commits"].log else {"rejected": []}
    rows = []
    for r in first["rejected"]:
        thread, plan = proposals.get(r["serial"], ("?", {}))
        rival = next((s for s, (t, p) in proposals.items() if t != thread and s != r["serial"] and (
            (plan.get("slot_id") and p.get("slot_id") == plan.get("slot_id")) or
            ("out of stock" in r["reason"] and p.get("kit_sku") == plan.get("kit_sku") and p.get("warehouse") == plan.get("warehouse")))), None)
        what = f"slot {plan.get('slot_id')}" if "slot" in r["reason"] else f"the last {plan.get('kit_sku')} at {plan.get('warehouse')}"
        rival_thread = proposals.get(rival, ("?", {}))[0] if rival else "?"
        rows.append({"serial": r["serial"], "started": f"{label.get(thread, thread)} (thread {thread})",
                     "evidence": f"proposed {what}, which {label.get(rival_thread, 'another thread')} had already given to "
                                 f"{rival or 'another unit'} in the same commit; "
                                 "both planned from the snapshot mounted at session start",
                     "category": "stale shared snapshot (no concurrency control between threads) - rejected at commit, re-planned"})
    return rows


# ------------------------------------------------------------------------------ the lab
def main() -> None:
    header("Lab 07 - Evaluating multi-agent systems, and the decision")
    d4.mock_note("every agent is a rule-based stand-in, so these numbers measure architecture, not model quality. The "
                 "single-agent stand-in reuses the latest slot search for the same region and skill (a scripted stand-in "
                 "for cross-item contamination in a long context); live, measure how often your model does it.")
    client = get_client()

    step(1, "The task and the scorer")
    print("  Task: a remedy plan for each of the 11 affected units - correct kit, contact and deadlines, a kit reserved and")
    print("  an engineer slot booked in the unit's region, with the skill, on or before its remedy_by date.")
    print("  Scorer: labs/_day4.check_plan() compares every plan with properties derived from the dataset and the")
    print("  campaign rules, and checks the plan against the desk's actual reservations and bookings. A unit counts as a")
    print("  success only if it is booked correctly; an honest 'pending_schedule' counts as escalated, not as success.")

    results = [run_single(client), run_swarm(client, "naive"), run_swarm(client, "tuned"), run_hosted(client, 2),
               run_hosted(client, 1)]
    for r in results:
        r["score"] = d4.score(r["plans"], r["desk"])

    step(2, "The scoreboard: the same 11 units, five configurations")
    rows = []
    for r in results:
        ok = r["score"]["ok"]
        rows.append([r["name"], f"{ok}/11", r["requests"], f"{r['tokens']:,}", d4.money(r["cost"]), d4.money(r["uncached"]),
                     d4.money(r["cost"] / ok) if ok else "-"])
    print(d4.table(rows, ["configuration", "success", "requests", "tokens processed", "cost as run", "cost uncached",
                          "$ per success"]))
    single, naive, tuned, hosted2, hosted = results
    print("  'cost as run' is what each run paid with prompt caching - the self-hosted requests carry cache_control, the")
    print("  hosted session caches its own history - and '$ per success' divides it by the units booked correctly.")
    print("  'cost uncached' prices the same tokens with no cache: how much of each bill the cache carries.")
    print(f"  cost multiplication over one agent, as run: naive {naive['cost'] / single['cost']:.2f}x, tuned "
          f"{tuned['cost'] / single['cost']:.2f}x, hosted {hosted2['cost'] / single['cost']:.2f}x (two planners) and "
          f"{hosted['cost'] / single['cost']:.2f}x (one planner)")
    print(f"  uncached, one agent would cost {single['uncached'] / single['cost']:.1f}x its bill and the tuned swarm "
          f"{tuned['uncached'] / tuned['cost']:.1f}x: the cache prices exactly the single agent's re-reading of its context")
    rows = []
    for r in results:
        share = r["coordination"] / r["tokens"] if r["tokens"] else 0.0
        parts = r.get("coordination_parts")
        detail = f"{parts[0]:,} coordinator + {parts[1]:,} briefs + {parts[2]:,} reports" if parts else "-"
        rows.append([r["name"], f"{r['coordination']:,}", f"{share:.0%}", detail, f"{r['largest']:,}", f"{r['latency']:.0f}s"])
    print()
    print(d4.table(rows, ["configuration", "coordination tokens", "share", "made of", "largest prompt", "latency*"]))
    print(f"  coordination tokens: naive swarm {naive['coordination']:,} vs tuned {tuned['coordination']:,} "
          f"({naive['coordination'] / tuned['coordination']:.1f}x); the naive swarm cost {naive['cost'] / tuned['cost']:.2f}x the tuned one.")
    print("  * modelled critical path (workers and threads in parallel; labs/_day4.py LATENCY_ASSUMPTIONS) - not a measurement.")
    print(wrap("Coordination tokens = every token the coordinator processes + every brief token a worker reads (re-read on "
               "each of its turns) + every report token a worker writes for the coordinator. They exist only because the "
               "work was split; one agent has none, and pays instead with a context that holds every unit.", "  "))

    step(3, "Failure attribution from traces")
    for r in results:
        found = []
        for row in r["score"]["rows"]:
            if not row["problems"] and row["status"] == "scheduled":
                continue
            run_id, agent = next(((rid, who) for rid, who in r.get("runs", {}).items()
                                  if rid == SINGLE_RUN or row["serial"] in rid), (None, "?"))
            where = attribute_run(r["store"], run_id, agent, row["serial"]) if run_id else \
                {"started": "?", "evidence": "no run holds this unit", "category": "never dispatched"}
            found.append((row["serial"], "; ".join(row["problems"]) or row["status"], where))
        if r["name"].startswith("hosted"):
            found += [(inc["serial"], "rejected at the first commit, re-planned", inc) for inc in attribute_hosted(r)]
        if not found:
            print(f"  {r['name']}: nothing to attribute - every unit booked correctly, no incident in the trace")
            continue
        for serial, outcome, where in found:
            print(f"  {r['name']} / {serial}: {outcome}")
            print(f"    started at: {where['started']}")
            print(f"    category:   {where['category']}")
            print(wrap(f"evidence:   {where['evidence']}", "    "))
    print(wrap("How the attribution works: start from the unit's final artifact (the booking, or the rejected commit), walk "
               "back through the trace to the tool result or message it came from, and stop at the first step whose input "
               "does not belong to this unit or this moment. The logs you already keep for durability (Day 1) and the "
               "session's event stream are the traces - so debugging a swarm is a query, not a reconstruction.", "  "))

    step(4, "The decision")
    rows = []
    for label, r in (("one agent", single), ("self-hosted swarm (tuned)", tuned), ("hosted swarm (one planner)", hosted)):
        ok = max(r["score"]["ok"], 1)
        rows.append([label, f"{r['score']['ok']}/11", d4.money(r["cost"] / ok), f"{r['cost'] / single['cost']:.2f}x",
                     f"{r['coordination'] / r['tokens']:.0%}", f"{r['largest']:,}", f"{r['latency']:.0f}s"])
    print(d4.table(rows, ["decision", "success", "$ per success*", "x one agent*", "coordination share", "largest prompt",
                          "latency**"]))
    print("  * as run, prompt caching included on both sides; live, compare the bills you actually pay (the platform also")
    print("    bills a session's running time).")
    print("  ** modelled critical path, not a measurement.")
    print(wrap("Read the cost column with care: the hosted swarm is cheaper here because of how its work is shaped - "
               "workers read a mounted snapshot in bulk and one commit books everything - not because it is hosted; a "
               "self-hosted swarm can be shaped the same way. Decide WHERE the orchestration runs by what you must own, "
               "and HOW the work is shaped by the numbers.", "  "))
    print("  Choose one agent when the items are few and alike, fit one context and need no waiting - and measure cross-")
    print("  item contamination on your own traces. Choose the self-hosted swarm when the work spans days, crashes and")
    print("  approvals and you need priorities, retries, dead letters and exactly-once effects. Choose the hosted swarm")
    print("  for a fan-out that fits one session, when you want the loop, the sandbox and a hard cost cap run for you -")
    print("  and remember that the tools, the policy at commit and the evals stay yours either way.")
    late = len([row for row in single["score"]["rows"] if row["problems"]])
    print(wrap(f"Rule of thumb from these numbers: split the work when isolation or parallelism buys something you can "
               f"measure - here {late} safety units booked on time instead of late, and a {single['latency']:.0f}s critical path "
               f"cut to {tuned['latency']:.0f}s - and only once the coordination bill is under control: the naive swarm spent "
               f"{naive['coordination'] / naive['tokens']:.0%} of its tokens on coordination, {naive['coordination'] / tuned['coordination']:.1f}x "
               f"what the tuned swarm needed for the same result, and the hosted swarm's two parallel planners needed "
               f"{hosted2['requests'] - hosted['requests']} more requests and {hosted2['cost'] / hosted['cost']:.1f}x the cost of one "
               "planner to reach the same 11 bookings. The capstone (advanced/day7_capstone/reference) is where the "
               "self-hosted branch of this decision goes next.", "  "))
    sessions = [r for r in results if r["name"].startswith("hosted")]
    print(f"\nUsage summary - Managed Agents sessions (from span.model_request_end; not in labkit's Messages API ledger)\n"
          f"  {len(sessions)} sessions  {sum(r['requests'] for r in sessions)} requests  {d4.money(sum(r['cost'] for r in sessions))}")

if __name__ == "__main__":
    main()
