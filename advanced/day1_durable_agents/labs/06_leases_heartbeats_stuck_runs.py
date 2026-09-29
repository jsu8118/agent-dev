"""Lab 06 - Leases, heartbeats and stuck runs: two workers, one run, and a worker that dies for real.

Objective
    Put two worker threads on the same run and watch the lease keep the second one off; make a slow tool
    outlive the lease and see what heartbeats change (with them the second worker is refused, without them it
    takes the run over while the first is still working, and the first writes once more before its next
    heartbeat tells it to stop); kill a worker the way an OOM kill does (no release) and watch stuck() find the
    run once its lease expires; take it over; and run the sweeper a production deployment runs every minute.

Concepts
    leases (owner + expiry) vs locks; TTL and heartbeat interval; heartbeats from inside long tools; stale-lease
    takeover; the false takeover (TTL shorter than a step), LeaseLost at the zombie's next heartbeat, and the
    write that lands before it; stuck-run detection; the sweeper loop; detection latency = TTL + sweep interval.

Run
    python advanced/day1_durable_agents/labs/06_leases_heartbeats_stuck_runs.py

What to observe
    * "leased by another worker": the second worker is refused while the first holds the lease.
    * With heartbeats a 2.5-second tool keeps a 1-second lease alive; without them the lease lapses mid-tool,
      worker-b takes over and finishes, and worker-a stops with LeaseLost at its next heartbeat - after its
      tool result has already landed as a duplicate (exercise 11 fences that write).
    * After the kill the lease is still owned by the dead worker; stuck() is empty until it expires, then
      lists the run; the takeover finishes it with the logged results replayed.
    * The sweeper table: healthy, dead and finished runs, and what it does with each.
"""
# test: expect=leased by another worker
# test: expect=stuck()
# test: timeout=180

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import Crash, DurableRunner, RunStore
from kestrel.support_tools import SupportDesk
from labkit import get_client, header, is_mock, step, wrap
from labkit.data import memory_db

import _day1 as d1

TICKET = "T-1001"          # GreenValley: "could you send me the tracking number for SO-10303?" (2 tools, 3 turns)


def slow_executor(desk: SupportDesk, *, delay_s: float, heartbeat=None):
    """get_order takes `delay_s` seconds (a slow ERP); `heartbeat(run_id)` is called every 0.25 s meanwhile."""
    reads = d1.desk_executor(desk)

    def execute(name, tool_input, ctx):
        if name == "get_order":
            waited = 0.0
            while waited < delay_s:
                time.sleep(min(0.25, delay_s - waited))
                waited += 0.25
                if heartbeat is not None:
                    heartbeat(ctx.run_id)
        return reads(name, tool_input, ctx)

    return execute


