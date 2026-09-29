"""Shared helpers for the Day 1 labs, starters and solutions: tickets, runner set-up, executors, log printing.

Not a script.  Labs import it with `import _day1 as d1` after putting their own directory on sys.path
(`sys.path.insert(0, str(Path(__file__).resolve().parent))`), so they run from any working directory; the
exercise starters and solutions put `../labs` on the path instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from advanced.lib.durable import ApprovalRequired, Crash, DurableRunner, Outcome, RunStore
from kestrel import policy
from kestrel.support_agent import SYSTEM_PROMPT
from kestrel.support_tools import TOOLS, SupportDesk, ToolError
from labkit import LEDGER, MODEL, runs_dir
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


def store_path(name: str) -> Path:
    return runs_dir("advanced", "day1") / f"{name}.db"


def fresh_store(name: str) -> RunStore:
    """A RunStore on disk under .runs/advanced/day1/<name>.db, wiped first so every lab run starts identical."""
    path = store_path(name)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    return RunStore(path)


# ---------------------------------------------------------------------------- the runner set-up
def cached_system(text: str = SYSTEM_PROMPT) -> list[dict]:
    """The system prompt exactly as kestrel.support_agent sends it: one block with a cache breakpoint."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def create_kwargs(model: str = MODEL) -> dict:
    """Automatic caching of the growing tail plus server-side refusal fallbacks - the support agent's request
    shape, so a durable run and the in-memory loop send the same requests."""
    return {"cache_control": {"type": "ephemeral"}, **fallback_kwargs(model)}


def support_runner(store: RunStore, client: Any, *, execute: Callable, worker: str, system: str = SYSTEM_PROMPT,
                   tools: list[dict] | None = None, runner_cls: type = DurableRunner, model: str = MODEL,
                   kwargs: dict | None = None, **kw) -> DurableRunner:
    """A DurableRunner configured like kestrel.support_agent: cached system prompt, the support tools, the same
    request parameters. `kwargs` replaces the default create_kwargs (e.g. to add thinking controls)."""
    return runner_cls(store, client, model=model, system=cached_system(system), tools=tools or TOOLS, execute=execute,
                      max_tokens=8000, worker=worker, create_kwargs=kwargs if kwargs is not None else create_kwargs(model),
                      **kw)


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


def model_calls() -> int:
    """Messages API calls made so far in this process - labkit's metering counts them in mock and live mode alike."""
    return LEDGER.total_calls


class RecordingClient:
    """The client a runner uses, plus a copy of every request it sends (mock and live alike). DurableRunner only
    calls `client.beta.messages.create`, so that is all this wrapper needs to provide."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.requests: list[dict] = []
        self.beta = self

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return self._client.beta.messages.create(**kwargs)


def api_error(exc: Exception) -> str:
    """The API's own error message from an SDK exception (without the SDK's 'Error code: 400 - {...}' wrapper)."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return str((body.get("error") or {}).get("message") or body)
    return str(exc)


# ---------------------------------------------------------------------------- the refund approval gate (lab 05, ex09, ex10)
APPROVALS_SYSTEM = SYSTEM_PROMPT + "\n<adv_day1_approvals>\n"       # the marker the day's stand-in policy answers to
APPROVALS_EMAIL = "travis.greer@midlandoil.example"
APPROVALS_TICKET = "T-1207"
APPROVALS_MESSAGE = ("Subject: Re: Refund for returned KP-250-X (T-1207)\n\nFollowing up on RMA-7001: the figure of "
                     "$9,188.50 is confirmed on our side, so please go ahead and process the refund.\n\nTravis Greer, "
                     "Midland Oil Services")


def approvals_input() -> dict:
    return {"message": APPROVALS_MESSAGE, "requester_email": APPROVALS_EMAIL, "ticket_id": APPROVALS_TICKET}


def find_refund(db, rma_id: str) -> dict | None:
    """The system of record's answer to 'did this refund happen?' - looked up by its natural key."""
    row = db.execute("SELECT refund_id, amount_usd, approved_by, status FROM refunds WHERE rma_id = ?", (rma_id,)).fetchone()
    if row is None:
        return None
    return {"refund_id": row["refund_id"], "amount_usd": row["amount_usd"], "status": row["status"],
            "approved_by": row["approved_by"], "note": "Refunds reach the original payment method within 10 business days."}


def approved_refund(db, rma: dict, amount: float, reason: str, approved_by: str) -> dict:
    """The refund path a person's approval unlocks. The agent's own limit stays in the tool; this path records
    who approved (dual control - Day 5 makes it a capability). The refund id is the natural key RF-<rma>."""
    refund_id = f"RF-{rma['rma_id'][4:]}"
    db.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?,?,?)",
               (refund_id, rma["order_id"], rma["rma_id"], amount, reason[:200], approved_by, "issued",
                "2026-09-15T09:00:00Z"))
    db.execute("UPDATE rmas SET status = 'refunded' WHERE rma_id = ?", (rma["rma_id"],))
    db.execute("INSERT INTO audit_log (ts, actor, action, target, details) VALUES (?,?,?,?,?)",
               ("2026-09-15T09:00:00Z", f"support-agent:approved-by:{approved_by}", "issue_refund", refund_id,
                json.dumps({"rma_id": rma["rma_id"], "amount_usd": amount})))
    db.commit()
    return find_refund(db, rma["rma_id"])


