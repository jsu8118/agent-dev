"""Lab 07 - Replay and time travel: reproduce a bug from the log offline, fork the run, export the trace.

Objective
    Investigate a reported problem the way the log lets you: rebuild the transcript without a single model call,
    find the turn where it happened, replay the read-only tools against a sandbox to check for drift, then fork
    the run at one event with a different tool result and let the model continue from there - what would the
    copilot have done if the numbers had been different?  Finish by exporting the log to labkit.tracing as an
    OpenTelemetry-style span tree, and by deciding what belongs in the log: PII, thinking signatures, cost.

Concepts
    offline replay (rebuild without the model), reads re-executed vs writes stubbed, drift between the logged
    world and today's, forks (copy the log up to an event, patch it, continue), preserved-thinking constraints on
    forks, the log-to-trace mapping (gen_ai.* attributes), redaction, what to keep and for how long.

Run
    python advanced/day1_durable_agents/labs/07_replay_and_time_travel.py

What to observe
    * Step 2 makes zero model calls and finds the turn where issue_refund was attempted although the customer
      asked for confirmation first - the finding is in the log, not in the reply.
    * Step 3: the replay re-runs reads (no drift) and stubs the writes; the escalation is not created again.
    * Step 4: the fork with refund_due_usd = 2,400 continues from the patched result and issues the refund - the
      latent bug, reproduced in a sandbox, without touching the original run.
    * Step 5: the span tree with one llm.call per turn and one tool.* span per call, cost per turn included.
"""
# test: expect=model calls made: 0
# test: expect=fork

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import RunStore
from kestrel.support_tools import SupportDesk
from labkit import MODEL, get_client, header, is_mock, mock_api, step, wrap
from labkit.data import memory_db
from labkit.tracing import Tracer

import _day1 as d1

