"""Lab 06 - Managed coordinator: the hosted twin of lab 01 - a roster, threads, a shared workspace, commits through your code.

Objective
    Rebuild the recall swarm on Managed Agents: a coordinator whose roster holds two worker agents (a unit
    investigator and a schedule planner) and itself, one session with a budget, the campaign data mounted into the
    session's workspace as files (read by the workers, never copied through the coordinator), and every booking
    committed through a custom tool that YOUR code validates. Two planner threads work from the same snapshot and
    collide; the commit catches it and the lead re-plans in an existing thread from a fresh snapshot. Then compare
    cost, latency and control with lab 01's self-hosted swarm on the same 11 units.

Concepts
    coordinator agents and rosters (1-20 entries, `self`, one delegation level), threads (context-isolated,
    persistent, one shared filesystem), list_agents / send_to_agent, files mounted as session resources (pass data
    by reference), custom tools on the coordinator, stale shared snapshots and commit-time validation, per-thread
    usage, one budget shared by all threads, hosted vs self-hosted (cost, latency, control)

Run
    python advanced/day4_orchestration_at_scale/labs/06_managed_coordinator.py [--planners 2]

What to observe
    * Step 1: a coordinator whose roster contains a coordinator is refused - one level of delegation.
    * Step 2: the data goes in as two Files API uploads mounted as session resources (session.resources lists
      them); the primary stream shows threads created, tasks sent and reports received, and the session stopping
      for your custom tools (the unit listing, and the commits).
    * Step 3: the first commit rejects the bookings that the second planner took from a stale snapshot; the lead
      refreshes the snapshot and re-plans them in the first planner's existing thread; 11 of 11 end scheduled.
    * Step 4: threads.list - the primary plus six threads, each with its own context and usage (mostly cache reads:
      every thread caches its own history).
    * Step 5: the comparison with lab 01: requests, tokens, cost, modelled latency and who owns which concern.
"""
# test: expect=one level of delegation
# test: expect=re-plan

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic  # noqa: E402

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402

LEAD = "recall-lead"
READ_ONLY = {"type": "agent_toolset_20260401", "default_config": {"enabled": False},
             "configs": [{"name": n, "enabled": True} for n in ("read", "glob", "grep")]}
INVESTIGATOR_SYSTEM = ("<adv_day4_hosted_investigator>\nYou assess affected units of recall RC-2026-03. Read "
                       "/workspace/campaign/units.json; for each serial you are given, report one JSON line: "
                       "serial, customer_id, risk_class, region, remedy, kit_sku, skill, contact_id (the site contact when "
                       "the primary contact is out of office), contact_by, remedy_by.\n</adv_day4_hosted_investigator>")
PLANNER_SYSTEM = ("<adv_day4_hosted_planner>\nYou plan field visits for assessed recall units. Use the snapshot in your "
                  "task if it has one, else read /workspace/campaign/resources.json; for each assessed unit, earliest deadline "
                  "first, choose the region's warehouse (else "
                  "the one with most stock) and the earliest free slot in the region with the skill on or before remedy_by. "
                  "Never give two units the same slot or more kits than the snapshot shows. One JSON line per unit."
                  "\n</adv_day4_hosted_planner>")
DATA_TOOLS = [
    d4.LIST_UNITS_TOOL,
    d4._tool("get_resources", "A fresh snapshot of remedy-kit stock per warehouse and of the free engineer slots in the "
                              "affected regions (the mounted resources.json is the snapshot at session start).", {}, []),
    d4._tool("record_plan", "Commit unit plans to Kestrel's system of record: each scheduled plan reserves its kit and books "
                            "its slot, or is rejected with the reason. Returns what was recorded and what was rejected.",
             {"plans": {"type": "array", "items": {"type": "object"}}}, ["plans"]),
]
MOUNTS = {"units.json": "/workspace/campaign/units.json", "resources.json": "/workspace/campaign/resources.json"}


def lead_system(planners: int) -> str:
    return (f'<adv_day4_hosted_lead name="{LEAD}" planners="{planners}">\nYou coordinate recall remedy campaign RC-2026-03. '
            "The campaign data and a resources snapshot are mounted in the workspace (/workspace/campaign/units.json, "
            "/workspace/campaign/resources.json). List the affected units, send the unit-investigator one batch of customers "
            f"per task, send the schedule-planner the assessments ({planners} planner task(s) in parallel), commit with "
            "record_plan, re-plan whatever the system of record rejects from a fresh snapshot, have a copy of yourself check "
            "the final plans, then report. Never promise compensation.\n</adv_day4_hosted_lead>")


