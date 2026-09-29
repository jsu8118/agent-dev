"""Lab 02 - Crash and resume: kill the worker at every point of the loop and let another one finish the run.

Objective
    Crash the durable support run at each crash point the runner offers (after the model responded, before a
    tool ran, after a tool ran) on every turn, resume each run from a second worker and count what the resumer
    replayed from the log versus executed again.  Add the crash point the runner cannot offer - the response
    arrived but was never logged - and the one it does not handle - the final answer was logged but the run was
    never marked completed.  Then prove the two properties a resume depends on: the resumed worker's first
    request extends the crashed worker's last request byte for byte (append-only), so the prompt cache and the
    signed thinking blocks keep working across the crash.  Finish with the alternative every team tries first:
    re-running the in-memory loop from scratch.

Concepts
    crash points and what each leaves behind; resumption by replay; replayed vs executed tools; at-most-once
    tool execution from the log alone; the one step that stays at-least-once (a model call whose response was
    lost); finishing a run from its log; append-only messages; prompt-cache survival across a resume; the naive
    retry and its cost.

Run
    python advanced/day1_durable_agents/labs/02_crash_and_resume.py

What to observe
    * The matrix: whatever the crash point, the refunds table holds ONE refund, the resumer executes only the
      tools the log has no result for, and its model calls equal the turns the log does not have yet.
    * "response lost": the model call is repeated - the only duplicate the log cannot prevent (billed twice,
      never a tool run twice).
    * The first request after the resume: prefix identical to the last request before the crash, two messages
      longer, cache_read_input_tokens covering the whole prefix.
    * The naive retry re-runs every turn from scratch: more model calls, and only the tools' own re-reading of
      the world stops a second refund. Lab 03 takes that safety net away.
"""
# test: expect=append-only
# test: expect=refunds=1

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import Crash
from kestrel.support_agent import run_support_agent
from kestrel.support_tools import SupportDesk
from labkit import get_client, header, is_mock, mock_api, step, wrap
from labkit.data import memory_db

import _day1 as d1

TICKET = "T-1206"


class ResponseLostClient:
    """A client whose Nth model call returns to a worker that dies before it can log the response.

    From the log's point of view this is the same as the request never having been made - which is why the
    resumer repeats it.  (A real cause: the process is killed while the SDK is parsing the response.)"""

    def __init__(self, client, die_on_call: int) -> None:
        self._client, self._die_on, self._calls = client, die_on_call, 0
        self.beta = self

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        response = self._client.beta.messages.create(**kwargs)
        self._calls += 1
        if self._calls == self._die_on:
            raise Crash(f"worker died with the turn-{self._die_on} response in memory, before logging it")
        return response


def new_run(store_name: str, ticket: dict, run_id: str):
    store = d1.fresh_store(store_name)
    db = memory_db()
    run = store.create("support", input=d1.run_input(ticket), run_id=run_id, tags={"ticket": ticket["ticket_id"]})
    return store, db, run


def worker(store, client, db, ticket, name, **kw):
    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    return d1.support_runner(store, client, execute=d1.desk_executor(desk), worker=name,
                             runner_cls=d1.FinishingRunner, **kw)


def refunds(db) -> int:
    return db.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]


def step_baseline(client, ticket):
    store, db, run = new_run("02_baseline", ticket, "sd-T-1206")
    before = len(mock_api().request_log)
    outcome = worker(store, client, db, ticket, "worker-a").run(run.id)
    print(f"{d1.outcome_line(outcome)}  model calls={len(mock_api().request_log) - before}  "
          f"events={len(store.events(run.id))}  refunds={refunds(db)}")
    print("Turns: 1 get_customer_profile, 2 get_rma, 3 issue_refund, 4 the reply. Tool calls: 3, model calls: 4.")