def gated_executor(desk: SupportDesk, db) -> Callable:
    """The support tools, except that a refund above the agent's limit raises ApprovalRequired instead of the
    tool's refusal. When the run resumes with an approval, the refund is an at-most-once effect: done -> replay,
    in flight -> ask the refunds table (the system of record) before issuing it."""
    desk_tools = desk_executor(desk)

    def execute(name: str, tool_input: dict, ctx) -> Any:
        if name != "issue_refund":
            return desk_tools(name, tool_input, ctx)
        try:
            rma = desk.get_rma(tool_input["rma_id"])
        except ToolError as exc:
            return {"error": str(exc)}
        due = rma.get("refund_due_usd")
        if rma.get("status") != "received" or due is None:
            return desk_tools(name, tool_input, ctx)             # the tool's own message explains the state
        approver = policy.refund_approver(due)
        if approver == "agent":
            return desk_tools(name, tool_input, ctx)             # within the agent's limit: the tool issues it
        if not tool_input.get("_approved"):
            raise ApprovalRequired({"summary": f"Refund of ${due:,.2f} on {rma['rma_id']} needs the "
                                               f"{approver.replace('_', ' ')}",
                                    "amount_usd": due, "queue": approver, "rma_id": rma["rma_id"],
                                    "requester_email": desk.requester_email})
        decided = [a for a in ctx.store.approvals(ctx.run_id) if a["status"] == "approved"]
        approved_by = decided[-1]["decided_by"] if decided else "unknown"
        with ctx.effect() as eff:                                # key: <run>:<tool_use>:approved
            if eff.done:
                return eff.stored
            if eff.in_flight:
                found = find_refund(db, rma["rma_id"])
                if found:
                    return eff.commit(found)
            return eff.commit(approved_refund(db, rma, round(float(tool_input["amount_usd"]), 2),
                                              tool_input["reason"], approved_by))

    return execute


def refunds(db) -> list[dict]:
    return [dict(r) for r in db.execute("SELECT refund_id, amount_usd, approved_by, status FROM refunds")]


def decided_but_unanswered(store: RunStore, run_id: str) -> bool:
    """True when a person decided an approval whose tool call has no logged result yet: such a run must be
    resumed with resume_after_decision(), not run() - run() would execute the gated tool again and ask again."""
    answered = {e["tool_use_id"] for e in store.events(run_id, types=("tool.result",))}
    return any(a["status"] in ("approved", "rejected") and a["action"].get("tool_use_id") not in answered
               for a in store.approvals(run_id))


# ---------------------------------------------------------------------------- a long run (exercises 3 and 12)
STOCK_AUDIT_SYSTEM = """\
<adv_day1_stock_audit>
You are Kestrel's inventory assistant. Check every stock location the planner lists with count_stock, one at a \
time, then report how many locations you checked and which ones are at or below their reorder point.
"""
COUNT_STOCK = {
    "name": "count_stock",
    "description": "On-hand, reserved and available units of one SKU in one warehouse, with its reorder point.",
    "strict": True,
    "input_schema": {"type": "object", "additionalProperties": False,
                     "properties": {"sku": {"type": "string"}, "warehouse": {"type": "string"}},
                     "required": ["sku", "warehouse"]},
}


def stock_locations(db, n: int = 40) -> list[tuple[str, str]]:
    return [(r["sku"], r["warehouse"]) for r in
            db.execute("SELECT sku, warehouse FROM inventory ORDER BY sku, warehouse LIMIT ?", (n,))]


def stock_audit_input(db, n: int = 40) -> dict:
    listing = ", ".join(f"{sku}@{wh}" for sku, wh in stock_locations(db, n))
    return {"message": f"Weekly stock audit. Locations to check ({n}): {listing}."}


def stock_executor(db) -> Callable:
    def execute(name: str, tool_input: dict, ctx) -> Any:
        if name != "count_stock":
            return {"error": f"unknown tool {name}"}
        row = db.execute("SELECT on_hand, reserved, reorder_point FROM inventory WHERE sku = ? AND warehouse = ?",
                         (tool_input["sku"], tool_input["warehouse"])).fetchone()
        if row is None:
            return {"error": f"no stock location {tool_input['sku']}@{tool_input['warehouse']}"}
        available = row["on_hand"] - row["reserved"]
        return {"sku": tool_input["sku"], "warehouse": tool_input["warehouse"], "on_hand": row["on_hand"],
                "reserved": row["reserved"], "available": available, "reorder_point": row["reorder_point"],
                "at_or_below_reorder": available <= row["reorder_point"]}

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
    the run is finished from the log.
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
