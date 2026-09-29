"""Solution to exercise 6 - a claim and lease scheme for three workers, checked on a simulated clock.

The design (see solutions/README.md for the reasoning): a 60-second lease renewed by a heartbeat every 20 seconds; a
worker that fails to renew stops before its next side effect; every claim carries a fencing token (a counter that
only grows) which the worker passes to the system of record with each side effect, and the system of record refuses
a token lower than the highest it has seen for that unit; completion is accepted only from the current holder;
transient failures are retried up to three attempts, permanent ones go to the dead letters at once.

The simulation plays the failures the exercise names - a paused ("zombie") worker, a crashed worker, a poison task -
and checks the invariants at the end. No model is involved: this is the part of the swarm that must be right
whatever the agents do.

Run: python advanced/day4_orchestration_at_scale/solutions/ex06_lease_simulation.py
"""
# test: expect=fenced
# test: expect=invariants hold

from __future__ import annotations

import heapq
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

LEASE_S, HEARTBEAT_S, POLL_S, MAX_ATTEMPTS = 60, 20, 5, 3


@dataclass
class Task:
    task_id: str
    serial: str
    duration: int
    status: str = "queued"
    owner: str | None = None
    token: int = 0
    lease_until: float = 0.0
    attempts: int = 0
    permanent_error: bool = False


@dataclass
class Queue:
    tasks: dict[str, Task]
    next_token: int = 0
    log: list[str] = field(default_factory=list)

    def claim(self, owner: str, now: float) -> Task | None:
        for t in self.tasks.values():
            if t.status == "queued":
                self.next_token += 1
                t.status, t.owner, t.token, t.lease_until = "claimed", owner, self.next_token, now + LEASE_S
                t.attempts += 1
                return t
        return None

    def heartbeat(self, task: Task, owner: str, token: int, now: float) -> bool:
        if task.status == "claimed" and task.owner == owner and task.token == token and now <= task.lease_until:
            task.lease_until = now + LEASE_S
            return True
        return False

    def complete(self, task: Task, owner: str, token: int) -> bool:
        if task.status == "claimed" and task.owner == owner and task.token == token:
            task.status, task.owner = "done", None
            return True
        return False

    def fail(self, task: Task, owner: str, token: int, *, permanent: bool) -> str:
        if task.owner != owner or task.token != token:
            return "ignored"
        task.status = "dead" if permanent or task.attempts >= MAX_ATTEMPTS else "queued"
        task.owner = None
        return task.status

    def reap(self, now: float) -> list[str]:
        out = []
        for t in self.tasks.values():
            if t.status == "claimed" and t.lease_until < now:
                t.status = "dead" if t.attempts >= MAX_ATTEMPTS else "queued"
                t.owner = None
                out.append(t.task_id)
        return out


class Calendar:
    """The downstream booking API: not idempotent by itself, so it checks the fencing token per unit."""

    def __init__(self) -> None:
        self.highest: dict[str, int] = {}
        self.bookings: dict[str, tuple[str, int]] = {}      # serial -> (worker, token)
        self.refused: list[str] = []

    def book(self, serial: str, worker: str, token: int) -> bool:
        if token < self.highest.get(serial, 0):
            self.refused.append(f"{worker} token {token} < {self.highest[serial]}")
            return False
        self.highest[serial] = token
        self.bookings[serial] = (worker, token)
        return True


