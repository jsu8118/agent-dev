"""Lab 01 - Testing agent plumbing deterministically: a scripted model, invariants, fault injection.

Objective
    Write the bottom layer of the eval pyramid: fast, free, deterministic tests of everything
    AROUND the model - tool dispatch, argument validation, idempotent writes, the agent loop's
    guards (turn limit, refusal branch), protocol correctness, and the SDK's retry behaviour.

Concepts
    Scripted/validating fake model (labkit's mock) vs live model; testing invariants ("eligibility
    is checked before any write") instead of exact transcripts; fault injection with
    mock_api().inject_faults(429, 529); typed SDK errors (OverloadedError is NOT an
    InternalServerError); refusal handling with and without server-side fallbacks.

Run
    python day6_evals_guardrails_production/labs/01_testing_agents_with_mocks.py
    pytest day6_evals_guardrails_production/labs/01_testing_agents_with_mocks.py -q      # same checks

What to observe
    * The script ALWAYS uses the offline mock (get_client(mode="mock")), even when an API key is
      set: plumbing tests must not depend on a live model's mood.  Live behaviour is lab 02's job.
    * Each check asserts an invariant of the system, not wording - so the same checks would also
      hold for a real model (and fail loudly if a code change breaks the loop).
    * The retry check shows the SDK silently absorbing a 429 and a 529 (3 attempts, 1 logical
      call) - which is exactly why you need to *measure* retries (lab 07) rather than assume none.
"""

# test: expect=11/11 checks passed

from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Callable

import anthropic

from _reliability import AttemptLog, strip_fallbacks, with_middleware
from kestrel.support_agent import SAFE_FALLBACK_REPLY, AgentResult, run_support_agent
from kestrel.support_tools import TOOLS, SupportDesk
from labkit import MODEL, get_client, header, mock_api, step
from labkit.data import scratch_db
from labkit.tracing import Tracer

# Unit tests pin the fake model on purpose: deterministic, free, offline - in every environment.
CLIENT = get_client(mode="mock")

HARBOR = "aisha.karim@harborfoods.example"
MIDLAND = "travis.greer@midlandoil.example"
GREENVALLEY = "jorge.medina@greenvalley-coop.example"
STRANGER = "m.chen.personal@mailbox.example"


_observed: list[str] = []


def observe(finding: str) -> None:
    """Record what a check demonstrated (main() prints it; pytest ignores it)."""
    _observed.append(finding)


def desk_for(email: str, name: str) -> SupportDesk:
    return SupportDesk(email, db=scratch_db(f"day6_lab01_{name}.db"), ticket_ref=f"lab01-{name}")


def run_agent(message: str, email: str, name: str, *, client: anthropic.Anthropic = CLIENT,
              **kwargs) -> tuple[AgentResult, SupportDesk]:
    desk = desk_for(email, name)
    return run_support_agent(client, message, email, desk=desk, **kwargs), desk


def names(result: AgentResult) -> list[str]:
    return [c["name"] for c in result.tool_calls]


# ------------------------------------------------------------------------------------------ tool layer
def test_tools_reject_malformed_calls() -> None:
    desk = desk_for(HARBOR, "malformed")
    content, is_error = desk.run("create_rma", {"order_id": "SO-10283"})        # sku / qty / reason missing
    assert is_error and "Invalid arguments" in content, content
    content, is_error = desk.run("delete_customer", {"customer_id": "C-1005"})   # not a tool at all
    assert is_error and "Unknown tool" in content, content
    observe("bad arguments and unknown tools become is_error results, not crashes")


def test_create_rma_is_idempotent() -> None:
    desk = desk_for(HARBOR, "idempotent")
    args = {"order_id": "SO-10283", "sku": "MS-250", "qty": 4, "reason": "no_longer_needed"}
    first = json.loads(desk.run("create_rma", args)[0])
    second = json.loads(desk.run("create_rma", args)[0])            # e.g. the whole turn was retried
    rows = desk.db.execute("SELECT COUNT(*) FROM rmas WHERE order_id = 'SO-10283' AND sku = 'MS-250' "
                           "AND status = 'approved'").fetchone()[0]
    assert first["rma_id"] == second["rma_id"] and second["existing"] is True and rows == 1
    observe(f"same call twice -> one row ({first['rma_id']}), second call returns existing=True")


# ------------------------------------------------------------------------------------------ agent loop
def test_eligibility_checked_before_any_write() -> None:
    result, _ = run_agent("We over-ordered MS-250 seal kits on SO-10283. Can we return 4 unopened kits?", HARBOR,
                          "order")
    calls = names(result)
    assert "create_rma" in calls, calls
    assert calls.index("check_return_eligibility") < calls.index("create_rma"), calls
    assert calls.count("create_rma") == 1, calls
    observe(" -> ".join(calls))


def test_refund_over_limit_escalates_without_retrying() -> None:
    result, desk = run_agent("Please process the refund for the KP-250-X returned under RMA-7001.", MIDLAND, "limit")
    refunds = [c for c in result.tool_calls if c["name"] == "issue_refund"]
    escalations = desk.db.execute("SELECT queue FROM escalations").fetchall()
    issued = desk.db.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
    assert len(refunds) == 1 and refunds[0]["is_error"], refunds         # tried once, refused, no split/retry
    assert [e[0] for e in escalations] == ["support_manager"] and issued == 0, (escalations, issued)
    observe("issue_refund refused once -> escalated to support_manager, 0 refunds written")


