"""Lab 03 - Budgets, circuit breakers and loop detection: the swarm's safety equipment, each tripped on purpose.

Objective
    Give the recall swarm the four controls a production swarm needs and watch each one fire: a dollar budget for
    the whole swarm (read from labkit's LEDGER) that pauses dispatch, a per-agent cap that first warns an agent and
    then kills it, a circuit breaker on the scheduling tool that stops a failing dependency from being hammered and
    lets it recover, a loop detector for an agent that repeats the same call, and backpressure from a token-rate
    window and from 429s.

Concepts
    swarm vs per-agent budgets (tokens, dollars, time), soft stop then hard stop, admission control vs a
    tripwire (overshoot), circuit breaker states (closed / open / half-open), fail-fast, loop detection
    (identical calls, ping-pong), rate windows and backpressure, 429 as a signal, the campaign's stop conditions

Run
    python advanced/day4_orchestration_at_scale/labs/03_budgets_circuit_breakers_loops.py

What to observe
    * Step 1: the "thorough" worker gets a warning as a tool error, keeps going, and is killed at the next call; the
      run is marked failed with the reason, and the desk shows how many calls it made.
    * Step 2: the naive gate stops only after the cap is crossed (the unit in flight finishes); admission control
      with a per-unit estimate stops before it, with the rest still queued.
    * Step 3: three failures open the circuit; the next units fail fast without touching the desk; after the outage
      one probe call closes it again.
    * Step 4: the looping worker gets a loop-guard error at the 3rd identical call and is stopped at the 5th.
    * Step 5: the rate window admits work in waves; a 429 that survives the SDK's retries is a backpressure signal.
"""
# test: expect=circuit OPEN
# test: expect=loop detected

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic  # noqa: E402

from advanced.lib.durable import RunStore  # noqa: E402
from labkit import LEDGER, MODEL, cost_usd, get_client, header, mock_api, step, wrap  # noqa: E402

import _day4 as d4  # noqa: E402


class HardStop(BaseException):
    """The supervisor kills an agent (a BaseException so the runner does not turn it into a tool error)."""


# ------------------------------------------------------------------------------ budgets
class SwarmBudget:
    """A dollar cap for the whole swarm, read from labkit's LEDGER (which meters every call this process makes).

    `exhausted()` is the naive gate - checked before claiming the next task, so the task that crosses the cap still
    runs (the overshoot). `admits(estimate)` is admission control: a task is started only if what is spent plus
    what it is expected to cost still fits, so the cap is a bound rather than a tripwire."""

    def __init__(self, cap_usd: float) -> None:
        self.cap = cap_usd
        self.start = LEDGER.total_cost

    @property
    def spent(self) -> float:
        return LEDGER.total_cost - self.start

    def exhausted(self) -> bool:
        return self.spent >= self.cap

    def admits(self, estimate: float) -> bool:
        return self.spent + estimate <= self.cap


class AgentGuard:
    """Per-agent cap, checked before every tool call from the run's own log: warn once (a tool error the model
    reads), then kill (HardStop)."""

    def __init__(self, store: RunStore, run_id: str, cap_usd: float) -> None:
        self.store, self.run_id, self.cap = store, run_id, cap_usd
        self.warned = False
        self.cost = 0.0

    def __call__(self, name: str, tool_input: dict) -> None:
        self.cost = sum(cost_usd(e["usage"], MODEL) for e in self.store.events(self.run_id, types=("model.response",)))
        if self.cost < self.cap:
            return
        if not self.warned:
            self.warned = True
            raise d4.ToolFailure(f"Budget: this agent has spent ${self.cost:.4f} of its ${self.cap:.2f} cap. Stop exploring "
                                 "and report the plan from what you already have.")
        raise HardStop(f"agent budget exceeded (${self.cost:.4f} > ${self.cap:.2f}) after the warning was ignored")


# ------------------------------------------------------------------------------ circuit breaker
class CircuitBreaker:
    def __init__(self, tool: str, *, failure_threshold: int = 3, window: int = 5, cooldown_s: float = 0.2) -> None:
        self.tool, self.threshold, self.cooldown = tool, failure_threshold, cooldown_s
        self.recent: deque = deque(maxlen=window)
        self.state = "closed"
        self.opened_at = 0.0
        self.transitions: list[str] = []

    def before(self, name: str, tool_input: dict) -> None:
        if name != self.tool:
            return
        if self.state == "open":
            if time.time() - self.opened_at >= self.cooldown:
                self._go("half_open")
            else:
                raise d4.ToolFailure(f"{self.tool} unavailable: circuit OPEN (fail fast). Do not retry; report pending_schedule.")

    def after(self, name: str, tool_input: dict, result: dict) -> None:
        if name != self.tool:
            return
        ok = "error" not in result
        self.recent.append(ok)
        if self.state == "half_open":
            self._go("closed" if ok else "open")
        elif self.state == "closed" and list(self.recent).count(False) >= self.threshold:
            self._go("open")

    def _go(self, state: str) -> None:
        self.transitions.append(f"{self.state}->{state}")
        self.state = state
        if state == "open":
            self.opened_at = time.time()
            self.recent.clear()


