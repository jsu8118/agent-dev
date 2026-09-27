"""Lab 07 - Reliability drills: retries, backoff, timeouts, deadlines, refusals, idempotency, circuit breakers.

Objective
    Rehearse the failures production WILL see, with deterministic drills that run the same against the live
    API and the mock: rate limits and overloads, exhausted retries, timeouts, a missed reply deadline, a
    classifier refusal (with and without server-side fallbacks), a retried request that re-runs a write
    tool, and a backend outage - and make the agent degrade to humans gracefully in every case.

Concepts
    The SDK's retry policy (408/409/429/5xx, exponential backoff with jitter, `retry-after(-ms)`,
    `x-should-retry`); OverloadedError (529) vs InternalServerError; application-level retry with full
    jitter and the retry-amplification trap; per-call timeouts vs an end-to-end deadline; stop_reason
    "refusal" + stop_details and server-side fallbacks (`fallbacks="default"`); idempotent writes under
    at-least-once processing; circuit breakers; budgets and caps (max_turns, max_tokens, task budgets).

Run
    python day6_evals_guardrails_production/labs/07_reliability.py

What to observe
    * The SDK hides retries from you: one logical call, three HTTP attempts. Measure them (AttemptLog).
    * `x-should-retry: false` wins over the status code; 529 raises OverloadedError, which is NOT a
      subclass of InternalServerError - catch both (or APIStatusError) in your error handling.
    * A deadline can fire after a write happened: the escalation says what was already done.
    * With the idempotent create_rma a redelivered ticket yields ONE RMA; the naive version yields two.
    * The circuit breaker stops spending tokens on a dead dependency and sends customers an honest reply.
    Faults are injected client-side (ChaosMiddleware), so the drills behave the same live and in mock mode;
    only the refusal drill needs the mock ("[simulate:refusal]") - live models decline benign requests rarely.
"""

# test: expect=OverloadedError
# test: expect=circuit breaker

from __future__ import annotations

import json
import random
import time

import anthropic

from _reliability import (AttemptLog, ChaosMiddleware, CircuitBreaker, DeadlineExceeded, DeadlineMiddleware,
                          LatencyMiddleware, RetryRecord, retry_with_backoff, with_middleware)
from kestrel.support_agent import SAFE_FALLBACK_REPLY, run_support_agent
from kestrel.support_tools import SupportDesk
from labkit import MODEL, fallback_kwargs, get_client, header, is_mock, show_message, step, text_of
from labkit.data import scratch_db

DRILL_SYSTEM = "You are Kestrel's reliability drill assistant (drill KREL-DRILL-1). Answer in one short sentence."
HARBOR = "aisha.karim@harborfoods.example"
RETURN_4_KITS = "We over-ordered MS-250 seal kits on SO-10283. Can we return 4 unopened kits?"


def ask(client: anthropic.Anthropic, text: str, **kwargs) -> anthropic.types.Message:
    return client.messages.create(model=MODEL, max_tokens=2000, system=DRILL_SYSTEM,
                                  messages=[{"role": "user", "content": text}], **kwargs)


def drill_sdk_retries(client) -> None:
    log, chaos = AttemptLog(), ChaosMiddleware(["429", "529"])
    llm = with_middleware(client, log, chaos, max_retries=2)
    reply = ask(llm, "Is the order portal up?")
    print("One messages.create() call, faults injected on the first two attempts:")
    print(log.render())
    print(f"  -> succeeded: \"{text_of(reply)[:70]}\"")
    print("  The 429 carried retry-after-ms=40, so the SDK waited ~40 ms; the 529 had no hint, so it used "
          "exponential backoff with jitter (0.5 s x 2^n, x0.75-1.0).")

    log2, chaos2 = AttemptLog(), ChaosMiddleware(["529-no-retry"])
    try:
        ask(with_middleware(client, log2, chaos2, max_retries=2), "Is the order portal up?")
    except anthropic.OverloadedError:
        print(f"\n`x-should-retry: false` on a 529: {len(log2.attempts)} attempt, no retry - the server's hint wins.")

    log3, chaos3 = AttemptLog(), ChaosMiddleware(["529", "529", "529"])
    try:
        ask(with_middleware(client, log3, chaos3, max_retries=2), "Is the order portal up?")
    except anthropic.APIStatusError as exc:
        print(f"\nThree 529s with max_retries=2 -> {type(exc).__name__} after {len(log3.attempts)} attempts "
              f"(status {exc.status_code}, type {exc.type!r}).")
        print(f"  isinstance(exc, anthropic.InternalServerError) = {isinstance(exc, anthropic.InternalServerError)}"
              " -> an `except InternalServerError` handler would NOT catch an overload.")


