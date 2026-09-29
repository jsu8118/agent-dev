"""Lab 04 - Sagas and compensation: a replacement that undoes itself when a step fails.

Objective
    Build the wrong-item replacement flow as a saga on the durable log: open the RMA, reserve replacement stock,
    book the carrier collection, send the confirmation - four effects in four systems, each at-most-once, with
    a compensation for each.  Watch the happy path, a carrier failure that rolls the earlier steps back, a crash
    in the middle that resumes without repeating a step, a crash inside a step that recovers from the carrier's
    own record, and the "just retry" version that double-reserves stock.

Concepts
    sagas (forward steps + compensating steps), semantic rollback vs transactional rollback, compensations as
    effects, recovery of an in-flight step from its system of record, the agent as planner vs the saga as
    executor, two-phase commit and why nobody offers it across an ERP, a carrier and a mail gateway,
    "just retry" and its precondition (idempotent steps).

Run
    python advanced/day1_durable_agents/labs/04_sagas_and_compensation.py

What to observe
    * The saga.step events in the log, one per effect, with status executed / replayed / recovered.
    * On the carrier failure: saga.compensated events in reverse order, the reservation released, the RMA
      cancelled, and the agent escalating instead of promising a collection.
    * After the mid-saga crash: two steps replayed, two executed, reserved stock up by 2 - once.
    * The naive version after the same crash: reserved stock up by 4.
"""
# test: expect=saga.compensated
# test: expect=replayed

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import Crash, ToolContext
from kestrel.support_tools import TOOLS, SupportDesk
from labkit import get_client, header, is_mock, step, wrap
from labkit.data import memory_db

import _day1 as d1

TICKET = "T-1205"          # Ironclad Steel: ordered IMP-250-D impellers on SO-10292, received IMP-250-A
SYSTEM = """\
<adv_day1_saga>
You are the customer-support assistant of Kestrel Pumps & Controls, handling wrong-item deliveries. Today is \
2026-09-15. Look the order up, confirm the return is eligible as wrong_item with check_return_eligibility, then \
call arrange_replacement once: it opens the RMA, reserves replacement stock, books the carrier collection and \
sends the confirmation - and it undoes its own steps if one of them fails. If it reports an error, escalate to \
the order desk (queue order_desk, priority P2) and tell the customer a person will confirm. Reply in 3-6 plain \
sentences with the references.
"""
ARRANGE = {
    "name": "arrange_replacement",
    "description": "Arrange the replacement of a wrong item in one go: RMA, replacement stock reservation, carrier "
                   "collection of the wrong item, confirmation email. Undoes its own steps if a later one fails. "
                   "Call it once per order line.",
    "strict": True,
    "input_schema": {"type": "object", "additionalProperties": False,
                     "properties": {"order_id": {"type": "string"}, "sku": {"type": "string", "description": "the SKU ordered"},
                                    "qty": {"type": "integer"}, "received_sku": {"type": "string"}},
                     "required": ["order_id", "sku", "qty", "received_sku"]},
}
TOOL_DEFS = [t for t in TOOLS if t["name"] in ("get_order", "check_return_eligibility", "escalate_to_human")] + [ARRANGE]


# ---------------------------------------------------------------------------- the four systems
class Warehouse:
    """The warehouse system (the inventory table of the ops DB). It honours a caller reference: the same
    reference reserves once."""

    def __init__(self, db) -> None:
        self.db, self.reservations = db, {}

    def reserve(self, sku: str, qty: int, ref: str) -> dict:
        if ref in self.reservations:
            return self.reservations[ref]
        row = self.db.execute("SELECT warehouse FROM inventory WHERE sku = ? AND on_hand - reserved >= ? "
                              "ORDER BY on_hand - reserved DESC LIMIT 1", (sku, qty)).fetchone()
        if row is None:
            raise RuntimeError(f"no free stock of {sku}")
        self.db.execute("UPDATE inventory SET reserved = reserved + ? WHERE sku = ? AND warehouse = ?", (qty, sku, row["warehouse"]))
        self.db.commit()
        reservation = {"reservation_id": f"RSV-{len(self.reservations) + 1:04d}", "sku": sku, "qty": qty,
                       "warehouse": row["warehouse"]}
        self.reservations[ref] = reservation
        return reservation

    def release(self, reservation: dict) -> dict:
        if not reservation.get("released"):
            self.db.execute("UPDATE inventory SET reserved = reserved - ? WHERE sku = ? AND warehouse = ?",
                            (reservation["qty"], reservation["sku"], reservation["warehouse"]))
            self.db.commit()
            reservation["released"] = True
        return reservation

    def reserved(self, sku: str) -> int:
        return self.db.execute("SELECT SUM(reserved) FROM inventory WHERE sku = ?", (sku,)).fetchone()[0]