# ------------------------------------------------------------------------------ loop detection
class LoopDetector:
    """Identical consecutive tool calls (name + input) and ping-pong between agents (A->B->A->B with the same digest)."""

    def __init__(self, *, soft: int = 3, hard: int = 5) -> None:
        self.soft, self.hard = soft, hard
        self.calls: list[str] = []
        self.messages: list[tuple[str, str, str]] = []

    @staticmethod
    def digest(obj) -> str:
        return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:8]

    def observe_call(self, name: str, tool_input: dict) -> None:
        key = f"{name}:{self.digest(tool_input)}"
        self.calls.append(key)
        run = 0
        for k in reversed(self.calls):
            if k != key:
                break
            run += 1
        if run == self.soft:
            raise d4.ToolFailure(f"Loop guard: this is identical call #{run} of {name} with the same input; the result will "
                                 "not change. Use the result you already have and move on.")
        if run >= self.hard:
            raise HardStop(f"loop detected: {name} called {run} times with identical input")

    def observe_message(self, sender: str, receiver: str, payload) -> str | None:
        self.messages.append((sender, receiver, self.digest(payload)))
        tail = self.messages[-4:]
        if len(tail) == 4 and tail[0] == tail[2] and tail[1] == tail[3] and tail[0][0] == tail[1][1]:
            return f"ping-pong: {tail[0][0]}<->{tail[1][0]} exchanging the same two messages ({tail[0][2]}/{tail[1][2]})"
        return None


# ------------------------------------------------------------------------------ backpressure
class RateWindow:
    """Admits work while the tokens used in the last `window_s` seconds stay under the limit (a sliding window)."""

    def __init__(self, limit_tokens: int, window_s: float, clock) -> None:
        self.limit, self.window, self.clock = limit_tokens, window_s, clock
        self.used: deque = deque()

    def admit(self, tokens: int) -> float:
        """Returns how long the caller had to wait (0 when admitted at once)."""
        waited = 0.0
        while True:
            now = self.clock()
            while self.used and self.used[0][0] <= now - self.window:
                self.used.popleft()
            if sum(t for _, t in self.used) + tokens <= self.limit:
                self.used.append((now, tokens))
                return waited
            wait = self.used[0][0] + self.window - now
            self.clock.advance(wait)
            waited += wait


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ------------------------------------------------------------------------------ the lab
def run_guarded(client, store, desk, serial, *, mode, guard=None, breaker=None, detector=None, run_id=None):
    on_call = guard or (breaker.before if breaker else None) or (detector.observe_call if detector else None)
    after = breaker.after if breaker else None
    run_id = run_id or f"unit:{mode}:{serial}"
    try:
        return d4.run_unit_worker(client, store, desk, [serial], run_id=run_id, mode=mode, on_call=on_call, after_call=after,
                                  max_turns=20)
    except HardStop as exc:
        store.set_status(run_id, "failed", error=str(exc))
        return None


