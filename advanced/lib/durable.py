"""A durable runtime for agent runs: event-sourced state, checkpoints, resumption, idempotent effects, leases.

Why this exists.  An agent loop is a sequence of model calls and tool calls that can take minutes or days
(approvals), on a process that can crash, be redeployed, or run twice by mistake.  Holding the loop's state
only in Python variables means a crash loses the run and a retry repeats side effects.  The pattern that
fixes this is the same one used for workflows and payments: **write every step to an append-only log
before acting on it**, make every external effect **idempotent**, and let any worker **resume** a run from
its log.  This module is the smallest version of that pattern that is still production-shaped; Day 1 of
the advanced course builds it up step by step, Day 4 and the capstone run on it.

    store = RunStore(".runs/durable.db")
    run = store.create("support", input={"email": "..."}, tags={"customer": "C-1005"})
    runtime = DurableRunner(store, client, model=MODEL, system=SYSTEM, tools=TOOLS, execute=run_tool)
    outcome = runtime.run(run.id)          # crash-safe: call it again after a crash and it resumes

Guarantees (each is tested in tests/test_advanced_durable.py):
  * every model response and every tool result is appended to the log before the next step;
  * a run resumed from the log never repeats a completed tool call (results are replayed), and a tool call
    with an idempotency key is executed at most once even if the log says "started" but not "finished";
  * two workers cannot run the same run at once (a lease with heartbeats; a stale lease can be taken over,
    and a worker that loses its lease stops instead of writing on);
  * an approval pauses the run durably; `decide()` from any process settles it, and the next `run()` (from
    any worker) answers the gated tool call with the decision instead of asking again;
  * the messages array is append-only across a resume, so prompt caches and preserved thinking stay valid;
  * a model call the API rejects (4xx) is recorded and fails the run instead of leaving it "running" forever.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, kind TEXT, status TEXT, input TEXT, tags TEXT, created_at TEXT, updated_at TEXT,
    lease_owner TEXT, lease_until REAL, result TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, type TEXT, payload TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS effects (
    key TEXT PRIMARY KEY, run_id TEXT, tool TEXT, status TEXT, result TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY, run_id TEXT, action TEXT, status TEXT, decided_by TEXT, note TEXT,
    requested_at TEXT, decided_at TEXT);
"""

