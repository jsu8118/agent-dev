"""Lab 05 - Managed Agents sessions: the recall assessor as a hosted agent - custom tools, events, usage and a budget.

Objective
    Move one piece of the swarm onto Claude Managed Agents: a persisted, versioned agent with a custom tool
    (`get_unit`, answered by YOUR system of record) and the built-in agent toolset (it writes its assessment into
    the session's container), an environment, and a session. Send a message, answer the custom tool call when the
    session stops with `requires_action`, stream the events, read usage and list cost - then run a second session
    into its budget and watch it pause instead of overspending.

Concepts
    agents (create once, update in place, versions, pinning), environments (unique names), sessions and their
    status, the event stream (user.*, agent.*, session.*, span.*), custom-tool round trips (agent.custom_tool_use ->
    idle with requires_action -> user.custom_tool_result), the built-in toolset, stream-first + history
    consolidation, usage and list_cost, session budgets (a pre-request gate), hosted runtime vs Day 1's runtime

Run
    python advanced/day4_orchestration_at_scale/labs/05_managed_agents_sessions.py

What to observe
    * Step 1: the agent and environment are looked up before they are created - they are persistent resources,
      not per-run objects; the session pins the agent version it runs.
    * Step 2: after the user message the session is idle with stop_reason requires_action and the id of the
      agent.custom_tool_use event: the agent is waiting for YOUR code.
    * Step 3: one user.custom_tool_result later, the agent writes its assessment with the built-in `write` tool
      (inside the session's container) and ends its turn; the stream shows every step.
    * Step 4: usage per model request (span.model_request_end) adds up to the session's usage; list_cost is in cents.
    * Step 5: a session with a 6-cent budget stops with stop_reason budget_reached part-way through six units.
"""
# test: expect=requires_action
# test: expect=budget_reached

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, runs_dir, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402

AGENT_NAME = "kestrel-recall-assessor"
ENVIRONMENT = "kestrel-recall-lab"
SYSTEM = ("<adv_day4_hosted_assessor>\nYou assess affected units of Kestrel recall campaign RC-2026-03 for the field-service "
          "team. For each serial you are given: call get_unit (Kestrel's system of record), then write the assessment to "
          "recall/<serial>.json in your workspace (serial, customer_id, risk_class, remedy, kit_sku, skill_required, "
          "contact_by, remedy_by, interim_measure), then summarize. One unit at a time. Never promise compensation and "
          "never change hazard wording.\n</adv_day4_hosted_assessor>")
TOOLS = [d4.custom_tool(next(t for t in d4.WORKER_TOOLS if t["name"] == "get_unit")), {"type": "agent_toolset_20260401"}]


def answer_with(desk: d4.RecallDesk):
    """The custom tool's executor: your code, your system of record, your policy. Returns (text, is_error)."""
    def answer(event) -> tuple[str, bool]:
        try:
            return json.dumps(desk.run(event.name, dict(event.input))), False
        except d4.ToolFailure as exc:
            return json.dumps({"error": str(exc)}), True
    return answer


