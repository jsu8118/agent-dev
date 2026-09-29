"""Tests for advanced/lib/durable.py - the event-sourced, resumable agent runtime used by the advanced course.

Each test is one guarantee from the module docstring: every step is logged before the next one, a resumed
run replays completed tool results instead of re-executing them, effects are at-most-once across crashes,
leases keep two workers off the same run, approvals pause durably, and the messages array stays append-only
across a resume (so caches and preserved thinking survive).
"""

from __future__ import annotations

import json
import time

import pytest

from advanced.lib.durable import ApprovalRequired, Crash, DurableRunner, RunStore
from labkit import get_client, mock_api
from labkit.mock import say, scenario, tool, use_tools

MODEL = "claude-opus-5"
SYSTEM = "You are the Kestrel billing assistant. Look the account up, then apply the credit the customer asks for."
TOOLS = [
    {"name": "lookup_account", "description": "Balance and tier of a customer account.",
     "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}},
    {"name": "issue_credit", "description": "Post a goodwill credit to the account (a real side effect).",
     "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}, "amount": {"type": "number"},
                                                       "reason": {"type": "string"}},
                      "required": ["customer_id", "amount", "reason"]}},
]


# ------------------------------------------------------------------ a deterministic stand-in model for these tests
@scenario("test.durable_billing", match=lambda r: r.has_tool("lookup_account", "issue_credit"), priority=50)
def billing_policy(req):
    text = req.first_user_text
    amount = float(req.search(r"\$(\d+(?:\.\d+)?)", text).group(1))
    if not req.called("lookup_account"):
        return use_tools(tool("lookup_account", customer_id="C-1005"), preface="Let me check the account.")
    if not req.called("issue_credit"):
        return use_tools(tool("issue_credit", customer_id="C-1005", amount=amount, reason="late delivery"))
    result = req.calls("issue_credit")[-1].result_json()
    if isinstance(result, dict) and "error" in result:
        return say(f"I could not apply the credit: {result['error']}")
    return say(f"Done - credit {result['credit_id']} for ${amount:.2f} has been applied to your account.")


class Ledger:
    """A fake downstream system of record that honours idempotency keys (as any payments API should)."""

    def __init__(self) -> None:
        self.credits: dict[str, dict] = {}         # idempotency key -> credit
        self.calls = 0

    def find(self, key: str) -> dict | None:
        return self.credits.get(key)

    def post(self, key: str, amount: float) -> dict:
        self.calls += 1
        credit = {"credit_id": f"CR-{len(self.credits) + 1:04d}", "amount": amount}
        self.credits[key] = credit
        return credit


def make_executor(ledger: Ledger, *, die_after_post: list[bool] | None = None, approval_over: float | None = None):
    executed: list[str] = []

    def execute(name, tool_input, ctx):
        executed.append(name)
        if name == "lookup_account":
            return {"customer_id": tool_input["customer_id"], "balance_usd": 1240.0, "tier": "key"}
        if name == "issue_credit":
            if approval_over is not None and tool_input["amount"] > approval_over and not tool_input.get("_approved"):
                raise ApprovalRequired({"summary": f"credit of ${tool_input['amount']:.2f} needs a manager"})
            with ctx.effect() as eff:
                if eff.done:
                    return eff.stored
                if eff.in_flight:                       # a previous worker died mid-effect: ask the ledger
                    found = ledger.find(ctx.idempotency_key)
                    if found:
                        return eff.commit(found)
                credit = ledger.post(ctx.idempotency_key, tool_input["amount"])
                if die_after_post and die_after_post.pop(0):
                    raise Crash("worker died right after the ledger accepted the credit")
                return eff.commit(credit)
        return {"error": f"unknown tool {name}"}

    execute.executed = executed
    return execute


def make_runner(store, execute, **kw) -> DurableRunner:
    return DurableRunner(store, get_client(), model=MODEL, system=SYSTEM, tools=TOOLS, execute=execute,
                         max_tokens=1024, **kw)


REQUEST = {"message": "Order SO-10248 arrived nine days late. Please apply the $150 goodwill credit you promised."}


