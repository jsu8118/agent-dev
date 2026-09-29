"""Lab 01 - Durable coordinator: a coordinator run that dispatches the 11 units through a work queue, dies half-way, and resumes.

Objective
    Build the recall swarm's spine: a coordinator agent running on Day 1's `DurableRunner`, a SQLite work queue
    with priorities, claims and leases, and worker agents that are durable runs themselves. Kill the coordinator
    after five unit tasks and a worker in the middle of a unit, resume both from their logs and the queue, and
    watch a poison task end in the dead letters instead of being retried forever.

Concepts
    durable coordinator, work queue (enqueue / claim / complete / fail), priorities, leases and dead-worker
    reaping, at-least-once delivery with idempotent tasks, transient vs permanent failures, dead letters, the
    queue as the system of record for an in-flight `dispatch_units` effect, two levels of durability
    (coordinator run, unit runs), replayed vs executed tool calls

Run
    python advanced/day4_orchestration_at_scale/labs/01_durable_coordinator.py [--crash-after 5] [--profile tuned|naive]

What to observe
    * Step 1: a stray task from a dry run already sits in the queue - it names a serial the desk does not know.
    * Step 2: a worker dies after its second tool call; its lease expires, the supervisor re-queues the task, and
      the retry resumes the worker's own run with 2 tool results replayed from its log.
    * Step 2: the coordinator dies inside `dispatch_units`; the log ends on a `tool.started` with no result.
    * Step 3: a second process resumes the same run id: `list_affected_units` is replayed, `dispatch_units` is
      re-executed against the queue (nothing done is repeated), the stray task becomes a dead letter at once
      (a permanent failure is not retried), and the summary names it.
    * Step 4: the coordinator's log, every unit run with its attempts, and the dead letter with its reason.
"""
# test: expect=resumed run: status=completed
# test: expect=dead letter

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import Crash, DurableRunner, Outcome, RunStore, ToolContext  # noqa: E402
from labkit import MODEL, get_client, header, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402

RUN_ID = "coordinator:RC-2026-03"
LEASE_S = 0.05                      # short on purpose so a dead worker's lease expires within the lab
STRAY = "batch:KP250-2608-0099"     # left in the queue by last week's dry run; the serial does not exist


def validate(task: d4.Task, result: d4.WorkerResult) -> str | None:
    """A worker that finished is not a worker that succeeded. Returns why the result is unusable, or None."""
    if result.outcome.status != "completed":
        return f"worker run {result.outcome.status}"
    planned = {p.get("serial") for p in result.plans}
    missing = [s for s in task.payload["serials"] if s not in planned]
    if missing:
        return f"no plan for {', '.join(missing)}"
    unknown = [p["serial"] for p in result.plans if not p.get("customer_id")]
    if unknown:
        return f"the desk has no record of {', '.join(unknown)}"
    return None