def drill_app_retry(client) -> None:
    chaos = ChaosMiddleware(["529", "529", "500"])
    llm = with_middleware(client, chaos, max_retries=0)          # exactly ONE layer retries: this one
    records: list[RetryRecord] = []
    started = time.perf_counter()
    reply = retry_with_backoff(lambda: ask(llm, "Is the order portal up?"), attempts=5, base_s=0.1, cap_s=2.0,
                               rng=random.Random(7), log=records)
    for r in records:
        print(f"  attempt {r.attempt}: {r.error:<20} -> sleep {r.sleep_s:.3f}s (uniform in [0, min(2.0, 0.1 x 2^"
              f"{r.attempt - 1})])")
    print(f"  attempt {len(records) + 1}: ok after {time.perf_counter() - started:.2f}s -> \"{text_of(reply)[:50]}\"")
    print("  Retry amplification: SDK (3 attempts) x app wrapper (5) x a queue redelivery (3) = 45 requests per "
          "logical call during an outage. Keep one retrying layer; make the others fail fast.")


def drill_timeouts_and_deadline(client) -> None:
    worst = (anthropic.DEFAULT_MAX_RETRIES + 1) * anthropic.DEFAULT_TIMEOUT.read
    print(f"SDK defaults: timeout {anthropic.DEFAULT_TIMEOUT.read:.0f}s read, max_retries "
          f"{anthropic.DEFAULT_MAX_RETRIES} -> one call can take ~{worst / 60:.0f} min before failing. "
          "Kestrel promises replies in 30 s.")
    log, chaos = AttemptLog(), ChaosMiddleware(["timeout"])
    tuned = with_middleware(client, log, chaos, timeout=anthropic.Timeout(20.0, connect=5.0), max_retries=1)
    ask(tuned, "Is the order portal up?")
    print(f"Tuned client (20 s read timeout, 1 retry): {log.render().strip()}  <- timeouts are retried too")

    desk = SupportDesk(HARBOR, db=scratch_db("day6_lab07_deadline.db"), ticket_ref="DEADLINE")
    deadline = DeadlineMiddleware(1.05)
    slow = with_middleware(client, deadline, LatencyMiddleware(0.3))        # each model call now takes 0.3 s
    started = time.perf_counter()
    try:
        reply, disposition = run_support_agent(slow, RETURN_4_KITS, HARBOR, desk=desk, ticket_ref="DEADLINE").reply, \
            "answered"
    except DeadlineExceeded as exc:
        done = [c["name"] for c in desk.calls if not c["is_error"]]
        desk.escalate_to_human("support_manager", "P3", f"Reply deadline missed ({exc}); already done: {done}")
        reply, disposition = SAFE_FALLBACK_REPLY, "degraded to a human"
    print(f"\nAgent with a 1.05 s end-to-end deadline and 0.3 s per model call: {disposition} after "
          f"{time.perf_counter() - started:.2f}s")
    print(f"  tool calls completed before the deadline: {[c['name'] for c in desk.calls]}")
    print(f"  customer gets: \"{reply[:80]}...\"")
    print("  The escalation records what was ALREADY done - a deadline can fire after a write, so writes must be "
          "idempotent and hand-offs explicit.")