class Commits:
    """The custom tools' executor. `record_plan` is where YOUR policy runs: the agents only propose."""

    def __init__(self, desk: d4.RecallDesk) -> None:
        self.desk = desk
        self.plans: dict[str, dict] = {}                    # serial -> the latest proposed plan
        self.log: list[dict] = []                           # one entry per record_plan call

    def answer(self, event) -> tuple[str, bool]:
        if event.name == "list_affected_units":
            return json.dumps(self.desk.run("list_affected_units", {})), False
        if event.name == "get_resources":
            return json.dumps(self.desk.resources()), False
        if event.name == "record_plan":
            recorded, rejected = [], []
            for plan in event.input.get("plans") or []:
                self.plans[plan.get("serial")] = plan
                try:
                    recorded.append(self.desk.commit(plan))
                except d4.ToolFailure as exc:
                    rejected.append({"serial": plan.get("serial"), "reason": str(exc)})
            result = {"recorded": len(recorded), "scheduled": sum(r.get("committed", False) for r in recorded),
                      "rejected": rejected}
            self.log.append(result)
            return json.dumps(result), False
        return json.dumps({"error": f"unknown tool {event.name}"}), True


def setup_agents(client, planners: int):
    investigator = d4.get_or_create_agent(client, "unit-investigator", model=MODEL, system=INVESTIGATOR_SYSTEM, tools=[READ_ONLY],
                                          description="Assesses a batch of recall units from the mounted campaign file; read-only.")
    planner = d4.get_or_create_agent(client, "schedule-planner", model=MODEL, system=PLANNER_SYSTEM, tools=[READ_ONLY],
                                     description="Assigns a kit warehouse and an engineer slot to assessed units; read-only.")
    lead = d4.get_or_create_agent(client, LEAD, model=MODEL, system=lead_system(planners),
                                  tools=[d4.custom_tool(t) for t in DATA_TOOLS] + [{"type": "agent_toolset_20260401"}],
                                  description="Coordinates the recall campaign: shares data, delegates, commits, reports.",
                                  multiagent={"type": "coordinator", "agents": [investigator.id, planner.id, {"type": "self"}]})
    return investigator, planner, lead


def mount_campaign_files(client, desk: d4.RecallDesk) -> list[dict]:
    """Upload the campaign data and the resources snapshot with the Files API; return the session resources that
    mount them in the workspace (plus each file's size, for the printout). Data goes to the workers by reference,
    not through anyone's context."""
    contents = {"units.json": json.dumps(desk.campaign_data()), "resources.json": json.dumps(desk.resources())}
    resources = []
    for name, text in contents.items():
        data = text.encode("utf-8")
        uploaded = client.files.upload(file=(name, data, "application/json"))
        resources.append({"type": "file", "file_id": uploaded.id, "mount_path": MOUNTS[name], "_bytes": len(data)})
    return resources


def run_hosted(client, desk: d4.RecallDesk, *, planners: int = 2, on_event=None, budget_cents: int = 200):
    """One hosted campaign session. Returns (session id, stop reason, Commits, all events, mounted resources)."""
    _, _, lead = setup_agents(client, planners)
    env = d4.get_or_create_environment(client, "kestrel-recall-lab")
    commits = Commits(desk)
    mounts = mount_campaign_files(client, desk)
    session = client.beta.sessions.create(
        agent=lead.id, environment_id=env.id, title="RC-2026-03 hosted swarm",
        budget={"type": "limit", "max_list_cost": {"amount": str(budget_cents), "currency": "USD"}},
        resources=[{k: v for k, v in m.items() if not k.startswith("_")} for m in mounts])
    client.beta.sessions.events.send(session.id, events=[{"type": "user.message", "content": [{"type": "text", "text":
        "Run recall campaign RC-2026-03: a remedy plan for every affected unit, committed to our systems."}]}])
    events: list = []

    def keep(ev) -> None:
        events.append(ev)
        if on_event is not None:
            on_event(ev)

    stop = d4.drive_session(client, session.id, commits.answer, keep)
    return session.id, stop, commits, events, mounts


def final_plans(commits: Commits) -> list[dict]:
    """The latest proposed plan per serial, as the system of record committed it."""
    out = []
    for serial, plan in commits.plans.items():
        booking = commits.desk.bookings.get(serial)
        out.append({**plan, "slot_id": booking["slot_id"] if booking else None,
                    "status": "scheduled" if booking else plan.get("status")})
    return out