class HeartbeatLog(RunStore):
    """The same on-disk store, opened by one worker's process, remembering what heartbeat() answered."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.answers: list[bool] = []

    def heartbeat(self, run_id: str, owner: str, ttl_s: float = 30.0) -> bool:
        ok = super().heartbeat(run_id, owner, ttl_s)
        self.answers.append(ok)
        return ok


def answers_line(answers: list[bool]) -> str:
    """Compress [True, True, True, False] into 'True x3, False x1'."""
    runs: list[list] = []
    for a in answers:
        if runs and runs[-1][0] == a:
            runs[-1][1] += 1
        else:
            runs.append([a, 1])
    return ", ".join(f"{a} x{n}" for a, n in runs) or "none"


def make_worker(store: RunStore, client, db, ticket, name: str, *, delay_s: float = 0.0, ttl_s: float = 30.0,
                heartbeats: bool = False, runner_cls=DurableRunner, **kw) -> DurableRunner:
    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    holder: dict = {}

    def beat(run_id):
        holder["runner"].store.heartbeat(run_id, name, ttl_s)

    execute = slow_executor(desk, delay_s=delay_s, heartbeat=beat if heartbeats else None)
    holder["runner"] = d1.support_runner(store, client, execute=execute, worker=name, lease_ttl_s=ttl_s,
                                         runner_cls=runner_cls, **kw)
    return holder["runner"]


def in_thread(runner: DurableRunner, run_id: str) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def target():
        try:
            result["outcome"] = runner.run(run_id)
        except BaseException as exc:            # Crash is a BaseException
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def event_counts(store, run_id) -> str:
    counts = Counter(e["type"] for e in store.events(run_id))
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if k in ("model.response", "tool.started", "tool.result"))


# ---------------------------------------------------------------------------- steps
def step_contention(store, client, ticket):
    db = memory_db()
    run = store.create("support", input=d1.run_input(ticket), run_id="lease-contention")
    a = make_worker(store, client, db, ticket, "worker-a", delay_s=1.0)
    b = make_worker(store, client, db, ticket, "worker-b")
    thread, result = in_thread(a, run.id)
    time.sleep(0.4)                                    # worker-a is inside its slow get_order now
    record = store.get(run.id)
    print(f"while worker-a works: status={record.status} lease_owner={record.lease_owner} "
          f"lease expires in {record.lease_until - time.time():.0f} s")
    try:
        b.run(run.id)
    except RuntimeError as exc:
        print(f"worker-b: RuntimeError: {exc}")
    thread.join()
    print(f"worker-a: {d1.outcome_line(result['outcome'])}  events: {event_counts(store, run.id)}")
    print(f"after completion: lease_owner={store.get(run.id).lease_owner}")
    print(wrap("A lease is a lock with an expiry: UPDATE ... WHERE lease_owner IS NULL OR lease_until < now, one "
               "row, atomic. Two workers that both pick the run from the queue cannot both win it, and a winner "
               "that dies does not hold it forever."))


def step_heartbeats(store, client, ticket):
    for heartbeats in (True, False):
        db = memory_db()
        run = store.create("support", input=d1.run_input(ticket), run_id=f"lease-heartbeat-{'on' if heartbeats else 'off'}")
        a_store = HeartbeatLog(store.path)            # worker-a's own connection, as if it were another process
        a = make_worker(a_store, client, db, ticket, "worker-a", delay_s=2.5, ttl_s=1.0, heartbeats=heartbeats)
        b = make_worker(store, client, db, ticket, "worker-b", ttl_s=1.0)
        thread, result = in_thread(a, run.id)
        time.sleep(1.6)                                # past the 1-second TTL, inside the 2.5-second tool
        label = "with heartbeats every 0.25 s" if heartbeats else "without heartbeats"
        try:
            outcome_b = b.run(run.id)
            print(f"{label}: worker-b at t=1.6 s ACQUIRED the lease (it had expired at t=1.0 s) and ran the run: "
                  f"{d1.outcome_line(outcome_b)}")
        except RuntimeError as exc:
            print(f"{label}: worker-b at t=1.6 s -> RuntimeError: {exc}")
        thread.join()
        outcome_a, error_a = result.get("outcome"), result.get("error")
        print(f"  worker-a: {d1.outcome_line(outcome_a) if outcome_a else f'{type(error_a).__name__}: {error_a}'}")
        print(f"  log: {event_counts(store, run.id)}")
        print(f"  worker-a's heartbeat() answers, in order: {answers_line(a_store.answers)}")
        if not heartbeats:
            dup = [e for e in store.events(run.id, types=("tool.result",)) if e["name"] == "get_order"]
            print(f"  get_order results in the log: {len(dup)} - the same tool_use answered twice")
            print(wrap("worker-a's heartbeat before its turn-3 model call answered False, and the runner stopped it "
                       "with LeaseLost: no second turn 3. But the get_order result it wrote on returning from the "
                       "slow tool - before that heartbeat - landed in the log anyway. The heartbeat check narrows "
                       "the zombie's window to one step; fencing every write on the lease (exercise 11) closes it."))
    print(wrap("A false takeover is worse than a slow one: for one step two workers believe they own the run, and "
               "whatever the zombie does in that step - a tool result, a write to another system, a whole model "
               "turn if the takeover happens during a long model call - happens twice. Rule: TTL is a multiple of "
               "the heartbeat interval (3x is common), and anything that can take longer than the TTL - a slow "
               "tool, a long model call - heartbeats from inside. The runner heartbeats before every model call "
               "and stops when that fails; your tools heartbeat through ctx.store.heartbeat(run_id, worker, ttl)."))


def step_killed(store, client, ticket):
    db = memory_db()
    run = store.create("support", input=d1.run_input(ticket), run_id="lease-killed")
    a = make_worker(store, client, db, ticket, "worker-a", ttl_s=1.0, runner_cls=d1.KilledWorker,
                    crash_at=("before_tool", 2))
    try:
        a.run(run.id)
    except Crash as exc:
        print(f"worker-a: {exc} (kill -9: no release)")
    else:
        print("worker-a finished before the crash point (live: the model took a different path) - rerun the lab")
        return
    record = store.get(run.id)
    print(f"store right after: status={record.status} lease_owner={record.lease_owner} "
          f"lease valid for another {record.lease_until - time.time():.1f} s")
    print(f"stuck(older_than_s=0) now: {[r.id for r in store.stuck(older_than_s=0)]}")
    time.sleep(1.2)
    print(f"stuck(older_than_s=0) after the TTL: {[r.id for r in store.stuck(older_than_s=0)]}")
    c = make_worker(store, client, db, ticket, "worker-c", ttl_s=30.0)
    outcome = c.run(run.id)
    print(f"worker-c takes over: {d1.outcome_line(outcome)}")
    print(f"log: {event_counts(store, run.id)}")
    print(wrap("Between the kill and the expiry the run looks alive - status running, a valid lease - and nothing "
               "can tell the difference from the outside. That window IS your detection latency; the TTL sets "
               "it, and the sweeper's interval adds to it."))


def sweep(store: RunStore, *, older_than_s: float, worker_factory) -> list[str]:
    """What a cron job does every minute: re-queue and resume runs whose worker died."""
    actions = []
    for run in store.stuck(older_than_s=older_than_s):
        outcome = worker_factory(run).run(run.id)
        actions.append(f"{run.id}: took over from {run.lease_owner!r} -> {outcome.status} "
                       f"(replayed {outcome.replayed_tools}, executed {outcome.executed_tools})")
    return actions


def step_sweeper(store, client, ticket):
    db = memory_db()
    healthy = store.create("support", input=d1.run_input(ticket), run_id="sweep-healthy")
    dead = store.create("support", input=d1.run_input(ticket), run_id="sweep-dead")
    done = store.create("support", input=d1.run_input(ticket), run_id="sweep-done")
    make_worker(store, client, db, ticket, "worker-d").run(done.id)                # finished normally
    store.acquire(healthy.id, "worker-live", ttl_s=30.0)                             # a live worker mid-run
    store.set_status(healthy.id, "running")
    try:                                                                             # a worker that died mid-run
        make_worker(store, client, db, ticket, "worker-x", ttl_s=0.5, runner_cls=d1.KilledWorker,
                    crash_at=("after_tool", 1)).run(dead.id)
    except Crash:
        pass
    time.sleep(0.7)
    print(f"{'run':<14} {'status':<10} {'lease_owner':<12} {'lease':<8} action")
    for run in (healthy, dead, done):
        r = store.get(run.id)
        state = "expired" if r.lease_until and r.lease_until < time.time() else ("live" if r.lease_until else "-")
        action = "leave alone" if r.status != "running" else ("leave alone (heartbeating)" if state == "live" else "take over")
        print(f"{r.id:<14} {r.status:<10} {str(r.lease_owner):<12} {state:<8} {action}")
    actions = sweep(store, older_than_s=0, worker_factory=lambda run: make_worker(store, client, db, ticket, "worker-sweeper"))
    print("sweeper:", *actions, sep="\n  ")
    store.release(healthy.id, "worker-live")
    print(wrap("stuck() is a query, not a judgement: status = running AND lease expired more than older_than_s ago. "
               "Set older_than_s above your heartbeat jitter so a worker that is merely late is not taken over, and "
               "make the takeover an ordinary run(): the lease acquire is the only coordination needed."))


def main() -> None:
    client = get_client()
    header("Lab 06 - Leases, heartbeats and stuck runs")
    if is_mock():
        print("[mock] worker threads, sleeps and kills are simulated in-process; the lease table, the heartbeats "
              "and stuck() are the real code, shared through the on-disk store like separate processes would.")
    ticket = d1.ticket(TICKET)
    store = d1.fresh_store("06_leases")

    step(1, "Two workers, one run: the lease")
    step_contention(store, client, ticket)

    step(2, "A tool slower than the lease: heartbeats on, heartbeats off")
    step_heartbeats(store, client, ticket)

    step(3, "A worker killed mid-run: stuck() and the takeover")
    step_killed(store, client, ticket)

    step(4, "The sweeper")
    step_sweeper(store, client, ticket)

    step(5, "Choosing the numbers")
    print("  heartbeat every h, lease TTL T = 3h, sweeper every S:")
    print("    detection of a dead worker    between T and T + S after its last heartbeat")
    print("    false takeover                impossible while the worker heartbeats; possible whenever a step > T runs without")
    print("    example                       h = 10 s, T = 30 s, S = 60 s -> a dead worker is resumed within 30-90 s;")
    print("                                  a 45-second ERP call must heartbeat (the tool's job), or T must exceed 45 s")
    print(wrap("A longer TTL means slower recovery; a shorter one means more false takeovers under load (GC pauses, "
               "a saturated database, a slow model response). Measure your slowest step and your heartbeat "
               "jitter before picking, and log every takeover: a rising count is the earliest sign of an "
               "overloaded fleet."))


if __name__ == "__main__":
    main()