class CarrierAPI:
    """SwiftParcel's booking API. Bookings carry the caller's reference, so a lost response can be looked up."""

    def __init__(self) -> None:
        self.pickups: dict[str, dict] = {}
        self.fail_next: str | None = None

    def book_pickup(self, ref: str, order_id: str) -> dict:
        if ref in self.pickups:
            return self.pickups[ref]
        if self.fail_next:
            reason, self.fail_next = self.fail_next, None
            raise RuntimeError(reason)
        pickup = {"pickup_id": f"PU-{len(self.pickups) + 1:04d}", "carrier": "SwiftParcel",
                  "window": "2026-09-17 08:00-12:00", "order_id": order_id}
        self.pickups[ref] = pickup
        return pickup

    def find(self, ref: str) -> dict | None:
        return self.pickups.get(ref)

    def cancel(self, pickup: dict) -> dict:
        pickup["cancelled"] = True
        return pickup


class MailGateway:
    def __init__(self) -> None:
        self.sent: dict[str, dict] = {}

    def send(self, ref: str, to: str, subject: str) -> dict:
        if ref not in self.sent:
            self.sent[ref] = {"message_id": f"MSG-{len(self.sent) + 1:04d}", "to": to, "subject": subject}
        return self.sent[ref]


def withdraw_rma(db, rma_id: str) -> dict:
    """The compensation for create_rma: the RMA is not deleted (the audit trail must keep it), it is withdrawn -
    status 'rejected' in Atlas ERP's vocabulary - with a note saying why."""
    db.execute("UPDATE rmas SET status = 'rejected', notes = COALESCE(notes, '') || ' [withdrawn: replacement saga "
               "rolled back]' WHERE rma_id = ?", (rma_id,))
    db.commit()
    return {"rma_id": rma_id, "status": "rejected"}


def rma_status(db, rma_id: str) -> str:
    row = db.execute("SELECT status FROM rmas WHERE rma_id = ?", (rma_id,)).fetchone()
    return row[0] if row else "?"


# ---------------------------------------------------------------------------- the saga
class SagaFailed(Exception):
    def __init__(self, step: str, reason: str, compensated: list[str]) -> None:
        super().__init__(f"{step} failed: {reason}")
        self.step, self.reason, self.compensated = step, reason, compensated


@dataclass
class Step:
    name: str
    action: Callable[[dict], dict]                       # state -> result; raises on failure
    compensate: Callable[[dict], Any] | None = None      # undo, given the state (idempotent by construction)
    recover: Callable[[dict], dict | None] | None = None  # after a crash mid-step: ask the system of record


class Saga:
    """Forward steps with compensations, each an at-most-once effect on the durable log.

    Forward: a step already 'done' in the effects table is replayed; a step left 'started' by a dead worker is
    recovered from its own system of record (or the saga fails closed); anything else is executed. Backward: when
    a step raises, the completed steps are compensated in reverse order - compensations are effects too, so a
    crash during rollback resumes the rollback."""

    def __init__(self, ctx: ToolContext, steps: list[Step], *, crash_after: str | None = None,
                 crash_inside: str | None = None) -> None:
        self.ctx, self.steps, self.crash_after, self.crash_inside = ctx, steps, crash_after, crash_inside

    def log(self, event: str, **payload) -> None:
        self.ctx.store.append(self.ctx.run_id, event, {"tool_use_id": self.ctx.tool_use_id, **payload})

    def run(self) -> dict:
        state: dict[str, Any] = {}
        completed: list[Step] = []
        for st in self.steps:
            with self.ctx.effect(f"{self.ctx.idempotency_key}:{st.name}") as eff:
                if eff.done:
                    result, how = eff.stored, "replayed"
                elif eff.in_flight:
                    found = st.recover(state) if st.recover else None
                    if found is None:
                        self.log("saga.step", step=st.name, status="unknown")
                        raise SagaFailed(st.name, "outcome unknown after a crash; needs reconciliation",
                                         self.compensate(completed, state))
                    result, how = eff.commit(found), "recovered"
                else:
                    try:
                        result = st.action(state)
                        if self.crash_inside == st.name:
                            self.crash_inside = None
                            raise Crash(f"worker killed inside {st.name}, after the carrier answered")
                    except Exception as exc:
                        self.log("saga.step", step=st.name, status="failed", error=str(exc))
                        raise SagaFailed(st.name, str(exc), self.compensate(completed, state)) from exc
                    result, how = eff.commit(result), "executed"
            state[st.name] = result
            completed.append(st)
            self.log("saga.step", step=st.name, status=how)
            if self.crash_after == st.name:
                self.crash_after = None
                raise Crash(f"worker killed after saga step {st.name}")
        return state

    def compensate(self, completed: list[Step], state: dict) -> list[str]:
        undone = []
        for st in reversed(completed):
            if st.compensate is None:
                continue
            try:
                with self.ctx.effect(f"{self.ctx.idempotency_key}:{st.name}:undo") as eff:
                    if not eff.done:
                        eff.commit(st.compensate(state))
                        self.log("saga.compensated", step=st.name)
                undone.append(st.name)
            except Exception as exc:                 # a compensation that fails is logged for a person, never hidden
                self.log("saga.compensation_failed", step=st.name, error=str(exc))
                undone.append(f"{st.name} (FAILED: {exc})")
        return undone