def main() -> None:
    header("Lab 03 - Budgets, circuit breakers and loop detection")
    d4.mock_note("the 'thorough' and 'loop' workers are scripted misbehaviours (a real model may or may not misbehave this "
                 "way); the budget, breaker, detector and rate window are the real controls, driven by real tool traffic.")
    client = get_client()
    db = d4.fresh_db("lab03.db")
    store, desk = RunStore(db), d4.RecallDesk()

    step(1, "Per-agent cap: warn the agent, then kill it")
    guard = AgentGuard(store, "unit:thorough:KP250-2608-0002", cap_usd=0.05)
    result = run_guarded(client, store, desk, "KP250-2608-0002", mode="thorough", guard=guard)
    run = store.get("unit:thorough:KP250-2608-0002")
    calls = [e["name"] for e in store.events(run.id, types=("tool.started",))]
    errors = [e for e in store.events(run.id, types=("tool.result",)) if e.get("is_error")]
    print(f"  worker in 'thorough' mode: {len(calls)} tool calls ({', '.join(calls[:3])}, ... {calls[-1]}), "
          f"{len(store.events(run.id, types=('model.response',)))} model turns")
    print(f"  warning delivered as a tool error: {errors[0]['content'][:110] if errors else '-'}...")
    print(f"  outcome: {'completed' if result else 'KILLED'} -> run status {run.status}: {run.error}")
    print(f"  desk saw {desk.calls['find_engineer_slots']} find_engineer_slots calls from one unit that needed 1")

    step(2, "Swarm budget: pause dispatch when the LEDGER says the cap is reached")
    for label, gated in (("naive gate: spent < cap", False), ("admission control: spent + estimate <= cap", True)):
        desk = d4.RecallDesk()
        queue = d4.WorkQueue(d4.fresh_db(f"lab03_budget_{int(gated)}.db"))
        budget = SwarmBudget(cap_usd=0.20)
        for u in d4.units():
            queue.enqueue(f"unit:{u['serial_number']}", "unit_plan", {"serials": [u["serial_number"]]},
                          priority=d4.PRIORITY[u["risk_class"]])
        costs: list[float] = []
        stopped, done = None, 0
        while True:
            estimate = max(costs) if costs else 0.04          # the most expensive unit so far (a prior before the first)
            if (budget.admits(estimate) if gated else not budget.exhausted()) is False:
                stopped = f"next unit needs ~${estimate:.4f}, ${budget.cap - budget.spent:.4f} left" if gated else \
                    f"${budget.spent:.4f} spent >= ${budget.cap:.2f}"
                break
            task = queue.claim("dispatcher", lease_s=30)
            if task is None:
                break
            before = budget.spent
            r = d4.run_unit_worker(client, store, desk, task.payload["serials"], run_id=f"unit:budget{int(gated)}:{task.task_id}",
                                   worker="dispatcher")
            queue.complete(task.task_id, "dispatcher", {"plans": r.plans})
            costs.append(budget.spent - before)
            done += 1
        over = budget.spent - budget.cap
        print(f"  {label:<44} {done} units done, ${budget.spent:.4f} spent "
              f"({'over the cap by $' + format(over, '.4f') if over > 0 else 'within the cap'}); stopped: {stopped}; "
              f"queue={queue.stats()}")
    print(wrap("Campaign rule 'model spend reaches the cap: pause and report' - the queued tasks stay queued; a human raises "
               "the cap or trims the scope, then the same dispatcher resumes from the queue. The naive gate overshoots by "
               "up to one task per concurrent worker; admission control spends the estimate before the task starts. (The "
               "second pass is cheaper per unit because it reads the worker prefix the first pass cached.)", "  "))

    step(3, "Circuit breaker on find_engineer_slots during a scheduling outage")
    outage = {"active": True}

    def flaky(tool_input: dict) -> None:
        if outage["active"]:
            raise d4.ToolFailure("scheduling service timeout (simulated outage)")

    desk = d4.RecallDesk(faults={"find_engineer_slots": flaky})
    breaker = CircuitBreaker("find_engineer_slots", failure_threshold=3, window=5, cooldown_s=0.2)
    rows = []
    for i, serial in enumerate(d4.serials()[:7], 1):
        if i == 6:
            outage["active"] = False
            time.sleep(breaker.cooldown + 0.05)          # the outage ends and the cooldown elapses
        before = desk.calls["find_engineer_slots"]
        r = run_guarded(client, store, desk, serial, mode="normal", breaker=breaker, run_id=f"unit:cb:{serial}")
        plan = r.plans[0] if r and r.plans else {}
        rows.append([i, serial, desk.calls["find_engineer_slots"] - before, breaker.state, plan.get("status", "-")])
    print(d4.table(rows, ["#", "unit", "desk calls", "breaker after", "plan status"]))
    print(f"  transitions: {' | '.join(breaker.transitions)}")
    print("  Units 4-5 never reached the desk (fail fast: circuit OPEN); unit 6 was the half-open probe that closed it.")

    step(4, "Loop detection: an agent that repeats the same call")
    desk = d4.RecallDesk()
    detector = LoopDetector(soft=3, hard=5)
    result = run_guarded(client, store, desk, "KP250-2608-0002", mode="loop", detector=detector, run_id="unit:loop:KP250-2608-0002")
    run = store.get("unit:loop:KP250-2608-0002")
    seq = [f"{e['name']}({e['input'].get('serial', '')})" for e in store.events(run.id, types=("tool.started",))]
    print(f"  calls: {' -> '.join(seq)}")
    print(f"  outcome: {'completed' if result else 'KILLED'} -> {run.status}: {run.error}")
    print(f"  ping-pong between agents (message-level): ", end="")
    verdict = None
    for sender, receiver, payload in [("coordinator", "worker-1", {"ask": "clarify slot"}), ("worker-1", "coordinator", {"ask": "which unit?"}),
                                      ("coordinator", "worker-1", {"ask": "clarify slot"}), ("worker-1", "coordinator", {"ask": "which unit?"})]:
        verdict = detector.observe_message(sender, receiver, payload) or verdict
    print(verdict)

    step(5, "Backpressure: a token-rate window, and a 429 that survives the SDK's retries")
    clock = FakeClock()
    window = RateWindow(limit_tokens=40_000, window_s=60.0, clock=clock)
    per_unit = 9_000                                     # about what one unit run read in lab 01
    rows = []
    for serial in d4.serials():
        waited = window.admit(per_unit)
        rows.append([f"t={clock():5.0f}s", serial, f"waited {waited:.0f}s" if waited else "admitted"])
    print(d4.table(rows, ["time", "unit", "admission"]))
    print(f"  40,000 tokens/min with ~{per_unit:,} tokens per unit: at most 4 units per minute, whatever the worker count.")
    mock_api().inject_faults(429, 429, 429)
    try:
        client.messages.create(model=MODEL, max_tokens=50, messages=[{"role": "user", "content": "ping"}])
        print("  the call succeeded after retries")
    except anthropic.RateLimitError as exc:
        print(f"  RateLimitError after the SDK's {client.max_retries} retries ({exc.status_code}): treat it as backpressure - "
              "halve the in-flight workers, re-queue the task (its lease expires), do not spin.")


if __name__ == "__main__":
    main()
