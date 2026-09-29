"""Lab 03 - Idempotent effects: a goodwill credit posted at most once, whatever crashes.

Objective
    Reproduce Kestrel's double-credit incident with the executor the copilot shipped with, then fix it in
    three steps: claim the effect in the durable effects table (`ctx.effect()`), push the idempotency key to the
    downstream ledger so an in-flight effect can be looked up after a crash, and retry transport failures with
    the same key.  Finish with the two ways the fix still fails - a dedup window shorter than the retry horizon,
    and a downstream system that ignores keys - and what to do about each.

Concepts
    at-least-once delivery, idempotency keys, the effects table (started -> done), in-flight vs done, pushing the
    key to the system of record, transport retries vs business retries, dedup windows, business-key reconciliation,
    "exactly-once" as at-least-once plus idempotent effects.

Run
    python advanced/day1_durable_agents/labs/03_idempotent_effects.py

What to observe
    * Step 1: the naive executor credits Harbor Foods twice after one crash - CR-0001 and CR-0002.
    * Step 2: the effects table alone stops the retry but cannot say whether the money moved - a human must
      reconcile.
    * Step 3: with the key in the ledger, a crash before OR after the ledger call ends with exactly one credit.
    * Step 4: a lost response is retried with the same key and deduplicated; without a key the retry pays twice.
    * Step 5: a key the ledger has forgotten (dedup window) lets a late resume pay again; the business-key
      check catches it.
"""
# test: expect=credits=2
# test: expect=credits=1

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import Crash
from kestrel.support_tools import TOOLS, SupportDesk
from labkit import get_client, header, is_mock, step, wrap
from labkit.data import memory_db

import _day1 as d1

SYSTEM = """\
<adv_day1_credit>
You are the billing assistant of Kestrel Pumps & Controls. Support managers send you goodwill credits they have \
already approved. Look the order up with get_order, then post exactly the credit you were asked for with \
post_credit - once. Confirm the credit reference to the manager in two sentences. Never post a credit twice; if \
post_credit reports an error, say so and stop.
"""
POST_CREDIT = {
    "name": "post_credit",
    "description": "Post a goodwill credit to the customer's account in the accounts-receivable ledger. This is a "
                   "real financial side effect: call it once per approved credit.",
    "strict": True,
    "input_schema": {"type": "object", "additionalProperties": False,
                     "properties": {"order_id": {"type": "string"}, "amount_usd": {"type": "number"},
                                    "reason": {"type": "string"}},
                     "required": ["order_id", "amount_usd", "reason"]},
}
GET_ORDER = next(t for t in TOOLS if t["name"] == "get_order")
CUSTOMER_EMAIL = "aisha.karim@harborfoods.example"
NOTE = ("Manager note re ticket T-1106 (Harbor Foods): the seal kits on SO-10283 were delivered late and I have "
        "approved a $150 goodwill credit. Please apply it to the account and confirm the reference.")
DAY = 24 * 3600.0


# ---------------------------------------------------------------------------- the downstream system
@dataclass
class ARLedger:
    """Kestrel's accounts-receivable ledger, behaving the way a payments API should: it honours an
    Idempotency-Key for `dedup_window_s` seconds. Without a key every call posts a new credit."""

    dedup_window_s: float = 1 * DAY
    now: float = 0.0                          # a simulated clock, so days can pass in a lab
    credits: list[dict] = field(default_factory=list)
    by_key: dict[str, dict] = field(default_factory=dict)
    calls: int = 0
    fail_next: str | None = None              # "lost_response" | "unavailable"

    def post(self, order_id: str, amount_usd: float, reason: str, *, idempotency_key: str | None = None) -> dict:
        self.calls += 1
        if self.fail_next == "unavailable":
            self.fail_next = None
            raise ConnectionError("AR ledger: 503 Service Unavailable")
        if idempotency_key:
            seen = self.by_key.get(idempotency_key)
            if seen and self.now - seen["at"] <= self.dedup_window_s:
                return {**seen, "replayed": True}
        credit = {"credit_id": f"CR-{len(self.credits) + 1:04d}", "order_id": order_id, "amount_usd": amount_usd,
                  "reason": reason, "at": self.now, "key": idempotency_key}
        self.credits.append(credit)
        if idempotency_key:
            self.by_key[idempotency_key] = credit
        if self.fail_next == "lost_response":
            self.fail_next = None
            raise ConnectionError("AR ledger: connection reset while reading the response (the credit WAS posted)")
        return credit

    def find(self, idempotency_key: str) -> dict | None:
        seen = self.by_key.get(idempotency_key)
        return seen if seen and self.now - seen["at"] <= self.dedup_window_s else None

    def find_by_business_key(self, order_id: str, amount_usd: float, within_s: float = 30 * DAY) -> dict | None:
        for credit in self.credits:
            if credit["order_id"] == order_id and credit["amount_usd"] == amount_usd and self.now - credit["at"] <= within_s:
                return credit
        return None

    def summary(self) -> str:
        return f"credits={len(self.credits)} ({', '.join(c['credit_id'] for c in self.credits) or '-'}), ledger calls={self.calls}"