class Dispatcher:
    """The coordinator's `dispatch_units` and `collect_results` tools: enqueue batches, drain the queue with
    in-process workers, collect what they produced.

    Everything here is idempotent by construction: a batch already in the queue is not enqueued twice, a task
    already done is never claimed again, and a unit run resumed under the same run id replays its own log.
    Failures are classified: a dead worker (transient) is retried after its lease expires; an unusable result
    (permanent - the same input would fail the same way) goes straight to the dead letters."""

    def __init__(self, queue: d4.WorkQueue, store: RunStore, client, desk: d4.RecallDesk, *, owner: str,
                 profile: str = "tuned", crash_after_tasks: int | None = None,
                 worker_crashes: dict[str, int] | None = None, log=print) -> None:
        self.queue, self.store, self.client, self.desk, self.owner = queue, store, client, desk, owner
        self.profile = d4.PROFILES[profile]
        self.crash_after_tasks = crash_after_tasks
        self.worker_crashes = dict(worker_crashes or {})
        self.log = log
        self.completed_now = 0
        self.records: dict[str, dict] = {}              # task_id -> run id, brief, reply (lab 07 reads these)

    def dispatch(self, batches: list[dict]) -> dict:
        new = 0
        for b in batches:
            serials_ = list(b.get("serials") or [])
            priority = max((d4.PRIORITY.get(d4.unit(s)["risk_class"], 0) for s in serials_ if s in d4.serials()), default=0)
            new += self.queue.enqueue("batch:" + "+".join(serials_), "unit_plan", {"serials": serials_, "brief": b.get("brief")},
                                      priority=priority)
        self.log(f"    dispatch_units: {new} new batch task(s) enqueued, {len(batches) - new} already known; "
                 f"queue={self.queue.stats()}")
        self.drain()
        return {"enqueued": new, **self.queue.stats()}

    def drain(self) -> None:
        while True:
            task = self.queue.claim(self.owner, lease_s=LEASE_S)
            if task is None:
                return
            serials_ = task.payload["serials"]
            worker_id = f"{self.owner}/w{task.attempts}"
            crash_calls = self.worker_crashes.pop(task.task_id, None)
            try:
                result = d4.run_unit_worker(self.client, self.store, self.desk, serials_, run_id=f"unit:{task.task_id}",
                                            worker=worker_id, brief=task.payload.get("brief"),
                                            report=self.profile["report"], crash_after_calls=crash_calls)
            except Crash as exc:
                self.log(f"    !! worker {worker_id} died on {task.task_id} (attempt {task.attempts}): {exc}")
                time.sleep(LEASE_S * 1.5)                           # the lease runs out ...
                reaped = self.queue.reap()                           # ... and the supervisor's sweep re-queues the task
                self.log(f"    supervisor: lease expired -> re-queued {reaped}; queue={self.queue.stats()}")
                continue
            self.records[task.task_id] = {"run_id": result.run_id, "brief": task.payload.get("brief") or "",
                                          "reply": result.outcome.reply}
            problem = validate(task, result)
            if problem:
                status = self.queue.fail(task.task_id, self.owner, f"permanent: {problem}", retry=False)
                self.log(f"    task {task.task_id} -> {status} (dead letter): {problem}; retrying the same input cannot help")
                continue
            self.queue.complete(task.task_id, self.owner, {"plans": result.plans, "reply": result.outcome.reply,
                                                           "turns": result.outcome.turns,
                                                           "replayed_tools": result.outcome.replayed_tools,
                                                           "executed_tools": result.outcome.executed_tools})
            self.completed_now += 1
            statuses = ", ".join(f"{p['serial']} {p['status']}" for p in result.plans)
            resumed = f" (resumed: {result.outcome.replayed_tools} tool results replayed)" if result.outcome.replayed_tools else ""
            self.log(f"    task {task.task_id} done by {worker_id}: {statuses}{resumed}")
            if self.crash_after_tasks is not None and self.completed_now >= self.crash_after_tasks:
                raise Crash(f"coordinator process died after {self.completed_now} unit tasks")

    def collect(self) -> dict:
        done = self.queue.results()
        dead = [{"task_id": t.task_id, "error": t.error} for t in self.queue.dead_letters()]
        if self.profile["digest"]:                                   # tuned: the parsed plans only
            return {"plans": [p for r in done for p in r["payload"]["plans"]], "queue": self.queue.stats(), "dead_letters": dead}
        return {"reports": [r["payload"]["reply"] for r in done], "queue": self.queue.stats(), "dead_letters": dead}


def make_coordinator(store: RunStore, client, dispatcher: Dispatcher, desk: d4.RecallDesk, *, worker: str) -> DurableRunner:
    def execute(name: str, tool_input: dict, ctx: ToolContext):
        if name == "list_affected_units":
            return desk.run(name, {})
        if name == "dispatch_units":
            with ctx.effect() as eff:
                if eff.done:
                    return eff.stored
                if eff.in_flight:
                    dispatcher.log("    dispatch_units: effect in flight from a previous process - the queue is the system "
                                   "of record, so re-running it only processes what is not done yet")
                return eff.commit(dispatcher.dispatch(tool_input.get("batches") or []))
        if name == "collect_results":
            return dispatcher.collect()
        return {"error": f"unknown tool {name}"}

    profile = dispatcher.profile
    return DurableRunner(store, client, model=MODEL, system=d4.coordinator_system(batch=profile["batch"], brief=profile["brief"]),
                         tools=d4.COORDINATOR_TOOLS, execute=execute, max_turns=8, worker=worker,
                         create_kwargs={"cache_control": {"type": "ephemeral"}})