STATUSES = ("pending", "running", "waiting_approval", "completed", "failed", "cancelled")
APPROVAL_OUTCOMES = ("approved", "rejected", "expired", "cancelled")
NON_RETRYABLE = (400, 401, 403, 404, 413, 422)       # HTTP statuses that a retry cannot fix


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class RunStore:
    """SQLite-backed store for runs, their event logs, effects and approvals. Safe for several processes."""

    def __init__(self, path: str | os.PathLike = ":memory:") -> None:
        self.path = str(path)
        self._local = threading.local()
        self._memory_conn: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_conn.executescript(SCHEMA)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ runs
    def create(self, kind: str, *, input: dict | None = None, tags: dict | None = None, run_id: str | None = None) -> "Run":
        """Create a run; creating an existing run_id again returns it unchanged (idempotent)."""
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        now = _now()
        cur = self._connect().execute(
            "INSERT OR IGNORE INTO runs (run_id, kind, status, input, tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, kind, "pending", json.dumps(input or {}), json.dumps(tags or {}), now, now))
        if cur.rowcount == 1:
            self.append(run_id, "run.created", {"kind": kind, "input": input or {}})
        return self.get(run_id)

    def get(self, run_id: str) -> "Run":
        row = self._connect().execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return Run.from_row(row)

    def list(self, *, status: str | None = None, kind: str | None = None) -> list["Run"]:
        sql, args = "SELECT * FROM runs WHERE 1=1", []
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        return [Run.from_row(r) for r in self._connect().execute(sql + " ORDER BY created_at", args)]

    def set_status(self, run_id: str, status: str, *, result: Any = None, error: str | None = None) -> None:
        assert status in STATUSES, status
        self._connect().execute("UPDATE runs SET status = ?, result = COALESCE(?, result), error = ?, updated_at = ? "
                                "WHERE run_id = ?", (status, json.dumps(result) if result is not None else None, error,
                                                     _now(), run_id))
        self.append(run_id, "run.status", {"status": status, "error": error})

    # ------------------------------------------------------------------ the log
    def append(self, run_id: str, type: str, payload: dict | None = None) -> int:
        cur = self._connect().execute("INSERT INTO events (run_id, type, payload, at) VALUES (?,?,?,?)",
                                      (run_id, type, json.dumps(payload or {}, default=str), _now()))
        return int(cur.lastrowid)

    def events(self, run_id: str, *, types: tuple[str, ...] | None = None) -> list[dict]:
        rows = self._connect().execute("SELECT seq, type, payload, at FROM events WHERE run_id = ? ORDER BY seq",
                                       (run_id,)).fetchall()
        out = [{"seq": r["seq"], "type": r["type"], "at": r["at"], **json.loads(r["payload"])} for r in rows]
        return [e for e in out if types is None or e["type"] in types]

    # ------------------------------------------------------------------ leases
    def acquire(self, run_id: str, owner: str, ttl_s: float = 30.0) -> bool:
        """Take the run's lease if it is free or expired. Returns False when another live worker holds it."""
        now = time.time()
        cur = self._connect().execute(
            "UPDATE runs SET lease_owner = ?, lease_until = ? WHERE run_id = ? AND (lease_owner IS NULL OR "
            "lease_owner = ? OR lease_until < ?)", (owner, now + ttl_s, run_id, owner, now))
        return cur.rowcount == 1

    def heartbeat(self, run_id: str, owner: str, ttl_s: float = 30.0) -> bool:
        """Extend the lease. Returns False when this worker no longer holds it (another worker took over)."""
        cur = self._connect().execute("UPDATE runs SET lease_until = ? WHERE run_id = ? AND lease_owner = ?",
                                      (time.time() + ttl_s, run_id, owner))
        return cur.rowcount == 1

    def release(self, run_id: str, owner: str) -> None:
        self._connect().execute("UPDATE runs SET lease_owner = NULL, lease_until = NULL WHERE run_id = ? AND lease_owner = ?",
                                (run_id, owner))

    def stuck(self, *, older_than_s: float) -> list["Run"]:
        """Runs marked running whose lease expired more than `older_than_s` ago (a worker died mid-run)."""
        cutoff = time.time() - older_than_s
        rows = self._connect().execute("SELECT * FROM runs WHERE status = 'running' AND (lease_until IS NULL OR lease_until < ?)",
                                       (cutoff,)).fetchall()
        return [Run.from_row(r) for r in rows]

    # ------------------------------------------------------------------ effects (idempotency)
    def effect_begin(self, key: str, run_id: str, tool: str) -> dict | None:
        """Claim an effect. Returns the stored result if it already completed, {"status": "started"} if a
        previous attempt started but never finished (the caller decides: check the system of record, or
        treat as failed), or None when this is the first attempt. Two racing claimants get one None."""
        conn = self._connect()
        cur = conn.execute("INSERT OR IGNORE INTO effects (key, run_id, tool, status, at) VALUES (?,?,?,?,?)",
                           (key, run_id, tool, "started", _now()))
        if cur.rowcount == 1:
            return None
        row = conn.execute("SELECT status, result FROM effects WHERE key = ?", (key,)).fetchone()
        return {"status": row["status"], "result": json.loads(row["result"]) if row["result"] else None}

    def effect_finish(self, key: str, result: Any) -> None:
        self._connect().execute("UPDATE effects SET status = 'done', result = ?, at = ? WHERE key = ?",
                                (json.dumps(result, default=str), _now(), key))

    def effect_reset(self, key: str) -> None:
        self._connect().execute("DELETE FROM effects WHERE key = ?", (key,))

    # ------------------------------------------------------------------ approvals
    def request_approval(self, run_id: str, action: dict) -> str:
        approval_id = f"apr_{uuid.uuid4().hex[:10]}"
        self._connect().execute("INSERT INTO approvals (approval_id, run_id, action, status, requested_at) VALUES (?,?,?,?,?)",
                                (approval_id, run_id, json.dumps(action, default=str), "pending", _now()))
        self.append(run_id, "approval.requested", {"approval_id": approval_id, "action": action})
        self.set_status(run_id, "waiting_approval")
        return approval_id

    def decide(self, approval_id: str, *, approved: bool, by: str, note: str = "") -> "Run":
        """Settle a pending approval. Deciding twice is a no-op: the first decision stands, whoever was faster."""
        return self._settle(approval_id, "approved" if approved else "rejected", by=by, note=note)

    def expire(self, approval_id: str, *, by: str = "sweeper", note: str = "no decision before the deadline") -> "Run":
        """Settle a pending approval as expired (a sweeper's job); the run resumes and the tool call gets a refusal."""
        return self._settle(approval_id, "expired", by=by, note=note)

    def _settle(self, approval_id: str, status: str, *, by: str, note: str) -> "Run":
        assert status in APPROVAL_OUTCOMES, status
        conn = self._connect()
        row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        cur = conn.execute("UPDATE approvals SET status = ?, decided_by = ?, note = ?, decided_at = ? "
                           "WHERE approval_id = ? AND status = 'pending'", (status, by, note, _now(), approval_id))
        if cur.rowcount == 0:                              # already settled by someone else
            return self.get(row["run_id"])
        self.append(row["run_id"], "approval.decided", {"approval_id": approval_id, "status": status, "by": by, "note": note})
        self.set_status(row["run_id"], "pending")          # back in the queue: any worker may resume it
        return self.get(row["run_id"])

    def approvals(self, run_id: str) -> list[dict]:
        rows = self._connect().execute("SELECT * FROM approvals WHERE run_id = ? ORDER BY requested_at", (run_id,)).fetchall()
        return [{**dict(r), "action": json.loads(r["action"])} for r in rows]


@dataclass
class Run:
    id: str
    kind: str
    status: str
    input: dict
    tags: dict
    created_at: str
    updated_at: str
    lease_owner: str | None = None
    lease_until: float | None = None
    result: Any = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        return cls(id=row["run_id"], kind=row["kind"], status=row["status"], input=json.loads(row["input"] or "{}"),
                   tags=json.loads(row["tags"] or "{}"), created_at=row["created_at"], updated_at=row["updated_at"],
                   lease_owner=row["lease_owner"], lease_until=row["lease_until"],
                   result=json.loads(row["result"]) if row["result"] else None, error=row["error"])


# ============================================================================== the runner
class ApprovalRequired(Exception):
    """Raised by a tool executor to pause the run until a person decides."""

    def __init__(self, action: dict) -> None:
        super().__init__(action.get("summary", "approval required"))
        self.action = action


class LeaseLost(RuntimeError):
    """Raised when a worker's heartbeat finds that another worker took the run over: stop, do not write on."""


class Crash(BaseException):
    """Used by the labs to simulate a worker dying at a chosen point.

    It derives from BaseException on purpose: a dying process is not a tool error, so the runner's
    `except Exception` (which turns tool failures into tool_result errors) must not catch it, and an
    effect that was in flight must stay recorded as "started" - exactly what a real crash leaves behind.
    """


@dataclass
class Outcome:
    run_id: str
    status: str
    reply: str = ""
    turns: int = 0
    replayed_tools: int = 0
    executed_tools: int = 0
    approval_id: str | None = None
    messages: list[dict] = field(default_factory=list)


ToolExecutor = Callable[[str, dict, "ToolContext"], Any]


def _tool_result(tool_use_id: str, result: dict) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": result["content"]}
    if result.get("is_error"):
        block["is_error"] = True
    return block


def _text_of(content: list[dict]) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


@dataclass
class ToolContext:
    run_id: str
    store: RunStore
    tool_use_id: str
    idempotency_key: str

    def effect(self, key: str | None = None):
        """Context manager for an at-most-once effect: `with ctx.effect() as done: if done is not None: return done`."""
        return _Effect(self.store, key or self.idempotency_key, self.run_id)


class _Effect:
    """One at-most-once effect, keyed by an idempotency key.

        with ctx.effect() as eff:
            if eff.done:                     # a previous attempt finished: replay its result
                return eff.stored
            if eff.in_flight:                # a previous attempt started and the worker died: outcome unknown
                found = system_of_record.find(ctx.idempotency_key)   # the key MUST have gone downstream
                if found: return eff.commit(found)
            return eff.commit(system_of_record.do(..., idempotency_key=ctx.idempotency_key))

    `done` and `in_flight` are the two states a crash can leave behind; the local table alone cannot tell
    whether an in-flight effect reached the downstream system, which is why the idempotency key must be
    passed to that system and looked up there.
    """

    def __init__(self, store: RunStore, key: str, run_id: str) -> None:
        self.store, self.key, self.run_id = store, key, run_id
        self.previous: dict | None = None
        self.result: Any = None
        self.committed = False

    def __enter__(self) -> "_Effect":
        self.previous = self.store.effect_begin(self.key, self.run_id, "")
        return self

    @property
    def done(self) -> bool:
        return bool(self.previous and self.previous["status"] == "done")

    @property
    def in_flight(self) -> bool:
        return bool(self.previous and self.previous["status"] == "started")

    @property
    def stored(self) -> Any:
        return self.previous["result"] if self.done else None

    def commit(self, result: Any) -> Any:
        self.result = result
        self.committed = True
        self.store.effect_finish(self.key, result)
        return result

    def __exit__(self, exc_type, exc, tb) -> None:
        # An ordinary failure on the first attempt may be retried, so forget the claim.  A committed effect is
        # never forgotten, and a crash (BaseException) leaves the "started" record in place: the next worker
        # must treat the outcome as unknown.
        if exc_type is not None and issubclass(exc_type, Exception) and self.previous is None and not self.committed:
            self.store.effect_reset(self.key)


class DurableRunner:
    """A model/tool loop whose every step is logged and which can be resumed by any worker."""

    def __init__(self, store: RunStore, client: Any, *, model: str, system: str, tools: list[dict],
                 execute: ToolExecutor, max_turns: int = 12, max_tokens: int = 8000, worker: str | None = None,
                 lease_ttl_s: float = 30.0, create_kwargs: dict | None = None,
                 crash_at: tuple[str, int] | None = None) -> None:
        self.store, self.client = store, client
        self.model, self.system, self.tools, self.execute = model, system, tools, execute
        self.max_turns, self.max_tokens = max_turns, max_tokens
        self.worker = worker or f"{socket.gethostname()}:{os.getpid()}"
        self.lease_ttl_s = lease_ttl_s
        self.create_kwargs = create_kwargs or {}
        self.crash_at = crash_at                           # ("after_model" | "before_tool" | "after_tool", turn)

    # ------------------------------------------------------------------ state reconstruction
    def rebuild(self, run_id: str) -> tuple[list[dict], dict[str, dict], int, int]:
        """Replay the log into (messages, tool results by tool_use_id, turns so far, results replayed).

        The messages array is rebuilt exactly as the crashed worker had it: assistant turns come from
        `model.response` events, and every tool round that the log shows as fully answered is closed with the
        same user message of tool_result blocks the worker sent.  Append-only, byte-for-byte."""
        run = self.store.get(run_id)
        messages: list[dict] = [{"role": "user", "content": run.input.get("message", "")}]
        results: dict[str, dict] = {}
        turns = replayed = 0
        for event in self.store.events(run_id):
            if event["type"] == "model.response":
                replayed += self._close_tool_round(messages, results)
                messages.append({"role": "assistant", "content": event["content"]})
                turns += 1
            elif event["type"] == "tool.result":
                results[event["tool_use_id"]] = {"content": event["content"], "is_error": bool(event.get("is_error"))}
            elif event["type"] == "user.message":
                replayed += self._close_tool_round(messages, results)
                messages.append({"role": "user", "content": event["content"]})
        replayed += self._close_tool_round(messages, results)
        return messages, results, turns, replayed

    @staticmethod
    def _close_tool_round(messages: list[dict], results: dict[str, dict]) -> int:
        """If the last message is an assistant turn whose tool calls all have logged results, append the
        tool_result message for them. Returns how many results were replayed."""
        if not messages or messages[-1]["role"] != "assistant":
            return 0
        pending = [b for b in messages[-1]["content"] if b.get("type") == "tool_use"]
        if not pending or any(b["id"] not in results for b in pending):
            return 0
        messages.append({"role": "user", "content": [_tool_result(b["id"], results[b["id"]]) for b in pending]})
        return len(pending)

    # ------------------------------------------------------------------ the loop
    def run(self, run_id: str) -> Outcome:
        if not self.store.acquire(run_id, self.worker, self.lease_ttl_s):
            raise RuntimeError(f"run {run_id} is leased by another worker")
        try:
            return self._run_leased(run_id)
        finally:
            self.store.release(run_id, self.worker)

    def _run_leased(self, run_id: str) -> Outcome:
        run = self.store.get(run_id)
        if run.status in ("completed", "failed", "cancelled"):
            return Outcome(run_id, run.status, reply=(run.result or {}).get("reply", ""))
        if run.status == "waiting_approval":
            pending = [a for a in self.store.approvals(run_id) if a["status"] == "pending"]
            if pending:
                return Outcome(run_id, "waiting_approval", approval_id=pending[0]["approval_id"])
        self.store.set_status(run_id, "running")
        self._answer_decided(run_id)                       # a decision made while the run was parked
        messages, results, turns, replayed = self.rebuild(run_id)
        outcome = Outcome(run_id, "running", turns=turns, replayed_tools=replayed)

        if messages and messages[-1]["role"] == "assistant":
            pending = [b for b in messages[-1]["content"] if b.get("type") == "tool_use"]
            if not pending:                                # the answer was logged; the worker died before recording it
                return self._complete(run_id, outcome, messages)
            # Finish the tool round the last assistant turn started: replay the results the log holds, execute the rest.
            answers, paused = self._answer_tools(run_id, pending, results, outcome, turns)
            if paused is not None:
                return paused
            messages.append({"role": "user", "content": answers})

        while outcome.turns < self.max_turns:
            if not self.store.heartbeat(run_id, self.worker, self.lease_ttl_s):
                raise LeaseLost(f"run {run_id}: lease taken over by another worker; stopping")
            turn = outcome.turns + 1
            try:
                response = self.client.beta.messages.create(model=self.model, max_tokens=self.max_tokens, system=self.system,
                                                            tools=self.tools, messages=messages, **self.create_kwargs)
            except Exception as exc:                        # a rejected request will not succeed on retry: record and fail
                status = getattr(exc, "status_code", None)
                if status in NON_RETRYABLE:
                    self.store.append(run_id, "model.error", {"turn": turn, "status": status, "message": str(exc)[:500]})
                    self.store.set_status(run_id, "failed", error=f"model call rejected ({status}): {str(exc)[:300]}")
                raise
            content = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in response.content]
            self.store.append(run_id, "model.response", {"turn": turn, "content": content,
                                                         "stop_reason": response.stop_reason,
                                                         "usage": response.usage.model_dump()})
            self._maybe_crash("after_model", turn)
            messages.append({"role": "assistant", "content": content})
            outcome.turns = turn
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                return self._complete(run_id, outcome, messages)
            answers, paused = self._answer_tools(run_id, tool_uses, results, outcome, turn)
            if paused is not None:
                return paused
            messages.append({"role": "user", "content": answers})
        outcome.status = "failed"
        self.store.set_status(run_id, "failed", error=f"turn limit {self.max_turns} reached")
        return outcome

    def _complete(self, run_id: str, outcome: Outcome, messages: list[dict]) -> Outcome:
        outcome.reply = _text_of(messages[-1]["content"])
        outcome.status = "completed"
        outcome.messages = messages
        self.store.set_status(run_id, "completed", result={"reply": outcome.reply, "turns": outcome.turns})
        return outcome

    def _answer_tools(self, run_id: str, tool_uses: list[dict], results: dict[str, dict], outcome: Outcome,
                      turn: int) -> tuple[list[dict], Outcome | None]:
        answers: list[dict] = []
        for block in tool_uses:
            tid = block["id"]
            if tid in results:                                     # already done before the crash: replay
                answers.append(_tool_result(tid, results[tid]))
                outcome.replayed_tools += 1
                continue
            self._maybe_crash("before_tool", turn)
            ctx = ToolContext(run_id=run_id, store=self.store, tool_use_id=tid, idempotency_key=f"{run_id}:{tid}")
            self.store.append(run_id, "tool.started", {"tool_use_id": tid, "name": block["name"], "input": block["input"]})
            try:
                result = self.execute(block["name"], block["input"], ctx)
            except ApprovalRequired as exc:
                approval_id = self.store.request_approval(run_id, {**exc.action, "tool_use_id": tid, "name": block["name"],
                                                                   "input": block["input"]})
                outcome.status = "waiting_approval"
                outcome.approval_id = approval_id
                return answers, outcome
            except Exception as exc:                                # a tool error is a result, not a crash
                result = {"error": f"{type(exc).__name__}: {exc}"}
                is_error = True
            else:
                is_error = isinstance(result, dict) and "error" in result
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            self.store.append(run_id, "tool.result", {"tool_use_id": tid, "name": block["name"], "content": text,
                                                      "is_error": is_error})
            results[tid] = {"content": text, "is_error": is_error}
            outcome.executed_tools += 1
            self._maybe_crash("after_tool", turn)
            answers.append(_tool_result(tid, results[tid]))
        return answers, None

    def _maybe_crash(self, point: str, turn: int) -> None:
        if self.crash_at == (point, turn):
            self.crash_at = None
            raise Crash(f"simulated crash {point} at turn {turn}")

    # ------------------------------------------------------------------ resuming after an approval
    def _answer_decided(self, run_id: str) -> None:
        """Answer every settled approval whose gated tool call has no result yet: execute it once if approved
        (the executor sees `_approved: True`), else log a refusal the model can explain to the user."""
        answered = {e["tool_use_id"] for e in self.store.events(run_id, types=("tool.result",))}
        for approval in self.store.approvals(run_id):
            action = approval["action"]
            tid = action.get("tool_use_id")
            if approval["status"] not in APPROVAL_OUTCOMES or not tid or tid in answered:
                continue
            if approval["status"] == "approved":
                ctx = ToolContext(run_id=run_id, store=self.store, tool_use_id=tid, idempotency_key=f"{run_id}:{tid}:approved")
                try:
                    result = self.execute(action["name"], {**action["input"], "_approved": True}, ctx)
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
            else:
                verdict = {"rejected": "Declined", "expired": "Not decided in time", "cancelled": "Cancelled"}[approval["status"]]
                result = {"error": f"{verdict} by {approval['decided_by']}: {approval['note'] or 'not approved'}. "
                          "Tell the customer the request could not be approved."}
            is_error = isinstance(result, dict) and "error" in result
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            self.store.append(run_id, "tool.result", {"tool_use_id": tid, "name": action["name"], "content": text, "is_error": is_error})
            answered.add(tid)

    def resume_after_decision(self, run_id: str) -> Outcome:
        """Resume a run whose approval was decided. `run()` does the same; this name keeps the intent visible."""
        return self.run(run_id)