# ---------------------------------------------------------------------------- executors, from naive to robust
def make_executor(desk: SupportDesk, ledger: ARLedger, *, mode: str, die: list[str] | None = None,
                  transport_retries: int = 0, business_key_check: bool = False):
    """mode: "naive" (no effects table, no key), "effect" (effects table, no key downstream),
    "keyed" (effects table + the key pushed to the ledger). `die` lists one-shot crash points:
    "before_post" or "after_post"."""
    die = die or []
    reads = d1.desk_executor(desk)

    def crash_if(point: str) -> None:
        if die and die[0] == point:
            die.pop(0)
            raise Crash(f"worker killed {point.replace('_', ' ')}")

    def post(order_id: str, amount: float, reason: str, key: str | None) -> dict:
        attempts = 0
        while True:
            attempts += 1
            try:
                return ledger.post(order_id, amount, reason, idempotency_key=key)
            except ConnectionError as exc:
                if attempts > transport_retries:
                    raise
                print(f"      transport error ({exc}); retrying with {'the same key' if key else 'NO key'}")

    def execute(name: str, tool_input: dict, ctx) -> dict:
        if name != "post_credit":
            return reads(name, tool_input, ctx)
        order_id, amount, reason = tool_input["order_id"], float(tool_input["amount_usd"]), tool_input["reason"]
        if mode == "naive":
            crash_if("before_post")
            credit = post(order_id, amount, reason, None)
            crash_if("after_post")
            return credit
        with ctx.effect() as eff:                             # key = f"{run_id}:{tool_use_id}"
            if eff.done:
                print("      effects table: done -> replaying the stored result")
                return eff.stored
            if eff.in_flight:
                print("      effects table: in flight -> a previous attempt started and never finished")
                if mode == "effect":
                    return {"error": "An earlier attempt to post this credit was interrupted and its outcome is "
                                     "unknown. Do not retry; ask finance to reconcile the ledger first."}
                found = ledger.find(ctx.idempotency_key)
                if found:
                    print(f"      ledger.find({ctx.idempotency_key[-12:]!r}) -> {found['credit_id']}: reuse it")
                    return eff.commit(found)
                if business_key_check:
                    found = ledger.find_by_business_key(order_id, amount)
                    if found:
                        print(f"      key unknown downstream (window expired) but order + amount match {found['credit_id']}: reuse it")
                        return eff.commit(found)
                print("      ledger has no record of the key -> the effect never reached it: safe to post")
            crash_if("before_post")
            credit = post(order_id, amount, reason, ctx.idempotency_key if mode == "keyed" else None)
            crash_if("after_post")
            return eff.commit(credit)

    return execute


def run_with(client, label: str, *, mode: str, die: list[str] | None = None, transport_retries: int = 0,
             fail_next: str | None = None, business_key_check: bool = False, ledger: ARLedger | None = None,
             advance_days: float = 0.0, dedup_window_s: float = 1 * DAY) -> ARLedger:
    """One scenario: a fresh run (and ledger unless given), worker-a runs it, crashes if told to, worker-b resumes."""
    ledger = ledger or ARLedger(dedup_window_s=dedup_window_s)
    ledger.fail_next = fail_next
    store = d1.fresh_store("03_effects")
    db = memory_db()
    run = store.create("credit", input={"message": NOTE, "requester_email": CUSTOMER_EMAIL}, run_id="credit-T-1106")
    print(f"  {label}")

    def worker(name):
        desk = SupportDesk(CUSTOMER_EMAIL, db=db, ticket_ref="T-1106")
        execute = make_executor(desk, ledger, mode=mode, die=die, transport_retries=transport_retries,
                                business_key_check=business_key_check)
        return d1.support_runner(store, client, execute=execute, worker=name, system=SYSTEM, tools=[GET_ORDER, POST_CREDIT])

    try:
        outcome = worker("worker-a").run(run.id)
    except Crash as exc:
        print(f"    worker-a: CRASH - {exc}")
        if advance_days:
            ledger.now += advance_days * DAY
            print(f"    ... {advance_days:g} days pass before the run is resumed (approval backlog, a redeploy queue)")
        outcome = worker("worker-b").run(run.id)
        print(f"    worker-b: {d1.outcome_line(outcome)}")
    else:
        print(f"    worker-a: {d1.outcome_line(outcome)}")
    print(f"    reply: {d1.short(outcome.reply, 110)}")
    print(f"    ledger: {ledger.summary()}")
    return ledger