def main() -> None:
    header("Lab 05 - Managed Agents: sessions, events, custom tools, usage, budgets")
    d4.mock_note("the agent loop is a rule-based stand-in; the objects, event stream, custom-tool round trip, built-in "
                 "tools (run in .runs/mock_sessions/<session id>) and budget gate are simulated with the API's shapes. "
                 "The mock processes events synchronously and its stream replays history; the platform does neither.")
    client = get_client()
    desk = d4.RecallDesk()

    step(1, "Agent, environment, session: create once, reference by id (beta header managed-agents-2026-04-01)")
    agent = d4.get_or_create_agent(client, AGENT_NAME, model=MODEL, system=SYSTEM, tools=TOOLS,
                                   description="Assesses one affected recall unit at a time for the field-service team.")
    tool_names = [t.name if getattr(t, "type", "") == "custom" else t.type for t in agent.tools]
    print(f"  agent {agent.name}: version {agent.version}, model {agent.model.id}, tools {tool_names}")
    env = d4.get_or_create_environment(client, ENVIRONMENT)
    print(f"  environment {env.name}: config {env.config.type}, networking {env.config.networking.type}")
    session = client.beta.sessions.create(agent={"type": "agent", "id": agent.id, "version": agent.version},
                                          environment_id=env.id, title="Assess KP250-2608-0006",
                                          metadata={"campaign": "RC-2026-03"})
    print(f"  session created: status={session.status}, pinned to agent version {session.agent.version}")
    print("  (model, system prompt and tools live on the agent; the session only points at it)")

    step(2, "Stream first, then send a user message; the session stops for YOUR custom tool")
    seen: set[str] = set()
    idle = use = None
    with client.beta.sessions.events.stream(session.id) as stream:          # open the stream BEFORE sending
        client.beta.sessions.events.send(session.id, events=[{"type": "user.message", "content": [{"type": "text", "text":
            "Assess unit KP250-2608-0006 for the field-service team."}]}])
        for ev in stream:
            seen.add(ev.id)
            print("    " + d4.describe_event(ev))
            if ev.type == "agent.custom_tool_use":
                use = ev
            if ev.type == "session.status_idle":
                idle = ev
                break
    print(f"\n  The session is idle with stop_reason={idle.stop_reason.type}; event {use.id} asks for {use.name}{json.dumps(use.input)}.")
    print("  Nothing happens until your code answers - the tool runs in your process, against your system of record.")

    step(3, "Answer with user.custom_tool_result, then read the stream until the turn ends")
    text, is_error = answer_with(desk)(use)
    client.beta.sessions.events.send(session.id, events=[{"type": "user.custom_tool_result", "custom_tool_use_id": use.id,
                                                           "content": [{"type": "text", "text": text}]}])
    stop = d4.drive_session(client, session.id, answer_with(desk), lambda ev: print("    " + d4.describe_event(ev)), seen=seen)
    print(f"\n  turn ended: stop_reason={stop.type}")
    print("  (The first line above came from the history, not the stream: the typed SSE stream of SDK 1.8 does not yield")
    print("  session.usage events. Consolidating stream + history by event id is how you get every event exactly once.)")
    if is_mock():
        written = runs_dir("mock_sessions", session.id) / "recall" / "KP250-2608-0006.json"
        data = json.loads(written.read_text(encoding="utf-8"))
        print(f"  [mock] the built-in write tool ran in the session's container (here {written.parent.name}/ under "
              f".runs/mock_sessions/<session>): {len(data)} fields, remedy_by={data['remedy_by']}")
    else:
        print("  The assessment is in the session's container; fetch session outputs through the Files API if you need them.")

    step(4, "Usage and cost: per request, per session")
    spend = d4.session_spend(client, session.id)
    print(f"  {spend['requests']} model requests: input {spend['input_tokens']:,} (cache read {spend['cache_read_input_tokens']:,}), "
          f"output {spend['output_tokens']:,} tokens -> {d4.money(spend['cost'])} at list prices")
    fetched = client.beta.sessions.retrieve(session.id)
    print(f"  sessions.retrieve().usage: input {fetched.usage.input_tokens:,}, output {fetched.usage.output_tokens:,}, "
          f"list_cost {fetched.usage.list_cost.amount} cents ({fetched.usage.list_cost.currency}; rounded to the cent)")
    d4.mock_note("the platform caches session history automatically and adds $0.08 per session-hour of running time to "
                 "list_cost; the mock bills every input token uncached and counts no running time.")

    step(5, "A session budget: a hard cap on list cost, checked before every model request")
    targets = ["KP250-2608-0007", "KP250-2608-0008", "KP100-2608-0002", "KP250-2608-0002", "KP250-2608-0003", "KP250-2608-0004"]
    capped = client.beta.sessions.create(agent=agent.id, environment_id=env.id, title="Assess six units on a budget",
                                         budget={"type": "limit", "max_list_cost": {"amount": "6", "currency": "USD"}})
    client.beta.sessions.events.send(capped.id, events=[{"type": "user.message", "content": [{"type": "text", "text":
        "Assess units " + ", ".join(targets) + " for the field-service team."}]}])
    log: list = []
    stop = d4.drive_session(client, capped.id, answer_with(desk), log.append)
    written = [e.input["path"] for e in log if e.type == "agent.tool_use" and e.name == "write"]
    spend = d4.session_spend(client, capped.id)
    print(f"  budget 6 cents -> stop_reason={stop.type} after {spend['requests']} model requests; "
          f"{len(written)} of {len(targets)} assessments written ({', '.join(Path(p).stem for p in written)})")
    print(f"  consumed list cost: {spend['list_cost_cents']} cents (exact {d4.money(spend['cost'])}): the request that crossed "
          "the cap completed, the next one was never made")
    print(wrap("At the cap only settle events are accepted (custom tool results, tool confirmations, interrupts); a "
               "user.message is a 400. Nothing resumes the session except a budget update: raise the cap above the "
               "consumed list cost, or remove it (budget: null; removal is one-way).", "  "))
    client.beta.sessions.update(capped.id, budget={"type": "limit", "max_list_cost": {"amount": "25", "currency": "USD"}})
    updated = [e for e in client.beta.sessions.events.list(capped.id) if e.type == "session.updated"][-1]
    print(f"  sessions.update(budget=25 cents) -> event session.updated, budget {updated.budget.max_list_cost.amount} cents")
    if is_mock():
        print("  [mock] the mock records the new budget but does not resume the paused turn; the platform resumes it "
              "automatically, and live the lab drives the session on to the end of its turn.")
    else:
        stop = d4.drive_session(client, capped.id, answer_with(desk), log.append)
        print(f"  resumed after the budget change: stop_reason={stop.type}")

    step(6, "What the hosted runtime gives you, and what stays yours")
    rows = [["agent loop, retries of model calls", "platform (sessions reschedule on retryable errors)", "your DurableRunner (Day 1)"],
            ["conversation state and history", "platform (event log per session and thread)", "your RunStore event log"],
            ["sandbox for bash / files / code", "platform (a container per session)", "yours to build and harden"],
            ["cost visibility and hard cap", "session usage, list_cost, budget", "LEDGER, your own guards (lab 03)"],
            ["custom tools = side effects", "YOURS (requires_action round trip)", "yours"],
            ["idempotency of your effects", "YOURS (the platform may retry a turn)", "yours (ctx.effect())"],
            ["policy, approvals, evals", "YOURS", "yours"]]
    print(d4.table(rows, ["concern", "Managed Agents", "self-hosted (Day 1 runtime)"]))
    for s in (session, capped):
        client.beta.sessions.archive(s.id)
    print("  Both sessions archived (read-only from now on). The agent and the environment are NOT archived: they are "
          "reused by the next run, and archiving them is permanent.")
    total = d4.session_spend(client, session.id)["cost"] + d4.session_spend(client, capped.id)["cost"]
    print(f"\nUsage summary - Managed Agents sessions (from span.model_request_end; not in labkit's Messages API ledger)\n"
          f"  2 sessions  {d4.money(total)}")


if __name__ == "__main__":
    main()