def run_naively(steps: list[Step], *, crash_after: str | None) -> dict:
    """What 'just retry' does: run the steps, no effects table, no compensation."""
    state: dict[str, Any] = {}
    for st in steps:
        state[st.name] = st.action(state)
        if crash_after == st.name:
            raise Crash(f"worker killed after step {st.name}")
    return state


# ---------------------------------------------------------------------------- the executor
def make_executor(desk: SupportDesk, db, warehouse: Warehouse, carrier: CarrierAPI, mail: MailGateway, *,
                  durable: bool = True, crash_after: list[str] | None = None, crash_inside: list[str] | None = None):
    reads = d1.desk_executor(desk)
    crash_after, crash_inside = crash_after or [], crash_inside or []

    def execute(name: str, tool_input: dict, ctx: ToolContext) -> dict:
        if name != "arrange_replacement":
            return reads(name, tool_input, ctx)
        order_id, sku, qty = tool_input["order_id"], tool_input["sku"], int(tool_input["qty"])
        received = tool_input["received_sku"]
        # A stable reference per tool call (durable) vs a fresh one per attempt (naive integrations)
        ref = ctx.idempotency_key if durable else f"attempt-{uuid.uuid4().hex[:8]}"

        def create_rma(state):
            content, is_error = desk.run("create_rma", {"order_id": order_id, "sku": sku, "qty": qty, "reason": "wrong_item",
                                                        "notes": f"received {received} instead of {sku}"})
            result = json.loads(content)
            if is_error:
                raise RuntimeError(result["error"])
            return result

        steps = [
            Step("create_rma", create_rma, compensate=lambda s: withdraw_rma(db, s["create_rma"]["rma_id"])),
            Step("reserve_stock", lambda s: warehouse.reserve(sku, qty, ref),
                 compensate=lambda s: warehouse.release(s["reserve_stock"])),
            Step("book_pickup", lambda s: carrier.book_pickup(ref, order_id), compensate=lambda s: carrier.cancel(s["book_pickup"]),
                 recover=lambda s: carrier.find(ref)),
            Step("notify_customer", lambda s: mail.send(ref, desk.requester_email,
                                                        f"Return {s['create_rma']['rma_id']} and replacement for {order_id}")),
        ]
        if not durable:
            state = run_naively(steps, crash_after=crash_after.pop(0) if crash_after else None)
        else:
            saga = Saga(ctx, steps, crash_after=crash_after.pop(0) if crash_after else None,
                        crash_inside=crash_inside.pop(0) if crash_inside else None)
            try:
                state = saga.run()
            except SagaFailed as exc:
                return {"error": f"{exc.step} failed: {exc.reason}", "compensated": exc.compensated}
        return {"rma_id": state["create_rma"]["rma_id"], "reservation": state["reserve_stock"],
                "pickup": state["book_pickup"], "notification": state["notify_customer"]}

    return execute


# ---------------------------------------------------------------------------- scenarios
class World:
    """One fresh copy of every system for a scenario."""

    def __init__(self) -> None:
        self.db = memory_db()
        self.warehouse, self.carrier, self.mail = Warehouse(self.db), CarrierAPI(), MailGateway()
        self.store = d1.fresh_store("04_saga")

    def worker(self, client, ticket, name, **kw):
        desk = SupportDesk(ticket["from_email"], db=self.db, ticket_ref=ticket["ticket_id"])
        execute = make_executor(desk, self.db, self.warehouse, self.carrier, self.mail, **kw)
        return d1.support_runner(self.store, client, execute=execute, worker=name, system=SYSTEM, tools=TOOL_DEFS)


def show_saga(store, run_id: str) -> None:
    for e in store.events(run_id):
        if e["type"].startswith("saga."):
            extra = f"  error={d1.short(e['error'], 60)}" if e.get("error") else ""
            print(f"    {e['seq']:>3}  {e['type']:<17} {e['step']:<16} {e.get('status', '')}{extra}")