def drill_refusals(client) -> None:
    prompt = "[simulate:refusal] Summarise the lockout/tagout steps for a KC-1 cabinet."
    plain = ask(client, prompt)
    print("Without fallbacks:")
    if plain.stop_reason == "refusal":
        details = plain.stop_details
        print(f"  stop_reason=refusal  category={getattr(details, 'category', None)!r}  content blocks="
              f"{len(plain.content)} -> branch on stop_reason BEFORE reading content; content[0] would crash.")
    else:
        print(f"  no refusal this time (stop_reason={plain.stop_reason}); live models decline benign requests rarely, "
              "the mock simulates one on request.")
    rescued = client.beta.messages.create(model=MODEL, max_tokens=2000, system=DRILL_SYSTEM,
                                          messages=[{"role": "user", "content": prompt}], **fallback_kwargs(MODEL))
    print(f"\nWith {fallback_kwargs(MODEL)}:")
    show_message(rescued)
    iterations = rescued.usage.iterations or []
    for it in iterations:
        print(f"  usage.iterations: {it.type:<17} model={getattr(it, 'model', None)} in={it.input_tokens} "
              f"cache_w={it.cache_creation_input_tokens} out={it.output_tokens}")
    served = any(it.type == "fallback_message" for it in iterations) and rescued.stop_reason != "refusal"
    print(f"  served by the fallback: {served} (model={rescued.model}). Fallbacks trigger on policy declines only - "
          "a 429/529 is returned as-is and handled by your retries. Batches reject `fallbacks`.")