def step_matrix(client, ticket):
    rows = []
    cases = [(point, turn) for turn in (1, 2, 3) for point in ("after_model", "before_tool", "after_tool")]
    cases.append(("after_model", 4))
    for point, turn in cases:
        store, db, run = new_run("02_matrix", ticket, "sd-T-1206")
        try:
            worker(store, client, db, ticket, "worker-a", crash_at=(point, turn)).run(run.id)
        except Crash:
            pass
        run_after_crash = store.get(run.id)
        events = store.events(run.id)
        before = len(mock_api().request_log)
        outcome = worker(store, client, db, ticket, "worker-b").run(run.id)     # another process, same store
        rows.append((f"{point:<12} {turn}", len(events), events[-1]["type"], run_after_crash.status,
                     outcome.replayed_tools, outcome.executed_tools, len(mock_api().request_log) - before,
                     refunds(db), outcome.status))
    print(f"{'crash point':<16} {'events':>6} {'last event logged':<19} {'status':<8} {'replayed':>8} {'executed':>8} "
          f"{'model calls':>11} {'refunds':>7}  resumed")
    for r in rows:
        print(f"{r[0]:<16} {r[1]:>6} {r[2]:<19} {r[3]:<8} {r[4]:>8} {r[5]:>8} {r[6]:>11} {'refunds=' + str(r[7]):>9}  {r[8]}")
    print(wrap("Read it row by row: 'replayed' counts tool results the resumer took from the log, 'executed' the tools "
               "it had to run, 'model calls' the turns it had to generate again. Crashing after a tool ran leaves "
               "its result in the log, so nothing runs twice; crashing before it runs leaves a model.response whose "
               "tool_use has no result, so the resumer runs that tool once. The run after the crash is still "
               "'running' in the store: its worker never got to change it, which is what stuck-run detection in "
               "lab 06 looks for."))
    print(wrap("The last row is the crash the shipped runner does not handle: turn 4 was logged with "
               "stop_reason=end_turn but the process died before marking the run completed. Resuming by re-sending "
               "that conversation would end on an assistant turn - a prefill, rejected with a 400 - so "
               "FinishingRunner (in _day1.py) completes the run from the log without a model call: 0 model calls, "
               "the same reply."))


def step_response_lost(client, ticket):
    store, db, run = new_run("02_lost", ticket, "sd-T-1206")
    dying = ResponseLostClient(client, die_on_call=2)
    before = len(mock_api().request_log)
    try:
        worker(store, dying, db, ticket, "worker-a").run(run.id)
    except Crash as exc:
        print(f"worker-a: {exc}")
    print(f"  log after the crash: {[e['type'] for e in store.events(run.id)][-3:]} - the turn-2 response is not "
          "there, only turn 1 and its tool result")
    outcome = worker(store, client, db, ticket, "worker-b").run(run.id)
    total = len(mock_api().request_log) - before
    print(f"  worker-b: {d1.outcome_line(outcome)}  refunds={refunds(db)}")
    print(f"  model calls for the whole run: {total} (4 turns + 1 repeated) - turns logged: "
          f"{[e['turn'] for e in store.events(run.id, types=('model.response',))]}")
    print(wrap("A model call whose response was lost is repeated, and billed twice: the log can make tool "
               "execution at-most-once but it cannot make a model call exactly-once, because the response has to "
               "exist before it can be logged. Keep the window small (log immediately, don't post-process first) "
               "and accept it; the alternative - logging an intent before the call - only tells you that a call "
               "MAY have happened, which changes nothing."))