TICKET = "T-1207"      # Midland Oil: "please confirm the figure before you issue it" - RMA-7001, $9,188.50 due
READS = {"get_customer_profile", "get_order", "list_customer_orders", "get_invoice", "get_rma",
         "check_return_eligibility", "check_warranty", "search_knowledge_base"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def run_original(store, client, ticket, db):
    run = store.create("support", input=d1.run_input(ticket), run_id="sd-T-1207")
    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    outcome = d1.support_runner(store, client, execute=d1.desk_executor(desk), worker="worker-a").run(run.id)
    print(f"{d1.outcome_line(outcome)}  tools: {' -> '.join(d1.tool_calls(store, run.id))}")
    print("reply:\n" + wrap(outcome.reply))
    return run, outcome


def step_investigate(store, client, run, ticket):
    print("Bug report from the support manager: \"the copilot tried to push a $9,188.50 refund the customer "
          "explicitly asked us to confirm first\".")
    before = len(mock_api().request_log)
    runner = d1.support_runner(store, client, execute=lambda *a: {"error": "offline"}, worker="investigator")
    messages, results, turns, replayed = runner.rebuild(run.id)
    print(f"\nrebuild(): {len(messages)} messages, {turns} turns, {replayed} tool results - model calls made: "
          f"{len(mock_api().request_log) - before}")
    d1.print_transcript(messages)
    attempts = [e for e in store.events(run.id, types=("tool.started",)) if e["name"] == "issue_refund"]
    response = next(e for e in store.events(run.id, types=("model.response",))
                    if any(b.get("type") == "tool_use" and b["name"] == "issue_refund" for b in e["content"]))
    ask = next(s for s in ticket["body"].split(".") if "confirm" in s).strip()
    print(f"\nEvidence: at turn {response['turn']} the model called issue_refund with {attempts[0]['input']}")
    print(f"          the ticket says: \"{ask}.\"")
    result = next(e for e in store.events(run.id, types=("tool.result",)) if e["name"] == "issue_refund")
    print(f"          the tool refused ({'is_error' if result['is_error'] else 'ok'}): {d1.short(result['content'], 90)}")
    print(wrap("Nothing was refunded, so no dashboard flagged it; the reply even reads well. Only the log shows "
               "the intent. In the first course this needed a tracing backend and luck; here it is a SELECT."))
    if is_mock():
        print("[mock] the stand-in policy issues the refund as soon as it sees a received RMA - that is the "
              "behaviour under investigation. Live, Claude may ask first; the log shows which.")


def replay_tools(store, run_id: str, desk: SupportDesk) -> None:
    """Re-execute the READ tools of a logged run against a sandbox and compare; stub the writes from the log."""
    logged = {e["tool_use_id"]: e for e in store.events(run_id, types=("tool.result",))}
    for started in store.events(run_id, types=("tool.started",)):
        name, was = started["name"], logged[started["tool_use_id"]]
        if name in READS:
            content, _ = desk.run(name, started["input"])
            verdict = "no drift" if content == was["content"] else "DRIFT: " + d1.short(content, 60)
            print(f"  {name:<22} read   re-executed -> {verdict}")
        else:
            print(f"  {name:<22} write  stubbed from the log -> {d1.short(was['content'], 60)}")


def step_replay(store, run, ticket, db):
    print("Replay against the production copy (the database the run used):")
    replay_tools(store, run.id, SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"]))
    print(f"  escalations in that database after the replay: {db.execute('SELECT COUNT(*) FROM escalations').fetchone()[0]} "
          "(still the one the run created)")
    print(wrap("A replay harness must classify tools: reads are re-executed and diffed (drift means the world has "
               "moved since the run: the RMA got refunded, the order shipped), writes are answered from the log. "
               "Re-executing a write during an investigation is how a second escalation, email or refund happens."))


def fork_run(store: RunStore, source_id: str, fork_id: str, *, at_seq: int, patch: Callable[[dict], dict]):
    """Copy the source run's log up to `at_seq` into a new run, applying `patch` to the event at `at_seq`."""
    source = store.get(source_id)
    fork = store.create(source.kind, input=source.input, run_id=fork_id, tags={**source.tags, "fork_of": source_id,
                                                                                "fork_at": at_seq})
    copied = 0
    for e in store.events(source_id):
        if e["seq"] > at_seq:
            break
        if e["type"] not in ("model.response", "tool.started", "tool.result", "user.message"):
            continue
        payload = {k: v for k, v in e.items() if k not in ("seq", "type", "at")}
        if e["seq"] == at_seq:
            payload = patch(payload)
        store.append(fork.id, e["type"], payload)
        copied += 1
    return fork, copied


def step_fork(store, client, run, ticket):
    rma_result = next(e for e in store.events(run.id, types=("tool.result",)) if e["name"] == "get_rma")
    new_due = 2400.0

    def patch(payload: dict) -> dict:
        data = json.loads(payload["content"])
        data["refund_due_usd"] = new_due
        return {**payload, "content": json.dumps(data)}

    fork, copied = fork_run(store, run.id, "sd-T-1207-fork", at_seq=rma_result["seq"], patch=patch)
    print(f"fork {fork.id}: {copied} events copied up to seq {rma_result['seq']} (get_rma's result), "
          f"refund_due_usd patched {json.loads(rma_result['content'])['refund_due_usd']} -> {new_due}")
    sandbox = memory_db()                                          # the world must agree with the patched log
    sandbox.execute("UPDATE rmas SET refund_due_usd = ? WHERE rma_id = 'RMA-7001'", (new_due,))
    sandbox.commit()
    desk = SupportDesk(ticket["from_email"], db=sandbox, ticket_ref=ticket["ticket_id"])
    outcome = d1.support_runner(store, client, execute=d1.desk_executor(desk), worker="time-traveller").run(fork.id)
    print(f"continued: {d1.outcome_line(outcome)}  tools: {' -> '.join(d1.tool_calls(store, fork.id))}")
    print("reply:\n" + wrap(outcome.reply))
    refunds = [dict(r) for r in sandbox.execute("SELECT refund_id, amount_usd, status FROM refunds")]
    print(f"sandbox refunds: {refunds}   original run's status: {store.get(run.id).status} (untouched)")
    print(wrap("Under the agent's limit the copilot would have issued the refund without confirming the figure: "
               "the tool's approval limit was the only thing between the customer's request and the money. That "
               "is a prompt-and-eval finding for Day 6, found without waiting for it to happen in production."))
    print(wrap("Forks edit history, and preserved thinking binds each thinking block to the bytes before it. A fork "
               "at a tool RESULT leaves every earlier block's prefix intact - the patched bytes come after them - "
               "and the continuation generates fresh blocks; a fork that rewrites an earlier turn or the system "
               "prompt would need thinking.block_binding.prefix_mismatch_behavior: drop_block (beta "
               "thinking-binding-controls-2026-08-01) on models that enforce the check, or a model that does not."))


def log_to_trace(store: RunStore, run_id: str, model: str = MODEL) -> Tracer:
    """Map the event log to spans: run -> llm.call per turn -> tool.<name> per call, with OTel GenAI attributes."""
    ts = lambda e: dt.datetime.fromisoformat(e["at"]).timestamp()
    events = store.events(run_id)
    tracer = Tracer("durable-support", trace_id=run_id.replace("-", "")[:16].ljust(16, "0"))
    with tracer.span("agent.run", **{"run.id": run_id, "run.kind": store.get(run_id).kind}) as root:
        root.start = ts(events[0])
        turn_span = None
        for i, e in enumerate(events):
            end = ts(events[i + 1]) if i + 1 < len(events) else ts(e)
            if e["type"] == "model.response":
                with tracer.span("llm.call", turn=e["turn"]) as s:
                    s.record_llm({"model": model, "id": "", "stop_reason": e["stop_reason"], "usage": e["usage"]})
                    s.start, s.end = ts(events[i - 1]), ts(e)
                turn_span = s
            elif e["type"] == "tool.started":
                with tracer.span(f"tool.{e['name']}", **{"tool.input": d1.short(e["input"], 80),
                                                        "tool.use_id": e["tool_use_id"]}) as s:
                    result = next((r for r in events[i + 1:] if r["type"] == "tool.result"
                                   and r["tool_use_id"] == e["tool_use_id"]), None)
                    if result and result.get("is_error"):
                        s.error(d1.short(result["content"], 80))
                    s.start, s.end = ts(e), ts(result) if result else end
            elif e["type"] == "approval.requested":
                with tracer.span("approval.wait", **{"approval.summary": e["action"].get("summary", "")}) as s:
                    s.start, s.end = ts(e), end
        root.end = ts(events[-1])
    return tracer


def step_trace(store, run):
    tracer = log_to_trace(store, run.id)
    print(tracer.render_tree())
    path = tracer.export()
    print(f"\nexported {len(tracer.spans)} spans to {path.relative_to(path.parents[2])}")
    print("  log field                     span attribute (OTel GenAI semantic conventions)")
    print("  model.response.usage.*        gen_ai.usage.input_tokens / output_tokens / cache_*")
    print("  model.response.stop_reason    gen_ai.response.finish_reason")
    print("  tool.started.name / input     span name tool.<name>, tool.input (truncated, redacted)")
    print("  tool.result.is_error          span status error + error.message")
    print("  run_id                        trace_id (one trace per run; a resume continues the same trace)")
    print(wrap("The log is the source of truth; the trace is a projection of it for humans and dashboards. Export "
               "spans from the log (as here, or from a sweeper) rather than from the worker's memory, and a "
               "crashed worker's spans still exist. Durations come from event timestamps, so they include the "
               "time between events, not only the time inside them."))


def redact(event: dict) -> dict:
    """Mask email addresses in anything a log consumer might see; keep signatures (needed to resume) verbatim."""
    text = json.dumps(event, default=str)
    return json.loads(EMAIL_RE.sub("<email>", text))


def step_what_to_log(store, run):
    created = store.events(run.id, types=("run.created",))[0]
    print("run.created as logged :", d1.short(created["input"], 110))
    print("run.created redacted  :", d1.short(redact(created)["input"], 110))
    thinking = next(b for e in store.events(run.id, types=("model.response",)) for b in e["content"] if b["type"] == "thinking")
    print(f"a thinking block in the log: text={thinking['thinking']!r} signature={thinking['signature'][:28]}...")
    per_turn = [(e["turn"], e["usage"]["input_tokens"] + e["usage"]["cache_read_input_tokens"]
                 + e["usage"]["cache_creation_input_tokens"], e["usage"]["output_tokens"])
                for e in store.events(run.id, types=("model.response",))]
    print(f"cost per turn from usage: {[(t, f'{i:,} in', f'{o} out') for t, i, o in per_turn]} -> ${d1.run_cost(store, run.id):.4f}")
    print("  what                      keep?   why")
    print("  requester email, names    masked  PRV-004: the run needs the channel identity, log readers do not")
    print("  tool inputs and results   yes     the evidence; redact PII fields, cap sizes, never drop errors")
    print("  thinking blocks           yes     signatures are required to resume on the same model; text is empty")
    print("                                    with display omitted and must not be summarised into the log")
    print("  usage per turn            yes     the bill, per run and per turn, without a second system")
    print("  model id                  yes     preserved thinking is model-bound: a resume on another model drops it")
    print("  retention                 policy  as long as the business record (refund, RMA) plus the audit window")
    print(wrap("Redact at the boundary readers cross (exports, traces, dashboards), not in the log itself: the "
               "resuming worker needs the exact bytes. Keep the log's own access as tight as the systems it "
               "mirrors - it holds everything the tools returned."))


def main() -> None:
    client = get_client()
    header("Lab 07 - Replay and time travel")
    if is_mock():
        print("[mock] the stand-in model's behaviour on this ticket is the bug under investigation; the rebuild, "
              "the replay harness, the fork and the trace export are real code.")
    ticket = d1.ticket(TICKET)
    store = d1.fresh_store("07_replay")
    db = memory_db()

    step(1, "The original run")
    run, _ = run_original(store, client, ticket, db)

    step(2, "Reproduce from the log, offline")
    step_investigate(store, client, run, ticket)

    step(3, "Replay the tools: reads re-executed, writes stubbed")
    step_replay(store, run, ticket, db)

    step(4, "Fork the run at one event with a different tool result")
    step_fork(store, client, run, ticket)

    step(5, "Export the log as a trace")
    step_trace(store, run)

    step(6, "What to log")
    step_what_to_log(store, run)


if __name__ == "__main__":
    main()
