"""Shared helpers for the Day 1 labs: tickets, the runner set-up, executors, log printing and a killed worker.

Not a script.  Labs import it with `import _day1 as d1` after putting their own directory on sys.path
(`sys.path.insert(0, str(Path(__file__).resolve().parent))`), so they run from any working directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from advanced.lib.durable import Crash, DurableRunner, Outcome, RunStore
from kestrel.support_agent import SYSTEM_PROMPT
from kestrel.support_tools import TOOLS, SupportDesk
from labkit import MODEL, runs_dir
from labkit.data import load_jsonl
from labkit.models import fallback_kwargs
from labkit.pricing import cost_usd

TICKETS: dict[str, dict] = {t["ticket_id"]: t for t in load_jsonl("support", "tickets.jsonl")}


# ---------------------------------------------------------------------------- tickets and runs
def ticket(ticket_id: str) -> dict:
    return TICKETS[ticket_id]


def ticket_message(t: dict) -> str:
    """The email as the support agent receives it (same shape as the first course's Day 2 lab 06)."""
    return f"Subject: {t['subject']}\n\n{t['body']}"


def run_input(t: dict) -> dict:
    """What a run needs to be resumed by ANY worker: the message, the channel-verified sender, the ticket."""
    return {"message": ticket_message(t), "requester_email": t["from_email"], "ticket_id": t["ticket_id"]}


def fresh_store(name: str) -> RunStore:
    """A RunStore on disk under .runs/advanced/day1/<name>.db, wiped first so every lab run starts identical."""
    path = runs_dir("advanced", "day1") / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    return RunStore(path)


# ---------------------------------------------------------------------------- the runner set-up
def cached_system(text: str = SYSTEM_PROMPT) -> list[dict]:
    """The system prompt exactly as kestrel.support_agent sends it: one block with a cache breakpoint."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def create_kwargs() -> dict:
    """Automatic caching of the growing tail plus server-side refusal fallbacks - the support agent's request
    shape, so a durable run and the in-memory loop send byte-identical requests."""
    return {"cache_control": {"type": "ephemeral"}, **fallback_kwargs(MODEL)}


def support_runner(store: RunStore, client: Any, *, execute: Callable, worker: str, system: str = SYSTEM_PROMPT,
                   tools: list[dict] | None = None, runner_cls: type = DurableRunner, **kw) -> DurableRunner:
    return runner_cls(store, client, model=MODEL, system=cached_system(system), tools=tools or TOOLS, execute=execute,
                      max_tokens=8000, worker=worker, create_kwargs=create_kwargs(), **kw)


def desk_executor(desk: SupportDesk) -> Callable:
    """Adapt a SupportDesk to the runner's executor protocol: (name, input, ctx) -> JSON-able result.

    The desk returns (content, is_error) with JSON content; returning the parsed dict lets the runner detect
    errors the same way for every executor (`"error" in result`)."""

    def execute(name: str, tool_input: dict, ctx) -> Any:
        content, is_error = desk.run(name, tool_input)
        try:
            return json.loads(content)
        except ValueError:
            return {"error": content} if is_error else content

    return execute


# ---------------------------------------------------------------------------- printing
def short(value: Any, limit: int = 72) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe(e: dict) -> str:
    """One line per event: what a person on call needs to see, no payload dumps."""
    t = e["type"]
    if t == "run.created":
        return f"kind={e.get('kind')}  input={sorted((e.get('input') or {}).keys())}"
    if t == "run.status":
        return e.get("status", "") + (f"  error={e['error']}" if e.get("error") else "")
    if t == "model.response":
        blocks = [b.get("type") for b in e.get("content", [])]
        calls = [b["name"] for b in e.get("content", []) if b.get("type") == "tool_use"]
        u = e.get("usage") or {}
        return (f"turn {e.get('turn')}  stop={e.get('stop_reason')}  blocks={blocks}  tools={calls}  "
                f"in={u.get('input_tokens', 0)} cache_w={u.get('cache_creation_input_tokens') or 0} "
                f"cache_r={u.get('cache_read_input_tokens') or 0} out={u.get('output_tokens', 0)}")
    if t == "tool.started":
        return f"{e.get('name')} {short(e.get('input', {}), 60)}"
    if t == "tool.result":
        flag = "ERROR" if e.get("is_error") else "ok"
        return f"{e.get('name')} {flag} {len(e.get('content') or '')} chars: {short(e.get('content', ''), 60)}"
    if t == "approval.requested":
        action = e.get("action") or {}
        return f"{action.get('name')} -> {short(action.get('summary', ''), 70)}"
    if t == "approval.decided":
        return f"{e.get('status')} by {e.get('by')}" + (f": {short(e.get('note'), 50)}" if e.get("note") else "")
    if t == "approval.escalated":
        return f"{e.get('from')} -> {e.get('to')} after {float(e.get('after_s', 0)) / 3600:.0f} h"
    if t == "user.message":
        return short(e.get("content", ""), 70)
    return short({k: v for k, v in e.items() if k not in ("seq", "type", "at")}, 80)


def print_log(store: RunStore, run_id: str, *, types: tuple[str, ...] | None = None, since_seq: int = 0) -> None:
    for e in store.events(run_id, types=types):
        if e["seq"] > since_seq:
            print(f"  {e['seq']:>4}  {e['type']:<19} {describe(e)}")


def print_transcript(messages: list[dict], *, max_text: int = 90) -> None:
    for i, m in enumerate(messages):
        content = m["content"]
        if isinstance(content, str):
            print(f"  [{i}] {m['role']:<9} text: {short(content, max_text)}")
            continue
        for b in content:
            kind = b.get("type")
            if kind == "text":
                print(f"  [{i}] {m['role']:<9} text: {short(b.get('text', ''), max_text)}")
            elif kind == "thinking":
                sig = (b.get("signature") or "")[:22]
                print(f"  [{i}] {m['role']:<9} thinking (signature {sig}...)")
            elif kind == "tool_use":
                print(f"  [{i}] {m['role']:<9} tool_use {b['name']}({short(b.get('input', {}), 50)})  id={b['id'][-8:]}")
            elif kind == "tool_result":
                flag = " ERROR" if b.get("is_error") else ""
                print(f"  [{i}] {m['role']:<9} tool_result{flag} for ...{b['tool_use_id'][-8:]}: {short(b.get('content', ''), 60)}")
            else:
                print(f"  [{i}] {m['role']:<9} {kind}")


def normalise(messages: list[dict]) -> list[dict]:
    """Turn SDK content blocks into plain dicts the way the runner logs them (model_dump(exclude_none=True))."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in content]
        out.append({"role": m["role"], "content": content})
    return out


def transcript_bytes(messages: list[dict]) -> str:
    return json.dumps(normalise(messages), sort_keys=True, ensure_ascii=False, default=str)


def tool_calls(store: RunStore, run_id: str) -> list[str]:
    return [e["name"] for e in store.events(run_id, types=("tool.started",))]


def run_cost(store: RunStore, run_id: str, model: str = MODEL) -> float:
    return sum(cost_usd(e["usage"], model) for e in store.events(run_id, types=("model.response",)))


def outcome_line(o: Outcome) -> str:
    return (f"status={o.status} turns={o.turns} replayed_tools={o.replayed_tools} executed_tools={o.executed_tools}"
            + (f" approval={o.approval_id[:4]}..." if o.approval_id else ""))


# ---------------------------------------------------------------------------- a worker that dies for real
class KilledWorker(DurableRunner):
    """A worker whose simulated crash behaves like `kill -9`: the lease is NOT released.

    DurableRunner.run releases the lease in a `finally` clause - what a worker that dies gracefully does.  A
    process killed by the OS, an out-of-memory kill or a node failure never reaches its `finally`: the lease
    stays in the table until it expires, which is the situation `RunStore.stuck()` exists for.
    """

    def run(self, run_id: str) -> Outcome:
        if not self.store.acquire(run_id, self.worker, self.lease_ttl_s):
            raise RuntimeError(f"run {run_id} is leased by another worker")
        try:
            outcome = self._run_leased(run_id)
        except Crash:
            raise                                   # dead: nobody releases the lease
        except BaseException:
            self.store.release(run_id, self.worker)
            raise
        self.store.release(run_id, self.worker)
        return outcome


# ---------------------------------------------------------------------------- finishing a run from its log
def last_response(store: RunStore, run_id: str) -> dict | None:
    responses = store.events(run_id, types=("model.response",))
    return responses[-1] if responses else None


class FinishingRunner(DurableRunner):
    """DurableRunner plus one resume case the shipped runner leaves out: a crash AFTER the final model response
    was logged but BEFORE the run was marked completed.

    Resuming such a run must not call the model again (the conversation already ends with the answer; sending it
    back would be an assistant prefill, which current models reject with a 400).  The answer is in the log, so
    the run is finished from the log.  Lab 02 shows the case; the integrator's fix belongs in advanced/lib/durable.py.
    """

    def run(self, run_id: str) -> Outcome:
        run = self.store.get(run_id)
        final = last_response(self.store, run_id)
        if run.status in ("pending", "running") and final is not None and final["stop_reason"] != "tool_use" \
                and not any(b.get("type") == "tool_use" for b in final["content"]):
            if not self.store.acquire(run_id, self.worker, self.lease_ttl_s):
                raise RuntimeError(f"run {run_id} is leased by another worker")
            try:
                reply = "".join(b.get("text", "") for b in final["content"] if b.get("type") == "text").strip()
                self.store.set_status(run_id, "completed", result={"reply": reply, "turns": final["turn"]})
                messages, _, turns, replayed = self.rebuild(run_id)
                return Outcome(run_id, "completed", reply=reply, turns=turns, replayed_tools=replayed, messages=messages)
            finally:
                self.store.release(run_id, self.worker)
        return super().run(run_id)