def scenario(client, ticket, title: str, *, worker_kwargs: dict, fail_carrier: str | None = None):
    world = World()
    sku = "IMP-250-D"
    before = world.warehouse.reserved(sku)
    world.carrier.fail_next = fail_carrier
    run = world.store.create("replacement", input=d1.run_input(ticket), run_id=f"rep-{ticket['ticket_id']}")
    print(f"  {title}")
    try:
        outcome = world.worker(client, ticket, "worker-a", **worker_kwargs).run(run.id)
    except Crash as exc:
        print(f"    worker-a: CRASH - {exc}")
        outcome = world.worker(client, ticket, "worker-b", durable=worker_kwargs.get("durable", True)).run(run.id)
        print(f"    worker-b: {d1.outcome_line(outcome)}")
    else:
        print(f"    worker-a: {d1.outcome_line(outcome)}")
    print("    reply: " + d1.short(outcome.reply, 150))
    show_saga(world.store, run.id)
    rma_ids = [r[0] for r in world.db.execute("SELECT rma_id FROM rmas WHERE order_id = 'SO-10292'")]
    print(f"    world: reserved {sku} {before} -> {world.warehouse.reserved(sku)} | RMAs for SO-10292: "
          f"{[(r, rma_status(world.db, r)) for r in rma_ids]} | pickups: {len(world.carrier.pickups)} | "
          f"emails: {len(world.mail.sent)}")
    return world


def main() -> None:
    client = get_client()
    header("Lab 04 - Sagas and compensation")
    if is_mock():
        print("[mock] the model is a stand-in that follows the system prompt's plan; the saga, the four systems, "
              "the crashes and the compensations are real code.")
    ticket = d1.ticket(TICKET)
    print(f"Ticket {ticket['ticket_id']}: {d1.short(ticket['body'], 160)}")

    step(1, "The happy path: four effects, four systems, one tool call")
    scenario(client, ticket, "all four steps succeed:", worker_kwargs={})
    print(wrap("The agent planned (look up, check eligibility, arrange) and the saga executed. Each step is an "
               "effect keyed run:tool_use:step, so the whole tool call is at-most-once per step, not just as a whole."))

    step(2, "A step fails: compensate in reverse, tell the model the truth")
    scenario(client, ticket, "SwiftParcel has no collection capacity:", worker_kwargs={},
             fail_carrier="SwiftParcel: no collection capacity in this postcode before 2026-09-22")
    print(wrap("Semantic rollback: the RMA is not deleted, it is withdrawn; the reservation is released; nothing "
               "was emailed. The tool result carries the error and what was undone, and the model does what the "
               "prompt says - it escalates and promises a person, not a collection window."))

    step(3, "A crash between steps: resume without repeating a step")
    scenario(client, ticket, "worker dies after reserve_stock, another worker resumes:",
             worker_kwargs={"crash_after": ["reserve_stock"]})
    print(wrap("worker-b re-executes the tool call (the log has no result for it), the saga finds create_rma and "
               "reserve_stock 'done' in the effects table and replays them, then books and notifies. Reserved "
               "stock went up by 2, once."))

    step(4, "A crash inside a step: recover from the system of record")
    scenario(client, ticket, "worker dies inside book_pickup, after the carrier booked:",
             worker_kwargs={"crash_inside": ["book_pickup"]})
    print(wrap("book_pickup was 'started' but not 'done': outcome unknown. The step's recover() asks SwiftParcel "
               "for the booking under our reference and finds it - one pickup, not two. A step without a recover() "
               "would fail closed here and compensate, which is the safe default for money and the wrong default "
               "for a courier."))

    step(5, "'Just retry': the same crash without effects or compensations")
    scenario(client, ticket, "naive executor, worker dies after reserve_stock, resumed from scratch:",
             worker_kwargs={"durable": False, "crash_after": ["reserve_stock"]})
    print(wrap("The RMA survived only because create_rma is idempotent by design (the tool returns the open RMA). "
               "The warehouse reserved 4 impellers for a 2-impeller replacement, and it will keep them until "
               "someone notices. 'Just retry' is fine when every step is idempotent by itself; it is a bet on "
               "every downstream system, taken silently."))

    step(6, "Where the options stand")
    print("  option              guarantee                              needs                                    fits")
    print("  just retry          none (each step's own idempotency)     idempotent steps, no ordering             reads, idempotent writes")
    print("  saga                every step done or compensated         a compensation per step, durable log      ERP + carrier + email")
    print("  two-phase commit    atomic across systems                  a coordinator and prepare/commit on ALL    databases you control")
    print(wrap("Two-phase commit needs every participant to hold a prepared transaction open until the coordinator "
               "decides; an ERP, a courier and a mail gateway do not offer that, and a coordinator that dies holds "
               "locks everywhere. Sagas trade atomicity for availability: between steps the world is visibly "
               "half-done, so compensations must exist and the customer-facing message must wait for the end."))


if __name__ == "__main__":
    main()
