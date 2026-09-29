"""Lab 01 - An event-sourced run: the Kestrel support agent on a real ticket, every step in a durable log.

Objective
    Run the first course's support agent on a real ticket - but through `DurableRunner`, so every model
    response and tool result is appended to a SQLite log before the loop moves on.  Read the log, rebuild the
    transcript from it with `rebuild()`, prove it is byte-identical to what the in-memory loop held in Python
    variables, and see what survives once the process is gone: the log, not the variables.

Concepts
    the run as a state machine (pending -> running -> completed), a write-ahead event log,
    model.response / tool.started / tool.result events, rebuild() (replaying the log into the messages
    array), the shared system of record vs the private worker, idempotent run creation, a second process
    reading the same store.

Run
    python advanced/day1_durable_agents/labs/01_event_sourced_run.py

What to observe
    * The event log: run.created, run.status, then one model.response per turn with a tool.started /
      tool.result pair per tool call, then the final run.status - 13 events for a four-turn, three-tool run.
    * The log's size against a snapshot of the messages array after every turn: linear against quadratic.
    * rebuild() returns the messages array this worker held in memory, byte for byte; the first course's loop
      holds the same conversation but not the same bytes (an explicit "is_error": false).
    * A second RunStore opened on the same file sees the run, its status, its reply and its cost; the
      in-memory loop's AgentResult.messages died with its process.
"""
# test: expect=identical: True
# test: expect=run.created

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import RunStore
from kestrel.support_agent import run_support_agent
from kestrel.support_tools import SupportDesk
from labkit import MODEL, get_client, header, is_mock, step, wrap
from labkit.data import memory_db
from labkit.pricing import cost_usd

import _day1 as d1

TICKET = "T-1206"          # Keystone Mechanical: "has RMA-7004 been processed, when is the refund issued?"


def step_create(store: RunStore, ticket: dict):
    print(f"Ticket {ticket['ticket_id']} from {ticket['from_email']}: {ticket['subject']!r}")
    print(wrap(ticket["body"]))
    run = store.create("support", input=d1.run_input(ticket), run_id=f"sd-{ticket['ticket_id']}",
                       tags={"ticket": ticket["ticket_id"], "channel": "email"})
    print(f"\nCreated run {run.id}: status={run.status}, lease_owner={run.lease_owner}")
    print("Events so far:")
    d1.print_log(store, run.id)
    print("\nThe run record is the queue entry: any worker that can open the store may pick it up. Nothing has "
          "been sent to the model yet.")
    return run


def step_run(store: RunStore, client, run, db):
    desk = SupportDesk(run.input["requester_email"], db=db, ticket_ref=run.input["ticket_id"])
    runner = d1.support_runner(store, client, execute=d1.desk_executor(desk), worker="worker-a")
    outcome = runner.run(run.id)
    print(d1.outcome_line(outcome))
    print("Tool calls:", " -> ".join(d1.tool_calls(store, run.id)))
    print("Reply to the customer:\n" + wrap(outcome.reply))
    refunds = [dict(r) for r in db.execute("SELECT refund_id, rma_id, amount_usd, status FROM refunds")]
    print(f"\nSystem of record after the run - refunds table: {refunds}")
    return runner, outcome


def snapshot_points(messages: list[dict]) -> list[int]:
    """Where a snapshotting runtime would save the whole array: after each tool round and after the final answer."""
    return [i + 1 for i, m in enumerate(messages)
            if (m["role"] == "user" and i > 0) or (m["role"] == "assistant" and i == len(messages) - 1)]


def step_log(store: RunStore, run_id: str, messages: list[dict]) -> None:
    d1.print_log(store, run_id)
    events = store.events(run_id)
    print(f"\n{len(events)} events. Every model.response was written BEFORE its tool calls ran, every tool.result "
          "before the next model call: the log is ahead of the world, never behind it.")
    total = sum(cost_usd(e["usage"], MODEL) for e in events if e["type"] == "model.response")
    print(f"The log is also the bill: usage is stored per turn, so this run cost ${total:.4f} and you can say which "
          "turn cost what without a tracing backend.")
    log_bytes = sum(len(json.dumps({k: v for k, v in e.items() if k not in ("seq", "type", "at")}, default=str))
                    for e in events)
    sizes = [len(json.dumps(messages[:n], default=str)) for n in snapshot_points(messages)]
    print(f"\nStorage: the log holds {log_bytes:,} bytes of payload (usage and statuses included). Saving a snapshot "
          f"of the messages array after each of the {len(sizes)} turns instead would store "
          f"{' + '.join(f'{b:,}' for b in sizes)} = {sum(sizes):,} bytes - and the array grows every turn, so "
          "snapshots grow with the square of the turns while the log grows linearly (exercise 3 does the 40-turn "
          "arithmetic).")


def first_difference(a: list[dict], b: list[dict]) -> str:
    """Where two transcripts first diverge, as a path (message index / block index / field)."""
    for i, (x, y) in enumerate(zip(a, b)):
        cx, cy = x["content"], y["content"]
        if isinstance(cx, str) or isinstance(cy, str):
            if cx != cy:
                return f"message {i}: text differs"
            continue
        for j, (bx, by) in enumerate(zip(cx, cy)):
            for key in sorted(set(bx) | set(by)):
                if bx.get(key) != by.get(key):
                    return f"message {i}, block {j} ({bx.get('type')}): field {key!r} = {bx.get(key)!r} vs {by.get(key)!r}"
        if len(cx) != len(cy):
            return f"message {i}: {len(cx)} vs {len(cy)} blocks"
    return "no difference" if len(a) == len(b) else f"{len(a)} vs {len(b)} messages"