# ------------------------------------------------------------------ the guarantees
def test_every_step_is_logged_in_order():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    outcome = make_runner(store, make_executor(ledger), worker="w1").run(run.id)

    assert outcome.status == "completed" and outcome.turns == 3
    assert "CR-0001" in outcome.reply and ledger.calls == 1
    types = [e["type"] for e in store.events(run.id)]
    assert types == ["run.created", "run.status", "model.response", "tool.started", "tool.result", "model.response",
                     "tool.started", "tool.result", "model.response", "run.status"]
    final = store.get(run.id)
    assert final.status == "completed" and final.result["reply"] == outcome.reply and final.lease_owner is None
    # each model.response carries the full assistant content and the API usage, so the log IS the transcript
    responses = store.events(run.id, types=("model.response",))
    assert [r["turn"] for r in responses] == [1, 2, 3]
    assert all("input_tokens" in r["usage"] for r in responses)
    assert responses[0]["content"][-1]["type"] == "tool_use"


def test_crash_after_model_response_resumes_without_repeating_the_call():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    with pytest.raises(Crash):
        make_runner(store, make_executor(ledger), worker="w1", crash_at=("after_model", 2)).run(run.id)
    assert store.get(run.id).status == "running" and store.get(run.id).lease_owner is None
    requests_before = len(mock_api().request_log)

    execute = make_executor(ledger)
    outcome = make_runner(store, execute, worker="w2").run(run.id)          # a different worker picks it up
    assert outcome.status == "completed" and "CR-0001" in outcome.reply
    assert len(mock_api().request_log) - requests_before == 1              # only turn 3 was generated again
    assert [r["turn"] for r in store.events(run.id, types=("model.response",))] == [1, 2, 3]
    assert execute.executed == ["issue_credit"]                             # the turn-2 tool call, once
    assert outcome.replayed_tools == 1 and outcome.executed_tools == 1     # turn 1's result came from the log


def test_crash_after_tool_replays_the_logged_result():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    with pytest.raises(Crash):
        make_runner(store, make_executor(ledger), worker="w1", crash_at=("after_tool", 2)).run(run.id)
    assert ledger.calls == 1 and store.events(run.id, types=("tool.result",))[-1]["name"] == "issue_credit"

    execute = make_executor(ledger)
    outcome = make_runner(store, execute, worker="w2").run(run.id)
    assert outcome.status == "completed" and "CR-0001" in outcome.reply
    assert execute.executed == [] and outcome.replayed_tools == 2          # nothing re-executed: both results replayed
    assert ledger.calls == 1


def test_effect_is_at_most_once_when_the_worker_dies_mid_effect():
    """The ledger accepted the credit, then the worker died before logging the result.  The next worker finds
    the effect 'in flight', asks the ledger by idempotency key, and reuses the credit instead of posting twice."""
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    with pytest.raises(Crash):
        make_runner(store, make_executor(ledger, die_after_post=[True]), worker="w1").run(run.id)
    assert ledger.calls == 1
    key = f"{run.id}:{store.events(run.id, types=('tool.started',))[-1]['tool_use_id']}"
    assert store.effect_begin(key, run.id, "issue_credit") == {"status": "started", "result": None}

    outcome = make_runner(store, make_executor(ledger), worker="w2").run(run.id)
    assert outcome.status == "completed" and "CR-0001" in outcome.reply
    assert ledger.calls == 1 and len(ledger.credits) == 1                  # still exactly one credit


def test_ordinary_tool_failure_on_first_attempt_releases_the_effect_key():
    store = RunStore()
    run = store.create("billing", input=REQUEST)
    attempts = []

    def execute(name, tool_input, ctx):
        if name == "lookup_account":
            return {"balance_usd": 0}
        with ctx.effect() as eff:
            attempts.append(eff.previous)
            if len(attempts) == 1:
                raise ConnectionError("ledger unavailable")        # a normal failure: the model sees a tool error
            return eff.commit({"credit_id": "CR-9"})

    outcome = make_runner(store, execute, worker="w1").run(run.id)
    assert outcome.status == "completed" and "could not apply" in outcome.reply
    assert attempts == [None]                                            # the failed claim was released ...
    key = f"{run.id}:{store.events(run.id, types=('tool.started',))[-1]['tool_use_id']}"
    assert store.effect_begin(key, run.id, "issue_credit") is None       # ... so a retry starts fresh
    result = store.events(run.id, types=("tool.result",))[-1]
    assert result["is_error"] and "ConnectionError" in result["content"]