def simulate() -> tuple[Queue, Calendar, list[tuple[float, str]]]:
    queue = Queue({t.task_id: t for t in [Task("T1", "KP250-2608-0006", 30), Task("T2", "KP250-2608-0007", 30),
                                            Task("T3", "KP250-2608-0008", 40), Task("T4", "KP250-2608-0099", 10, permanent_error=True)]})
    calendar = Calendar()
    timeline: list[tuple[float, str]] = []
    pauses = {"worker-A": (10, 100)}          # a 90-second stop-the-world pause while holding T1
    crashes = {"worker-C": 30}                 # worker-C's process dies at t=30 holding its task
    # events: (time, seq, worker, action, task)
    events: list[tuple[float, int, str, str, str | None]] = [(0, i, w, "poll", None) for i, w in
                                                              enumerate(("worker-A", "worker-B", "worker-C"))]
    heapq.heapify(events)
    seq = 10
    held: dict[str, tuple[Task, int, float]] = {}  # worker -> (task, token, started)

    def push(t: float, worker: str, action: str, task_id: str | None = None) -> None:
        nonlocal seq
        seq += 1
        heapq.heappush(events, (t, seq, worker, action, task_id))

    supervisor_t = 0.0
    while events:
        now, _, worker, action, task_id = heapq.heappop(events)
        while supervisor_t + POLL_S <= now:                     # the supervisor sweeps every POLL_S seconds
            supervisor_t += POLL_S
            for reaped in queue.reap(supervisor_t):
                timeline.append((supervisor_t, f"supervisor: lease of {reaped} expired -> {queue.tasks[reaped].status}"))
        if worker in crashes and now >= crashes[worker]:
            if worker in held:
                timeline.append((crashes[worker], f"{worker}: process died holding {held[worker][0].task_id}"))
                held.pop(worker)
            crashes.pop(worker)
            continue
        if worker == "worker-C" and worker not in held and action != "poll":
            continue
        start, end = pauses.get(worker, (None, None))
        if start is not None and start <= now < end:
            push(end, worker, action, task_id)                  # frozen: the event happens when the pause ends
            continue
        if action == "poll":
            task = queue.claim(worker, now)
            if task is None:
                if any(t.status in ("queued", "claimed") for t in queue.tasks.values()):
                    push(now + POLL_S, worker, "poll")
                continue
            held[worker] = (task, task.token, now)
            timeline.append((now, f"{worker}: claims {task.task_id} (attempt {task.attempts}, fencing token {task.token})"))
            if task.permanent_error:
                push(now + task.duration, worker, "fail", task.task_id)
            else:
                push(now + HEARTBEAT_S, worker, "heartbeat", task.task_id)
                push(now + task.duration, worker, "book", task.task_id)
        elif action == "heartbeat" and worker in held:
            task, token, _ = held[worker]
            if task.task_id != task_id or task.status != "claimed" or task.owner != worker:
                continue
            if queue.heartbeat(task, worker, token, now):
                push(now + HEARTBEAT_S, worker, "heartbeat", task.task_id)
            else:
                timeline.append((now, f"{worker}: heartbeat for {task.task_id} refused - lease lost, abandoning before any effect"))
                held.pop(worker)
                push(now, worker, "poll")
        elif action == "book" and worker in held:
            task, token, _ = held[worker]
            if task.task_id != task_id:
                continue
            ok = calendar.book(task.serial, worker, token)
            if not ok:
                timeline.append((now, f"{worker}: book {task.serial} with token {token} -> fenced by the calendar"))
                done = queue.complete(task, worker, token)
                timeline.append((now, f"{worker}: complete {task.task_id} -> {done} (not the current holder)"))
                held.pop(worker)
                push(now, worker, "poll")
                continue
            done = queue.complete(task, worker, token)
            timeline.append((now, f"{worker}: books {task.serial} (token {token}); complete {task.task_id} -> {done}"))
            held.pop(worker)
            push(now, worker, "poll")
        elif action == "fail" and worker in held:
            task, token, _ = held[worker]
            status = queue.fail(task, worker, token, permanent=True)
            timeline.append((now, f"{worker}: {task.task_id} fails permanently (unknown serial) -> {status}"))
            held.pop(worker)
            push(now, worker, "poll")
    return queue, calendar, timeline


def main() -> None:
    header("Exercise 6 - a claim and lease scheme, checked on a simulated clock (solution)")
    step(1, f"Lease {LEASE_S}s, heartbeat every {HEARTBEAT_S}s, supervisor sweep every {POLL_S}s, {MAX_ATTEMPTS} attempts")
    queue, calendar, timeline = simulate()
    for t, what in sorted(timeline, key=lambda x: x[0]):
        print(f"  t={t:>5.0f}s  {what}")

    step(2, "Invariants")
    booked_once = len(calendar.bookings) == len({s for s in calendar.bookings})
    zombie_refused = bool(calendar.refused)
    statuses = {t.task_id: t.status for t in queue.tasks.values()}
    print(f"  final statuses: {statuses}")
    print(f"  bookings: {dict(sorted((s, f'{w} token {tok}') for s, (w, tok) in calendar.bookings.items()))}")
    print(f"  refused by fencing: {calendar.refused}")
    ok = booked_once and zombie_refused and statuses == {"T1": "done", "T2": "done", "T3": "done", "T4": "dead"}
    print(f"  every unit booked once, the zombie's late write refused, the crash retried, the poison task dead-lettered: "
          f"{'invariants hold' if ok else 'INVARIANTS BROKEN'}")


if __name__ == "__main__":
    main()