def run_campaign(client, store: RunStore, queue: d4.WorkQueue, desk: d4.RecallDesk, *, profile: str = "tuned",
                 owner: str = "coordinator", run_id: str = RUN_ID, log=print) -> tuple[Outcome, Dispatcher]:
    """One uninterrupted campaign run (lab 07 calls this for the self-hosted rows of its comparison)."""
    store.create("campaign", input={"message": "Run recall campaign RC-2026-03: plan the remedy for every affected unit."},
                 run_id=run_id)
    dispatcher = Dispatcher(queue, store, client, desk, owner=owner, profile=profile, log=log)
    return make_coordinator(store, client, dispatcher, desk, worker=owner).run(run_id), dispatcher


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--crash-after", type=int, default=5, help="kill the coordinator after this many unit tasks")
    parser.add_argument("--profile", choices=sorted(d4.PROFILES), default="tuned",
                        help="batching, brief and report style of the swarm (see lab 07)")
    args = parser.parse_args()

    header("Lab 01 - Durable coordinator, work queue, crash and resume")
    d4.mock_note("the coordinator and the workers are rule-based stand-ins that read real tool results; the queue, leases, "
                 "crashes, replay and resumption are the real code (advanced/lib/durable.py + _day4.WorkQueue).")
    client = get_client()
    db = d4.fresh_db("lab01.db")
    store, queue, desk = RunStore(db), d4.WorkQueue(db), d4.RecallDesk()

    step(1, "The campaign: 11 affected units, 7 customers, 3 remedy kits - and the queue that will carry them")
    rows = [[u["serial_number"], u["customer_id"], u["customer"][:26], u["region"], u["risk_class"], d4.kit_for(u["sku"])["sku"]]
            for u in d4.units()]
    print(d4.table(rows, ["serial", "customer", "name", "region", "risk", "kit"]))
    print("\n  The queue is one SQLite table. A claim is ONE atomic statement - no read-then-write race between workers:")
    print("    UPDATE tasks SET status='claimed', owner=?, lease_until=?, attempts=attempts+1, version=version+1")
    print("      WHERE task_id = (SELECT task_id FROM tasks WHERE status='queued' OR (status='claimed' AND lease_until < ?)")
    print("                       ORDER BY priority DESC, created_at, task_id LIMIT 1) RETURNING *")
    print("  Priority comes from the risk class (safety 2 > production 1 > standard 0); a claim expires with its lease.")
    queue.enqueue(STRAY, "unit_plan", {"serials": ["KP250-2608-0099"], "brief": "dry run"}, priority=0)
    print(f"  Already in the queue: {STRAY}, left by last week's dry run (KP250-2608-0099 is not an affected unit).")

    step(2, f"First coordinator process: dies after {args.crash_after} unit tasks; one worker dies mid-unit")
    run = store.create("campaign", input={"message": "Run recall campaign RC-2026-03: plan the remedy for every affected unit."},
                       run_id=RUN_ID)
    dispatcher = Dispatcher(queue, store, client, desk, owner="coordinator-1", profile=args.profile,
                            crash_after_tasks=args.crash_after, worker_crashes={"batch:KP250-2608-0004": 2})
    try:
        make_coordinator(store, client, dispatcher, desk, worker="coordinator-1").run(run.id)
        print("  (no crash - lower --crash-after to see one)")
    except Crash as exc:
        print(f"\n  !! {exc}")
    events = store.events(run.id)
    print(f"  run status={store.get(run.id).status}, events={len(events)}, last={events[-1]['type']} ({events[-1].get('name', '')})")
    print(f"  queue after the crash: {queue.stats()}")

    step(3, "A second coordinator process resumes the same run id")
    dispatcher2 = Dispatcher(queue, store, client, desk, owner="coordinator-2", profile=args.profile)
    outcome = make_coordinator(store, client, dispatcher2, desk, worker="coordinator-2").run(run.id)
    print(f"\n  resumed run: status={outcome.status} turns={outcome.turns} replayed_tools={outcome.replayed_tools} "
          f"executed_tools={outcome.executed_tools}; unit tasks completed in this process: {dispatcher2.completed_now}")
    print("\n  Campaign summary (the coordinator's final message):")
    print(wrap(outcome.reply, "    "))

    step(4, "What the logs say: the coordinator's events, every unit run, the dead letter")
    kinds = [e["type"] + (f"({e['name']})" if e.get("name") else "") for e in store.events(run.id)]
    print(wrap("coordinator log: " + " -> ".join(kinds), "  "))
    rows = []
    for task in queue.tasks():
        r = store.get(f"unit:{task.task_id}")
        payload = task.result or {}
        rows.append([task.task_id.replace("batch:", ""), task.status, task.attempts, r.status, payload.get("turns", "-"),
                     payload.get("replayed_tools", "-"), payload.get("executed_tools", "-")])
    print(d4.table(rows, ["task", "queue status", "attempts", "run status", "turns", "replayed", "executed"]))
    for t in queue.dead_letters():
        print(f"\n  dead letter: {t.task_id} after {t.attempts} attempt(s) - {t.error}")
    print(f"  queue: {queue.stats()}; results rows (one per completed attempt): {len(queue.results(latest_only=False))}")
    print(wrap("Two levels of durability: the coordinator resumed without re-generating its first turns, and the worker that "
               "died resumed ITS run with its first tool results replayed - the queue only told the supervisor what to retry. "
               "Retry what can succeed on retry (a dead worker, a 429, an outage); dead-letter what cannot (bad input).", "  "))


if __name__ == "__main__":
    main()