def test_leases_keep_two_workers_off_one_run():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    assert store.acquire(run.id, "w1", ttl_s=30)
    with pytest.raises(RuntimeError, match="leased by another worker"):
        make_runner(store, make_executor(ledger), worker="w2").run(run.id)
    assert store.heartbeat(run.id, "w1") and not store.heartbeat(run.id, "w2")
    store.release(run.id, "w1")
    assert make_runner(store, make_executor(ledger), worker="w2").run(run.id).status == "completed"

    # a dead worker's lease expires and is taken over; `stuck()` finds runs abandoned mid-flight
    run2 = store.create("billing", input=REQUEST)
    assert store.acquire(run2.id, "dead", ttl_s=0.05)
    store.set_status(run2.id, "running")
    time.sleep(0.1)
    assert [r.id for r in store.stuck(older_than_s=0)] == [run2.id]
    assert store.acquire(run2.id, "w3", ttl_s=30)


def test_approval_pauses_durably_and_resumes_from_another_process():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input={"message": "Please apply the $4000 credit for the failed KP-250-S."})
    runner = make_runner(store, make_executor(ledger, approval_over=2500), worker="w1")
    outcome = runner.run(run.id)
    assert outcome.status == "waiting_approval" and outcome.approval_id
    assert store.get(run.id).status == "waiting_approval" and ledger.calls == 0
    requests = len(mock_api().request_log)
    assert runner.run(run.id).status == "waiting_approval"                # idle while waiting: no model call
    assert len(mock_api().request_log) == requests

    # a manager's approval UI (any process that can open the store) decides; deciding twice is a no-op
    decided = store.decide(outcome.approval_id, approved=True, by="ops.manager")
    assert decided.status == "pending"
    assert store.decide(outcome.approval_id, approved=True, by="someone.else").status == "pending"
    assert [a["status"] for a in store.approvals(run.id)] == ["approved"]

    resumed = make_runner(store, make_executor(ledger, approval_over=2500), worker="w2").resume_after_decision(run.id)
    assert resumed.status == "completed" and "CR-0001" in resumed.reply and ledger.calls == 1
    types = [e["type"] for e in store.events(run.id)]
    assert "approval.requested" in types and "approval.decided" in types
    last_result = len(types) - 1 - types[::-1].index("tool.result")
    assert types.index("approval.decided") < last_result                  # the decision came before the credit


def test_rejected_approval_is_reported_to_the_model_as_a_tool_error():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input={"message": "Please apply the $4000 credit for the failed KP-250-S."})
    outcome = make_runner(store, make_executor(ledger, approval_over=2500), worker="w1").run(run.id)
    store.decide(outcome.approval_id, approved=False, by="ops.manager", note="over the goodwill limit")
    resumed = make_runner(store, make_executor(ledger, approval_over=2500), worker="w1").resume_after_decision(run.id)
    assert resumed.status == "completed" and "could not apply" in resumed.reply and "over the goodwill limit" in resumed.reply
    assert ledger.calls == 0


def test_messages_stay_append_only_across_a_resume():
    """The resumed worker's first request must extend the crashed worker's last request byte-for-byte: that is
    what keeps prompt-cache prefixes and preserved-thinking bindings valid after a resume."""
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    with pytest.raises(Crash):
        make_runner(store, make_executor(ledger), worker="w1", crash_at=("after_tool", 2)).run(run.id)
    last_before = mock_api().request_log[-1]
    make_runner(store, make_executor(ledger), worker="w2").run(run.id)
    first_after = mock_api().request_log[-1]

    assert first_after["messages"][:len(last_before["messages"])] == last_before["messages"]
    assert len(first_after["messages"]) == len(last_before["messages"]) + 2   # + assistant turn 2, + its results
    assert first_after["system"] == last_before["system"] and first_after["tools"] == last_before["tools"]