class NaiveDesk(SupportDesk):
    """create_rma as it looked before it was hardened: no lookup of an existing open RMA (and no re-check)."""

    def create_rma(self, order_id: str, sku: str, qty: int, reason: str, notes: str = "") -> dict:
        order = self._order_row(order_id)
        self._verify(order["customer_id"])
        line = self._line(order["order_id"], sku)
        next_id = self.db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(rma_id, 5) AS INTEGER)), 7000) + 1 FROM rmas"
                                  ).fetchone()[0]
        rma_id = f"RMA-{next_id}"
        self.db.execute("INSERT INTO rmas VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rma_id, order["order_id"], line["sku"], qty, reason, "approved", "2026-09-15", None, None, notes))
        self.db.commit()
        return {"rma_id": rma_id, "status": "approved", "existing": False}


def after_create_rma(request) -> bool:
    """True for the model call that carries create_rma's result - the moment a crash hurts most."""
    messages = (request.json or {}).get("messages") or []
    return len(messages) >= 2 and "create_rma" in json.dumps(messages[-2], default=str)


def drill_idempotency(client) -> None:
    for desk_cls in (SupportDesk, NaiveDesk):
        desk = desk_cls(HARBOR, db=scratch_db(f"day6_lab07_idem_{desk_cls.__name__}.db"), ticket_ref="REDELIVERY")
        crashing = with_middleware(client, ChaosMiddleware(["529"] * 3, when=after_create_rma), max_retries=2)
        replies = []
        for attempt, llm in ((1, crashing), (2, client)):          # a queue redelivers the ticket after the crash
            try:
                replies.append(run_support_agent(llm, RETURN_4_KITS, HARBOR, desk=desk, ticket_ref="REDELIVERY").reply)
            except anthropic.OverloadedError:
                replies.append(None)
        rmas = [r[0] for r in desk.db.execute("SELECT rma_id FROM rmas WHERE order_id = 'SO-10283' AND sku = 'MS-250' "
                                               "AND requested_at = '2026-09-15'")]
        print(f"  {desk_cls.__name__:<12} attempt 1 crashed after create_rma ran; attempt 2 replied: "
              f"\"{(replies[1] or '')[:48]}...\"")
        print(f"  {'':<12} RMAs now open for SO-10283 / MS-250: {rmas}  "
              f"{'<- one authorization, safe to redeliver' if len(rmas) == 1 else '<- DUPLICATE authorizations'}")


class ERP:
    """A system of record with an outage window, driven by a simulated clock (tickets every 30 s)."""

    def __init__(self, down_until: float) -> None:
        self.now = 0.0
        self.down_until = down_until


class FlakyDesk(SupportDesk):
    ERP_TOOLS = {"get_order", "get_invoice", "get_rma", "list_customer_orders", "check_return_eligibility",
                 "check_warranty", "create_rma", "issue_refund"}

    def __init__(self, *args, erp: ERP, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.erp = erp
        self.dependency_errors = 0

    def run(self, name: str, tool_input: dict) -> tuple[str, bool]:
        if name in self.ERP_TOOLS and self.erp.now < self.erp.down_until:
            self.dependency_errors += 1
            self.calls.append({"name": name, "input": tool_input, "is_error": True})
            return json.dumps({"error": "The order system did not respond (dependency timeout). Do not retry; tell "
                                        "the customer a specialist will follow up."}), True
        return super().run(name, tool_input)


def drill_circuit_breaker(client) -> None:
    erp = ERP(down_until=75.0)
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=60.0, clock=lambda: erp.now)
    tickets = [("T-1001", "jorge.medina@greenvalley-coop.example", "Tracking number for SO-10303, please."),
               ("T-1003", "fiona.macleod@coastalshipyards.example", "Could you confirm the ETA for SO-10300?"),
               ("T-1004", "ingrid.solberg@polarcold.example", "What is the status of our KP-400 order SO-10285?"),
               ("T-1103", "jorge.medina@greenvalley-coop.example", "Where are the valves for SO-10303?"),
               ("T-1107", "fiona.macleod@coastalshipyards.example", "Still no sign of SO-10300. Is it stuck?"),
               ("T-1002", "mei.chen@orion-semi.example", "Checking on SO-10306 - has it shipped?")]
    holding = "We received your message; our order system is briefly unavailable and a specialist will reply shortly."
    raw_reply = ""
    print(f"  {'t':>4}  {'ticket':<8}{'breaker':<11}{'action':<15}{'ERP errors':<12}{'breaker after':<15}tokens")
    for i, (tid, email, text) in enumerate(tickets):
        erp.now = 30.0 * i
        before = breaker.state
        if not breaker.allow():
            print(f"  {erp.now:>4.0f}  {tid:<8}{before:<11}{'skip agent':<15}{'-':<12}{breaker.state:<15}0   -> holding "
                  "reply + human queue")
            continue
        desk = FlakyDesk(email, db=scratch_db(f"day6_lab07_breaker_{i}.db"), ticket_ref=tid, erp=erp)
        result = run_support_agent(client, text, email, desk=desk, ticket_ref=tid)
        if desk.dependency_errors:
            breaker.record_failure()
            desk.escalate_to_human("support_manager", "P3", "Order system unavailable; customer got a holding reply.")
        else:
            breaker.record_success()
        tokens = result.input_tokens + result.output_tokens
        if desk.dependency_errors and i == 0:
            raw_reply = result.reply
        action = "run agent" if before == "closed" else "probe"
        print(f"  {erp.now:>4.0f}  {tid:<8}{before:<11}{action:<15}{desk.dependency_errors:<12}{breaker.state:<15}"
              f"{tokens:,}" + ("   -> holding reply + human queue" if desk.dependency_errors else ""))
    print(f"  circuit breaker transitions: {' , '.join(breaker.transitions)}")
    print(f"  without the orchestrator's check, T-1001's customer would have been sent: \"{raw_reply[:90]}\"")
    print(f"  (ERP outage from t=0 to t={erp.down_until:.0f}s; while open, tickets skip the agent - no tokens burnt on "
          f"a dead dependency, and customers get: \"{holding[:60]}...\")")


def main() -> None:
    client = get_client()
    header("Lab 07 - reliability drills")
    if is_mock():
        print("[mock] Faults are injected client-side, so every drill except the refusal one behaves identically live.")

    step(1, "SDK retries: 429 and 529, x-should-retry, exhausted retries")
    drill_sdk_retries(client)

    step(2, "Application-level retry with full jitter (SDK retries disabled)")
    drill_app_retry(client)

    step(3, "Timeouts and an end-to-end deadline")
    drill_timeouts_and_deadline(client)

    step(4, "Refusals: stop_reason='refusal', then server-side fallbacks")
    drill_refusals(client)

    step(5, "Idempotent writes under a retried request (at-least-once delivery)")
    drill_idempotency(client)

    step(6, "Graceful degradation: a circuit breaker around the order system")
    drill_circuit_breaker(client)

    step(7, "Budgets and caps in this agent")
    print("  per call   max_tokens=8000 (caps thinking + text on Opus 5); timeout; max_retries")
    print("  per ticket max_turns=12 -> human hand-off (lab 01); end-to-end deadline (step 3); tool-call and write "
          "budgets (lab 04)")
    print("  per task   task budgets (beta): the model SEES a token budget for the whole loop and paces itself -")
    print("             client.beta.messages.create(..., betas=['task-budgets-2026-03-13'],")
    print("                 output_config={'effort': 'medium', 'task_budget': {'type': 'tokens', 'total': 40_000}})")
    print("             advisory, minimum 20,000 tokens; max_tokens stays the hard per-response cap.")
    print("  fleet      workspace spend limits and rate limits; alert on cost per ticket (lab 05)")


if __name__ == "__main__":
    main()