def hosted_timeline(events: list, model: str = MODEL) -> dict:
    """Attribute each model request to the primary thread or a sub-thread by the order of events (the mock runs a
    thread inside the coordinator's tool call), then model the critical path: the primary's requests in sequence,
    plus, for every coordinator turn that messaged threads, the slowest of those threads (they run in parallel)."""
    active: list[str] = []
    names: dict[str, str] = {}
    per_thread: dict[str, float] = {}
    requests: dict[str, int] = {}
    groups: list[set[str]] = []
    primary = 0.0
    for ev in events:
        if ev.type == "session.thread_created":
            names[ev.session_thread_id] = ev.agent_name
        elif ev.type == "session.thread_status_running":
            if not active and (not groups or groups[-1] is None):
                groups.append(set())
            active.append(ev.session_thread_id)
            groups[-1].add(ev.session_thread_id)
        elif ev.type == "session.thread_status_idle" and active:
            active.pop()
        elif ev.type == "span.model_request_end":
            seconds = d4.modelled_seconds(ev.model_usage.model_dump(), model)
            if active:
                per_thread[active[-1]] = per_thread.get(active[-1], 0.0) + seconds
                requests[active[-1]] = requests.get(active[-1], 0) + 1
            else:
                primary += seconds
                groups.append(None)                          # a new coordinator turn closes the fan-out group
    fanouts = [g for g in groups if g]
    critical = primary + sum(max(per_thread.get(t, 0.0) for t in g) for g in fanouts)
    return {"primary": primary, "threads": per_thread, "requests": requests, "names": names, "critical": critical,
            "sequential": primary + sum(per_thread.values())}