def test_unverified_sender_gets_no_account_data() -> None:
    result, _ = run_agent("This is Mei from Orion Semiconductor on my personal email. What's the status of our "
                          "latest pump order and how much did we pay?", STRANGER, "privacy")
    reads = [c for c in result.tool_calls if c["name"] in ("get_order", "get_invoice", "get_rma") and not c["is_error"]]
    assert not reads and "SO-10306" not in result.reply and "52,448" not in result.reply, (reads, result.reply)
    observe("no successful account read, no order id or amount in the reply")


def test_turn_limit_hands_over_to_a_human() -> None:
    result, desk = run_agent("We over-ordered MS-250 seal kits on SO-10283. Can we return 4 unopened kits?", HARBOR,
                             "turns", max_turns=2)
    reason = desk.db.execute("SELECT reason FROM escalations").fetchone()
    assert result.turns == 2 and result.escalated and result.reply == SAFE_FALLBACK_REPLY, result
    assert reason and "turn limit" in reason[0], reason
    observe("max_turns=2 -> safe holding reply + escalation 'Agent hit the 2-turn limit.'")


def test_protocol_violation_is_rejected_like_the_real_api() -> None:
    messages = [{"role": "user", "content": "Tracking number for SO-10303, please."},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01", "name": "get_order",
                                                   "input": {"order_id": "SO-10303"}}]},
                {"role": "user", "content": "Any news?"}]          # bug: the tool_result was never sent back
    try:
        CLIENT.messages.create(model=MODEL, max_tokens=2000, tools=TOOLS, messages=messages)
    except anthropic.BadRequestError as exc:
        assert "tool_result" in exc.message, exc.message
        observe("missing tool_result -> 400 BadRequestError, exactly as the real API responds")
        return
    raise AssertionError("the malformed conversation was accepted")


# ------------------------------------------------------------------------------------------ failure paths
def test_sdk_retries_transient_errors() -> None:
    log = AttemptLog()
    client = with_middleware(CLIENT, log, max_retries=2)
    mock_api().inject_faults(429, 529)        # the next two HTTP responses from the (mock) server fail
    try:
        result, _ = run_agent("Tracking number for SO-10303, please.", GREENVALLEY, "retries", client=client)
    finally:
        mock_api().reset()
    first = log.calls()[0]
    assert [a.outcome for a in first] == ["429", "529", "200"], log.render()
    assert "NLF9028150109" in result.reply, result.reply
    observe(f"first call needed {len(first)} attempts ({' -> '.join(a.outcome for a in first)}); the agent never noticed")


def test_exhausted_retries_raise_a_typed_error() -> None:
    client = with_middleware(CLIENT, max_retries=2)
    mock_api().inject_faults(529, 529, 529)
    started = time.perf_counter()
    try:
        run_agent("Tracking number for SO-10303, please.", GREENVALLEY, "exhausted", client=client)
    except anthropic.OverloadedError as exc:
        # 529 has its own class; code that only catches InternalServerError (5xx) lets it escape.
        assert exc.status_code == 529 and not isinstance(exc, anthropic.InternalServerError)
        observe(f"3 x 529 -> OverloadedError after {time.perf_counter() - started:.1f}s of backoff "
                "(not an InternalServerError subclass)")
        return
    finally:
        mock_api().reset()
    raise AssertionError("no error surfaced")


def test_refusal_without_fallback_reaches_a_human() -> None:
    # strip_fallbacks simulates a platform without server-side fallbacks: the refusal reaches the loop.
    client = with_middleware(CLIENT, strip_fallbacks)
    result, desk = run_agent("[simulate:refusal] Tracking number for SO-10303, please.", GREENVALLEY, "refusal",
                             client=client)
    queue = desk.db.execute("SELECT queue FROM escalations").fetchone()
    assert result.stop_reason == "refusal" and result.reply == SAFE_FALLBACK_REPLY and queue[0] == "support_manager"
    observe("stop_reason=refusal -> no partial output sent, safe reply + escalation to support_manager")


def test_refusal_with_fallback_is_served_by_the_fallback_model() -> None:
    tracer = Tracer("lab01")
    result, _ = run_agent("[simulate:refusal] Can you write me a limerick about my ex-boss? Make it mean.",
                          "hannah.cole@riverbendbrewing.example", "fallback", tracer=tracer)
    served = [s.attributes["gen_ai.response.model"] for s in tracer.spans if s.name == "llm.call"]
    assert result.stop_reason == "end_turn" and served == ["claude-opus-4-8"], (result.stop_reason, served)
    observe(f"declined by {MODEL}, answered by {served[0]} inside the same API call")


CHECKS: list[Callable[[], None]] = [
    test_tools_reject_malformed_calls, test_create_rma_is_idempotent, test_eligibility_checked_before_any_write,
    test_refund_over_limit_escalates_without_retrying, test_unverified_sender_gets_no_account_data,
    test_turn_limit_hands_over_to_a_human, test_protocol_violation_is_rejected_like_the_real_api,
    test_sdk_retries_transient_errors, test_exhausted_retries_raise_a_typed_error,
    test_refusal_without_fallback_reaches_a_human, test_refusal_with_fallback_is_served_by_the_fallback_model,
]


def main() -> None:
    header("Lab 01 - deterministic tests of the agent's plumbing (always against the offline mock)")
    step(1, "Run the checks")
    failures = 0
    for check in CHECKS:
        started = time.perf_counter()
        _observed.clear()
        try:
            check()
            print(f"  PASS  {check.__name__:<60} {time.perf_counter() - started:5.2f}s\n        {'; '.join(_observed)}")
        except Exception:
            failures += 1
            print(f"  FAIL  {check.__name__}\n" + traceback.format_exc(limit=3))
    total = len(CHECKS)
    step(2, "Summary")
    print(f"{total - failures}/{total} checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