def test_store_on_disk_is_shared_between_store_instances(tmp_path):
    path = tmp_path / "durable.db"
    a, b = RunStore(path), RunStore(path)
    run = a.create("billing", input=REQUEST, tags={"customer": "C-1005"})
    assert b.get(run.id).tags == {"customer": "C-1005"}
    a.append(run.id, "user.message", {"content": "hello"})
    assert [e["type"] for e in b.events(run.id)][-1] == "user.message"
    assert a.create("billing", run_id=run.id).id == run.id                   # idempotent creation
    assert [r.id for r in b.list(kind="billing")] == [run.id]
    assert json.loads(json.dumps(b.get(run.id).input)) == REQUEST


# ------------------------------------------------------------------ the guarantees added after Day 1's review
def test_plain_run_answers_a_decided_approval_instead_of_asking_again():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input={"message": "Please apply the $4000 credit for the failed KP-250-S."})
    runner = make_runner(store, make_executor(ledger, approval_over=2500), worker="w1")
    outcome = runner.run(run.id)
    store.decide(outcome.approval_id, approved=True, by="ops.manager")
    resumed = make_runner(store, make_executor(ledger, approval_over=2500), worker="w2").run(run.id)   # not resume_after_decision
    assert resumed.status == "completed" and "CR-0001" in resumed.reply and ledger.calls == 1
    assert [a["status"] for a in store.approvals(run.id)] == ["approved"]                          # no second approval


def test_expired_approval_resumes_with_a_refusal():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input={"message": "Please apply the $4000 credit for the failed KP-250-S."})
    outcome = make_runner(store, make_executor(ledger, approval_over=2500), worker="w1").run(run.id)
    assert store.expire(outcome.approval_id).status == "pending"
    resumed = make_runner(store, make_executor(ledger, approval_over=2500), worker="w1").run(run.id)
    assert resumed.status == "completed" and "could not apply" in resumed.reply and "Not decided in time" in resumed.reply
    assert ledger.calls == 0


def test_crash_after_the_final_response_completes_from_the_log_without_a_model_call():
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    with pytest.raises(Crash):
        make_runner(store, make_executor(ledger), worker="w1", crash_at=("after_model", 3)).run(run.id)
    requests_before = len(mock_api().request_log)
    outcome = make_runner(store, make_executor(ledger), worker="w2").run(run.id)
    assert outcome.status == "completed" and "CR-0001" in outcome.reply
    assert len(mock_api().request_log) == requests_before                     # the answer was already in the log


def test_a_worker_that_lost_its_lease_stops():
    from advanced.lib.durable import LeaseLost
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    calls = []

    def execute(name, tool_input, ctx):
        calls.append(name)
        time.sleep(0.03)                                   # slower than the (tiny) lease: it expires mid-tool
        assert store.acquire(run.id, "usurper", ttl_s=30)  # another worker takes the run over
        return make_executor(ledger)(name, tool_input, ctx)

    with pytest.raises(LeaseLost):
        make_runner(store, execute, worker="w1", lease_ttl_s=0.01).run(run.id)
    assert calls == ["lookup_account"] and ledger.calls == 0                 # it stopped before the credit


def test_store_claims_are_race_safe_and_idempotent():
    store = RunStore()
    run = store.create("billing", input=REQUEST)
    store.create("billing", input={"message": "other"}, run_id=run.id)          # a repeated create changes nothing
    assert [e["type"] for e in store.events(run.id)] == ["run.created"] and store.get(run.id).input == REQUEST
    assert store.effect_begin("k1", run.id, "t") is None
    assert store.effect_begin("k1", run.id, "t") == {"status": "started", "result": None}
    approval = store.request_approval(run.id, {"summary": "x"})
    store.decide(approval, approved=False, by="a")
    store.decide(approval, approved=True, by="b")                              # loses: the first decision stands
    assert store.approvals(run.id)[0]["status"] == "rejected"


def test_a_rejected_model_call_fails_the_run_and_raises():
    import anthropic
    store, ledger = RunStore(), Ledger()
    run = store.create("billing", input=REQUEST)
    runner = make_runner(store, make_executor(ledger), worker="w1",
                         create_kwargs={"extra_body": {"temperature": 0.2}})     # sampling parameters: a 400 on Opus 5
    with pytest.raises(anthropic.BadRequestError):
        runner.run(run.id)
    assert store.get(run.id).status == "failed" and "400" in (store.get(run.id).error or "")
    assert [e["type"] for e in store.events(run.id)][-2:] == ["model.error", "run.status"]