def same_conversation(a: list[dict], b: list[dict]) -> bool:
    """Equal once the bytes that don't change the conversation are ignored: an explicit is_error: false and the
    thinking signatures (which are bound to the exact bytes before them, so they differ whenever anything does)."""

    def strip(messages):
        out = []
        for m in d1.normalise(messages):
            content = m["content"]
            if isinstance(content, list):
                blocks = []
                for block in content:
                    block = {k: v for k, v in block.items() if not (k == "is_error" and v is False) and k != "signature"}
                    blocks.append(block)
                content = blocks
            out.append({"role": m["role"], "content": content})
        return json.dumps(out, sort_keys=True, default=str)

    return strip(a) == strip(b)


def signatures(messages: list[dict]) -> list[str]:
    return [b.get("signature", "") for m in d1.normalise(messages) if m["role"] == "assistant"
            for b in m["content"] if b.get("type") == "thinking"]


def step_rebuild(runner, store: RunStore, run, outcome, client, ticket: dict) -> None:
    messages, results, turns, replayed = runner.rebuild(run.id)
    print(f"rebuild() -> {len(messages)} messages, {len(results)} tool results, {turns} turns, "
          f"{replayed} results replayed into tool_result blocks")
    d1.print_transcript(messages)
    same = d1.transcript_bytes(messages) == d1.transcript_bytes(outcome.messages)
    print(f"\nrebuild() == the messages array this worker held in memory -> identical: {same}")
    print("  (tool_use ids, tool results and the signed thinking blocks included: a worker that resumes this run "
          "sends the model exactly the bytes the crashed one would have sent)")

    print("\nThe first course's loop (kestrel.support_agent.run_support_agent) on the same ticket, same request "
          "shape, its own copy of the database:")
    requests_before = d1.model_calls()
    desk = SupportDesk(ticket["from_email"], db=memory_db(), ticket_ref=ticket["ticket_id"])
    result = run_support_agent(client, d1.ticket_message(ticket), ticket["from_email"], desk=desk,
                               ticket_ref=ticket["ticket_id"])
    print(f"  turns={result.turns} tools={[c['name'] for c in result.tool_calls]} "
          f"model calls={d1.model_calls() - requests_before}")
    print(f"  same conversation (ignoring is_error:false and signatures)? {same_conversation(messages, result.messages)}")
    byte_same = d1.transcript_bytes(messages) == d1.transcript_bytes(result.messages)
    print(f"  byte-identical? {byte_same} - first difference: {first_difference(d1.normalise(messages), d1.normalise(result.messages))}")
    same_sig = [a == b for a, b in zip(signatures(messages), signatures(result.messages))]
    print(f"  thinking signature of turn 1..{len(same_sig)} equal in both runs: {same_sig}")
    print(wrap("The first course's loop sends \"is_error\": false on every tool_result; the runner omits the field "
               "when it is false. Both are valid requests with the same meaning, but they are different bytes: here "
               "the two runs' thinking signatures differ from the first tool result on, because the mock binds "
               "each thinking block to the exact JSON before it. Whether the real API normalises this particular "
               "field is not something to rely on. A resumed run must reproduce the crashed worker's BYTES, not "
               "merely its conversation - which is why rebuild() re-creates the tool_result blocks the way the "
               "runner sent them and never re-serialises history (lab 02 breaks this on purpose)."))


def step_second_process(store_path: str, run_id: str, result_messages_len: int) -> None:
    print("The in-memory loop's AgentResult.messages: gone when this process exits (it held "
          f"{result_messages_len} messages a moment ago; nothing wrote them anywhere).")
    other = RunStore(store_path)                        # e.g. the approval UI, a sweeper, tomorrow's worker
    run = other.get(run_id)
    print(f"A second RunStore on {Path(store_path).name}: status={run.status}, turns={run.result['turns']}, "
          f"reply={d1.short(run.result['reply'], 60)!r}")
    again = other.create("support", run_id=run_id)
    print(f"store.create(run_id={run_id!r}) again -> status={again.status}: creation is idempotent, so a ticket "
          "delivered twice by the mail gateway is one run, not two.")
    statuses = [e["status"] for e in other.events(run_id, types=("run.status",))]
    print(f"Status transitions recorded in the log: pending -> {' -> '.join(statuses)}")


def main() -> None:
    client = get_client()
    header("Lab 01 - An event-sourced run")
    if is_mock():
        print("[mock] the model is a rule-based stand-in: it reads the ticket, calls the same tools a model would "
              "and writes the reply from their results. The log, the replay and the comparison are real.")
    ticket = d1.ticket(TICKET)
    store = d1.fresh_store("01_event_sourced")
    db = memory_db()                                    # the system of record, shared by every worker below

    step(1, "A ticket becomes a run record")
    run = step_create(store, ticket)

    step(2, "Run it through DurableRunner")
    runner, outcome = step_run(store, client, run, db)

    step(3, "The event log")
    step_log(store, run.id, outcome.messages)

    step(4, "rebuild(): the transcript from the log vs the in-memory loop")
    step_rebuild(runner, store, run, outcome, client, ticket)

    step(5, "What survives the process")
    step_second_process(store.path, run.id, len(outcome.messages))


if __name__ == "__main__":
    main()