def self_hosted_baseline(client) -> dict:
    """Lab 01's tuned swarm on the same 11 units, measured the same way (no crash, no stray task)."""
    lab01 = d4.load_lab("01_durable_coordinator")
    db = d4.fresh_db("lab06_selfhosted.db")
    store, queue, desk = RunStore(db), d4.WorkQueue(db), d4.RecallDesk()
    outcome, dispatcher = lab01.run_campaign(client, store, queue, desk, profile="tuned", log=lambda *_: None)
    meter = d4.Meter()
    meter.add_run("coordinator", store, lab01.RUN_ID)
    workers = []
    for record in dispatcher.records.values():
        meter.add_run("worker", store, record["run_id"])
        workers.append(d4.run_seconds(store, record["run_id"]))
    coordinator_s = d4.run_seconds(store, lab01.RUN_ID)
    plans = [p for r in queue.results() for p in r["payload"]["plans"]]
    return {"meter": meter, "score": d4.score(plans, desk), "critical": coordinator_s + max(workers),
            "sequential": coordinator_s + sum(workers), "outcome": outcome}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--planners", type=int, default=2, help="planner tasks the lead runs in parallel (1 avoids the conflict)")
    args = parser.parse_args()

    header("Lab 06 - Managed coordinator: roster, threads, shared workspace, commits through your code")
    d4.mock_note("the agents are rule-based stand-ins; roster, threads, events, custom tools and the shared workspace are "
                 "simulated with the API's shapes. The mock runs each send_to_agent to completion before returning the "
                 "reply; the platform returns at once and delivers the report in a later coordinator turn.")
    client = get_client()
    desk = d4.RecallDesk()

    step(1, "The roster: two worker agents and the coordinator itself - one level of delegation")
    investigator, planner, lead = setup_agents(client, args.planners)
    roster = [m.id if getattr(m, "type", "") == "agent" else m.type for m in lead.multiagent.agents]
    print(f"  {lead.name} (version {lead.version}) roster: {investigator.name}, {planner.name}, self  -> {len(roster)} entries")
    print(f"  workers are read-only (read/glob/grep); only {lead.name} holds the custom tools that touch Kestrel's systems")
    try:
        client.beta.agents.create(name="campaign-director", model=MODEL, system="Direct several recall leads.",
                                  multiagent={"type": "coordinator", "agents": [lead.id]})
        print("  a coordinator of coordinators was accepted (unexpected)")
    except anthropic.BadRequestError as exc:
        print(f"  a 'campaign-director' whose roster holds {lead.name} -> 400: {exc.body.get('error', {}).get('message', exc) if isinstance(exc.body, dict) else exc}")
    print("  So the hierarchy is at most lead -> workers: a deeper tree needs your own orchestration (lab 01's queue).")

    step(2, "One session; the primary stream (spans, usage and thinking events omitted)")
    shown = {"session.thread_created", "agent.thread_message_sent", "agent.thread_message_received", "agent.custom_tool_use",
             "session.status_idle", "user.custom_tool_result"}

    def show(ev) -> None:
        if ev.type in shown:
            print("    " + d4.describe_event(ev, width=64))

    session_id, stop, commits, events, mounts = run_hosted(client, desk, planners=args.planners, on_event=show)
    print(f"\n  session ended: stop_reason={stop.type if stop else None}")
    size = {m["file_id"]: m["_bytes"] for m in mounts}
    listed = client.beta.sessions.retrieve(session_id).resources
    print("  session.resources - Files API uploads mounted into the container at creation: "
          + ", ".join(f"{r.mount_path} ({size.get(r.file_id, 0) // 1000} kB)" for r in listed))
    d4.mock_note("the session's container is the directory .runs/mock_sessions/<session id>; the mounted files are in "
                 "its workspace/campaign/.")

    step(3, "Commits: where the stale snapshot was caught")
    for i, entry in enumerate(commits.log, 1):
        print(f"  record_plan #{i}: {entry['recorded']} recorded ({entry['scheduled']} scheduled), {len(entry['rejected'])} rejected")
        for r in entry["rejected"]:
            print(f"    - {r['serial']}: {r['reason']}")
    plans = final_plans(commits)
    scored = d4.score(plans, desk)
    print(f"  final: {scored['ok']}/{scored['total']} units scheduled correctly; "
          f"{len({b['slot_id'] for b in desk.bookings.values()})} distinct slots booked, stock left {desk.stock['MS-250-R']} (MS-250-R)")
    if commits.log and commits.log[0]["rejected"]:
        print(wrap("Both planners read the same mounted resources.json and each took the earliest slots and kits it saw. "
                   "Threads share a filesystem, not transactions: the system of record, behind a custom tool, refused the "
                   "second booking of a slot and the last WH-EAST board - and the lead's re-plan from a fresh snapshot, in "
                   "the first planner's existing thread (threads persist), fixed it at the price of extra turns. One "
                   "planner owning the calendar (--planners 1) avoids the conflict.", "  "))

    step(4, "Threads: one context per delegated task, one budget for the session")
    rows = []
    for t in client.beta.sessions.threads.list(session_id):
        u = d4.usage_tokens(t.usage)
        rows.append([t.agent.name, "primary" if t.parent_thread_id is None else "child", t.status,
                     f"{d4.prompt_tokens(u):,}", f"{u['cache_read_input_tokens']:,}", f"{u['output_tokens']:,}",
                     f"{t.usage.list_cost.amount}c"])
    print(d4.table(rows, ["agent", "thread", "status", "prompt tok", "cache read", "output tok", "list cost"]))
    spend = d4.session_spend(client, session_id)
    print(f"  session: {spend['requests']} model requests, {d4.prompt_tokens(spend):,} prompt tokens "
          f"({spend['cache_read_input_tokens']:,} read from the cache) and {spend['output_tokens']:,} output; list_cost "
          f"{spend['list_cost_cents']} cents of a 200-cent budget (exact {d4.money(spend['cost'])})")

    step(5, "Hosted vs self-hosted on the same 11 units (lab 01's tuned swarm, re-run here without the crash)")
    base = self_hosted_baseline(client)
    timeline = hosted_timeline(events)
    total = base["meter"].total()
    rows = [["self-hosted (lab 01)", f"{base['score']['ok']}/11", total.calls, f"{total.input_tokens:,}", f"{total.output_tokens:,}",
             d4.money(total.cost), d4.money(total.uncached_cost), f"{base['critical']:.0f}s", f"{total.largest_prompt:,}"],
            ["hosted (this lab)", f"{scored['ok']}/11", spend["requests"], f"{d4.prompt_tokens(spend):,}",
             f"{spend['output_tokens']:,}", d4.money(spend["cost"]), d4.money(d4.uncached_cost(spend)),
             f"{timeline['critical']:.0f}s", f"{spend['largest_prompt']:,}"]]
    print(d4.table(rows, ["architecture", "scheduled", "requests", "prompt tok", "output tok", "cost", "cost uncached",
                          "latency*", "largest prompt"]))
    print("  * modelled critical path (labs/_day4.py LATENCY_ASSUMPTIONS): workers and threads in parallel; not a measurement.")
    print("  Both sides cache: the self-hosted requests carry cache_control, and the session caches its own history.")
    d4.mock_note("the platform also bills $0.08 per session-hour of running time, which the mock does not count; "
                 "measure the hosted bill live.")
    rows = [["work queue, priorities, dead letters", "yours (SQLite queue)", "none: the lead's turn order is the schedule"],
            ["crash of the orchestrator", "resume from the run log (lab 01)", "platform: the session and threads persist"],
            ["parallel workers", "your worker pool", "threads (up to 25 concurrent per session)"],
            ["sandbox and shared files", "yours", "one container per session, shared by all threads"],
            ["side effects and their idempotency", "your executor + ctx.effect()", "yours: custom tools + your desk"],
            ["policy at commit (rules, conflicts)", "yours", "yours: record_plan validated every plan"],
            ["cost cap", "your guards (lab 03)", "session budget shared by all threads"],
            ["hierarchy depth", "anything you build", "one level: lead -> roster"]]
    print(d4.table(rows, ["concern", "self-hosted (labs 01-03)", "hosted (Managed Agents)"]))
    print(f"\nUsage summary - Managed Agents session (from span.model_request_end; not in labkit's Messages API ledger)\n"
          f"  1 session  {spend['requests']} requests  {d4.money(spend['cost'])}"
          + ("" if is_mock() else f"  (list_cost {spend['list_cost_cents']} cents)"))


if __name__ == "__main__":
    main()