def main() -> None:
    client = get_client()
    header("Lab 03 - Idempotent effects")
    if is_mock():
        print("[mock] the model is a stand-in that looks the order up and posts the credit it was told to; the "
              "ledger, the effects table and the crashes are real code.")
    print(wrap(NOTE))

    step(1, "Kestrel before the incident: no effects table, no idempotency key")
    run_with(client, "crash after the ledger accepted the credit, before the tool result was logged:",
             mode="naive", die=["after_post"])
    print(wrap("This is the double credit from the case study. The log says the tool never finished, so the "
               "resumer runs it again - correctly, from the log's point of view. The log cannot know what the "
               "ledger did."))

    step(2, "The effects table alone: the retry is stopped, the question is not answered")
    run_with(client, "same crash, with ctx.effect() but no key sent downstream:", mode="effect", die=["after_post"])
    print(wrap("Better: no second credit. Worse: the ledger holds one credit nobody confirmed, the manager is told "
               "to ask finance, and a person now reconciles by hand. The effects table knows the effect STARTED; "
               "only the system that received it knows whether it happened."))

    step(3, "Push the key to the system of record: in-flight becomes answerable")
    run_with(client, "crash AFTER the ledger call (key posted with the credit):", mode="keyed", die=["after_post"])
    run_with(client, "crash BEFORE the ledger call (key claimed locally, never sent):", mode="keyed", die=["before_post"])
    print(wrap("Same code path for both: the resumer sees 'in flight', asks the ledger for the key, and either "
               "reuses the credit it finds or posts it once. The key is run_id:tool_use_id - unique per tool call, "
               "stable across resumes because the tool_use id comes from the logged model response."))

    step(4, "Transport failures: retry with the same key, never without one")
    run_with(client, "the ledger posted the credit but the response was lost; 1 retry, keyed:", mode="keyed",
             fail_next="lost_response", transport_retries=1)
    run_with(client, "the same lost response, naive executor with 1 retry:", mode="naive",
             fail_next="lost_response", transport_retries=1)
    run_with(client, "the ledger was unavailable (503) on the first call; 1 retry, keyed:", mode="keyed",
             fail_next="unavailable", transport_retries=1)
    print(wrap("Retries belong at the transport level with the SAME key, so the receiver can deduplicate; a retry "
               "that mints a new request is a second order. Note the ordinary failure (503) on a first attempt "
               "releases the effect claim (durable.py's _Effect.__exit__), so a business-level retry by the model "
               "would start clean - and still carry a new key, because it would be a new tool_use."))

    step(5, "Dedup windows: keys must outlive the longest retry horizon")
    run_with(client, "crash after post; the run is resumed 3 days later; the ledger forgets keys after 1 day:",
             mode="keyed", die=["after_post"], advance_days=3)
    run_with(client, "the same, with a business-key check (order + amount within 30 days) as the fallback:",
             mode="keyed", die=["after_post"], advance_days=3, business_key_check=True)
    print(wrap("A run parked on an approval for days, a queue drained after an outage, a replay from a backup: "
               "all resume long after the crash. The effects table is durable and keeps 'started' forever; the "
               "downstream window is the weak link. Prefer receivers that keep keys for as long as the business "
               "record exists, and keep a business-key check for the ones that don't."))

    step(6, "Summary")
    print("  approach                                  crash after post  lost response + retry  late resume (3 days)")
    print("  naive (no table, no key)                  2 credits         2 credits              2 credits")
    print("  effects table, no key downstream          1 + reconcile     n/a                    1 + reconcile")
    print("  effects table + key downstream            1                 1                      2 (window expired)")
    print("  ... + business-key fallback               1                 1                      1")
    print(wrap("'Exactly-once' is not a property a network can give you; at-least-once delivery plus an idempotent "
               "receiver is what every payment system, message broker and workflow engine actually implements. "
               "Your part: claim locally, key the call, look the key up when in doubt, and know the window."))


if __name__ == "__main__":
    main()