def step_append_only(client, ticket):
    store, db, run = new_run("02_append", ticket, "sd-T-1206")
    try:
        worker(store, client, db, ticket, "worker-a", crash_at=("after_tool", 2)).run(run.id)
    except Crash:
        pass
    last_before = mock_api().request_log[-1]
    calls_before = len(mock_api().request_log)
    outcome = worker(store, client, db, ticket, "worker-b").run(run.id)
    calls_by_b = len(mock_api().request_log) - calls_before
    first_after = mock_api().request_log[-calls_by_b]                # worker-b's first request (turn 3)
    n = len(last_before["messages"])
    print(f"worker-a's last request: {n} messages; worker-b's first request: {len(first_after['messages'])} messages")
    print(f"  same system prompt: {first_after['system'] == last_before['system']}   "
          f"same tools: {first_after['tools'] == last_before['tools']}")
    print(f"  first {n} messages byte-identical: {first_after['messages'][:n] == last_before['messages']}   "
          f"-> append-only: the resumer added assistant turn 2 and its tool result, nothing else changed")
    responses = store.events(run.id, types=("model.response",))
    u2, u3 = responses[1]["usage"], responses[2]["usage"]
    prefix = u3["cache_read_input_tokens"] + u3["cache_creation_input_tokens"] + u3["input_tokens"]
    print(f"  usage of worker-a's turn 2: cache_read={u2['cache_read_input_tokens']:,} cache_write={u2['cache_creation_input_tokens']:,}")
    print(f"  usage of worker-b's turn 3: cache_read={u3['cache_read_input_tokens']:,} cache_write={u3['cache_creation_input_tokens']:,} "
          f"uncached={u3['input_tokens']:,}  -> {u3['cache_read_input_tokens'] / prefix:.0%} of the prompt read from the cache")
    print(wrap("The resumer's first request read the whole prompt from the cache worker-a's requests had written "
               "(and, in this process, the earlier identical runs) - because the bytes matched. Re-serialising "
               "history differently on resume (dropping is_error: "
               "false, re-ordering keys, trimming a tool result) would invalidate the cache from the first changed "
               "byte and, on models with preserved thinking, invalidate every signed thinking block after it."))
    if is_mock():
        print("[mock] the cache is simulated per process, with the real API's prefix rules; live, the same request "
              "bytes hit the same org-scoped cache within the TTL.")


def step_naive_retry(client, ticket):
    db = memory_db()
    before = len(mock_api().request_log)

    def die_after_refund(name, tool_input, content, is_error):
        if name == "issue_refund":
            raise Crash("deploy restarted the pod right after issue_refund returned")

    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    try:
        run_support_agent(client, d1.ticket_message(ticket), ticket["from_email"], desk=desk,
                          ticket_ref=ticket["ticket_id"], on_tool=die_after_refund)
    except Crash as exc:
        print(f"in-memory loop, attempt 1: {exc}")
    print(f"  model calls so far: {len(mock_api().request_log) - before}, refunds in the database: {refunds(db)}, "
          "messages array: gone with the process")
    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    result = run_support_agent(client, d1.ticket_message(ticket), ticket["from_email"], desk=desk,
                               ticket_ref=ticket["ticket_id"])
    print(f"  attempt 2 (from scratch): turns={result.turns} tools={[c['name'] for c in result.tool_calls]}")
    print("  reply:\n" + wrap(result.reply))
    print(f"  model calls in total: {len(mock_api().request_log) - before} (durable: 4), refunds={refunds(db)}")
    print(wrap("The retry did not double-refund only because get_rma re-read the world and the tool's primary key "
               "would have refused anyway - two safety nets that live in the tools, not in the loop. It also "
               "re-generated every turn (the cache made that cheaper, not free) and told the customer a different "
               "story than attempt 1 was about to. When the effect is a payment gateway call, an email or a "
               "carrier booking, those nets are not there by default: lab 03."))


def main() -> None:
    client = get_client()
    header("Lab 02 - Crash and resume")
    if is_mock():
        print("[mock] crashes are simulated with `crash_at` and a BaseException; the log, the leases and the "
              "resumption logic are the real thing.")
    ticket = d1.ticket(TICKET)

    step(1, "Baseline: the run without a crash")
    step_baseline(client, ticket)

    step(2, "The crash matrix: every crash point, resumed by a second worker")
    step_matrix(client, ticket)

    step(3, "The crash point the log cannot cover: the response was lost")
    step_response_lost(client, ticket)

    step(4, "Append-only messages: the cache and the signatures survive the crash")
    step_append_only(client, ticket)

    step(5, "The naive alternative: re-run the in-memory loop from scratch")
    step_naive_retry(client, ticket)


if __name__ == "__main__":
    main()
