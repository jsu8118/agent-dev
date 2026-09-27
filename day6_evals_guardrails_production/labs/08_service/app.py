"""Kestrel support-agent service (Day 6, lab 08): the reference agent behind a small, production-shaped HTTP API.

Run it (from the repository root):

    uvicorn app:app --app-dir day6_evals_guardrails_production/labs/08_service --port 8000
    curl -s localhost:8000/healthz
    curl -s -X POST localhost:8000/v1/tickets -H 'Content-Type: application/json' -H 'Idempotency-Key: T-1001' \
         -d '{"from_email": "jorge.medina@greenvalley-coop.example", "subject": "Tracking",
              "body": "Tracking number for SO-10303, please."}'
    curl -s localhost:8000/metrics

or in Docker (see the Dockerfile next to this file).  Test it in-process: labs/08_service_smoke_test.py.

What makes it production-shaped:
  * stateless request handling: everything a request needs is in the request, the system of record, or the
    idempotency store - so you can run N workers / replicas behind a load balancer
  * guardrails on every request (sender check, input screener, tool authorization, output checks - lab 04)
  * a concurrency limit per worker with load shedding (503 + Retry-After) instead of an unbounded queue
  * an end-to-end deadline under the 30 s reply promise, tight per-call timeouts, ONE retrying layer (the SDK)
  * Idempotency-Key: a redelivered ticket returns the stored reply instead of running the agent (and its
    write tools) again
  * prompt + model versioning on every response (canary routing by a stable hash of the ticket id)
  * /healthz that never calls the model; /metrics in the Prometheus text format
  * configuration from environment variables; the API key is read by the SDK and never logged
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the day's shared helpers (labs/_*.py)

import _guardrails as g  # noqa: E402
from _evalkit import prompt_version  # noqa: E402
from _reliability import DeadlineExceeded, DeadlineMiddleware  # noqa: E402
from _telemetry import Metrics  # noqa: E402
from kestrel.support_agent import SAFE_FALLBACK_REPLY, SYSTEM_PROMPT  # noqa: E402
from kestrel.support_tools import SupportDesk  # noqa: E402
from labkit import FAST_MODEL, MODEL, get_client, mode, runs_dir  # noqa: E402
from labkit.data import scratch_db  # noqa: E402
from labkit.tracing import Tracer  # noqa: E402

log = logging.getLogger("kestrel.service")


# ------------------------------------------------------------------------------------------ configuration
@dataclass(frozen=True)
class Settings:
    model: str = MODEL
    screen_model: str = FAST_MODEL
    max_concurrency: int = 4             # agent runs in flight per worker process
    queue_timeout_s: float = 2.0         # wait this long for a slot, then shed load (503)
    deadline_s: float = 25.0             # end-to-end budget per ticket; the promise to customers is 30 s
    request_timeout_s: float = 20.0      # per API attempt
    max_retries: int = 1                 # the SDK is the ONE retrying layer
    max_turns: int = 8
    canary_percent: int = 0              # share of tickets routed to the candidate prompt
    candidate_system_prompt: str | None = None
    state_dir: Path = field(default_factory=lambda: runs_dir("day6_service"))   # idempotency store lives here

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        candidate = env.get("KESTREL_CANDIDATE_PROMPT_FILE")
        return cls(model=env.get("KESTREL_MODEL", MODEL), screen_model=env.get("KESTREL_SCREEN_MODEL", FAST_MODEL),
                   max_concurrency=int(env.get("KESTREL_MAX_CONCURRENCY", 4)),
                   queue_timeout_s=float(env.get("KESTREL_QUEUE_TIMEOUT_S", 2.0)),
                   deadline_s=float(env.get("KESTREL_DEADLINE_S", 25.0)),
                   request_timeout_s=float(env.get("KESTREL_REQUEST_TIMEOUT_S", 20.0)),
                   max_retries=int(env.get("KESTREL_MAX_RETRIES", 1)),
                   max_turns=int(env.get("KESTREL_MAX_TURNS", 8)),
                   canary_percent=int(env.get("KESTREL_CANARY_PERCENT", 0)),
                   candidate_system_prompt=Path(candidate).read_text(encoding="utf-8") if candidate else None)


# ------------------------------------------------------------------------------------------ API schema
class TicketIn(BaseModel):
    ticket_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    from_email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=20_000)


class TicketOut(BaseModel):
    ticket_id: str
    disposition: str                 # answered | held_for_review | withheld | blocked_sender | blocked_screen | degraded
    reply: str
    escalation_queues: list[str]
    guardrail_flags: list[str]
    model: str
    prompt_version: str
    trace_id: str
    cost_usd: float
    latency_ms: int


# ------------------------------------------------------------------------------------------ idempotency
class IdempotencyStore:
    """Idempotency-Key -> stored response.  SQLite here; Redis or Postgres when you run more than one host."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._run("CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, status TEXT, response TEXT, "
                  "created_at REAL)")

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(sql, args)

    def begin(self, key: str) -> tuple[str, dict | None]:
        """'new' (you own the key now), 'pending' (another request is running it) or 'done' (+ stored response)."""
        if self._run("INSERT OR IGNORE INTO idempotency VALUES (?, 'pending', NULL, ?)", (key, time.time())).rowcount:
            return "new", None
        with self._lock, closing(sqlite3.connect(self.path)) as conn:
            status, response = conn.execute("SELECT status, response FROM idempotency WHERE key = ?", (key,)).fetchone()
        return status, json.loads(response) if response else None

    def status(self, key: str) -> str | None:
        """Read-only lookup ('pending', 'done' or None) - unlike begin(), it never claims the key."""
        with self._lock, closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT status FROM idempotency WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def finish(self, key: str, response: dict) -> None:
        self._run("UPDATE idempotency SET status = 'done', response = ? WHERE key = ?", (json.dumps(response), key))

    def abandon(self, key: str) -> None:
        self._run("DELETE FROM idempotency WHERE key = ? AND status = 'pending'", (key,))


# ------------------------------------------------------------------------------------------ the app
def create_app(settings: Settings | None = None, client: anthropic.Anthropic | None = None,
               middleware: tuple = ()) -> FastAPI:
    """App factory: tests and workers build their own instance; dependencies are injectable.

    `middleware` is extra SDK middleware for every model call (latency or fault injection in tests and
    chaos drills).  Pass it here rather than on `client`: the per-request metering replaces a client's
    middleware list.
    """
    settings = settings or Settings.from_env()
    client = client or get_client(timeout=settings.request_timeout_s, max_retries=settings.max_retries)
    worker = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    # The course's stand-in for Kestrel's ERP: a private scratch copy per worker.  In production this is the
    # shared system of record, reached over the network - never state inside the worker.
    erp_name = f"day6_service_erp_{worker}.db"
    scratch_db(erp_name).close()
    erp_path = runs_dir("db") / erp_name
    domains = g.customer_domains()
    # Shared by every worker on the host (SQLite locks across processes). A per-worker store would let a
    # redelivery that lands on another worker run the agent - and its write tools - a second time.
    store = IdempotencyStore(settings.state_dir / "idempotency.db")
    metrics = Metrics()
    versions = {"baseline": prompt_version(SYSTEM_PROMPT)}
    if settings.candidate_system_prompt:
        versions["candidate"] = prompt_version(settings.candidate_system_prompt)
    in_flight = {"n": 0}

    def arm_for(ticket_id: str) -> str:
        """Stable bucketing: the same ticket always gets the same prompt (retries stay comparable)."""
        bucket = int(hashlib.sha256(ticket_id.encode()).hexdigest(), 16) % 100
        return "candidate" if settings.candidate_system_prompt and bucket < settings.canary_percent else "baseline"

    def handle(ticket: TicketIn, ticket_id: str) -> TicketOut:
        arm = arm_for(ticket_id)
        system_prompt = settings.candidate_system_prompt if arm == "candidate" else SYSTEM_PROMPT
        tracer = Tracer("support-service")
        conn = sqlite3.connect(erp_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        started = time.perf_counter()
        flags: list[str] = []
        cost = 0.0
        with tracer.span("ticket", ticket=ticket_id, prompt_version=versions[arm], arm=arm):
            try:
                report = g.run_guarded(client, message=f"Subject: {ticket.subject}\n\n{ticket.body}",
                                       sender_email=ticket.from_email, ticket_ref=ticket_id, db=conn, domains=domains,
                                       model=settings.model, screen_model=settings.screen_model,
                                       max_turns=settings.max_turns, system_prompt=system_prompt, tracer=tracer,
                                       extra_middleware=[DeadlineMiddleware(settings.deadline_s), *middleware])
                disposition, reply, cost = report.disposition, report.reply, report.cost_usd
                flags = (report.screen.flags if report.screen else []) + \
                    (["lookalike_sender"] if report.sender.lookalike_of else []) + \
                    [f"tool_blocked:{b['tool']}" for b in report.tool_blocks] + \
                    [f"output:{v.rule}" for v in report.output_violations]
            except (DeadlineExceeded, anthropic.APIError, sqlite3.Error) as exc:
                # Degrade to a human with an honest holding reply; never surface an internal error to a customer.
                SupportDesk(ticket.from_email, db=conn, ticket_ref=ticket_id).escalate_to_human(
                    "support_manager", "P3", f"Automated reply failed ({type(exc).__name__}); please answer manually.")
                disposition, reply = "degraded", SAFE_FALLBACK_REPLY
                metrics.inc("agent_failures_total", reason=type(exc).__name__, help="Agent runs that raised")
        queues = [json.loads(row[0]).get("queue") for row in conn.execute(
            "SELECT details FROM audit_log WHERE actor = ? AND action = 'escalate_to_human'",
            (f"support-agent:{ticket_id}",))]
        conn.close()
        latency = time.perf_counter() - started
        metrics.inc("tickets_total", disposition=disposition, arm=arm, help="Tickets handled, by outcome")
        metrics.observe("ticket_latency_seconds", latency, help="End-to-end handling time per ticket")
        metrics.inc("llm_cost_usd_total", cost, help="Model spend (USD, list prices)")
        for queue in queues:
            metrics.inc("escalations_total", queue=queue, help="Human hand-offs by queue")
        for flag in flags:
            metrics.inc("guardrail_events_total", flag=flag.split(":")[0], help="Guardrail decisions")
        log.info(json.dumps({"ticket": ticket_id, "disposition": disposition, "latency_ms": round(latency * 1000),
                             "cost_usd": round(cost, 6), "prompt_version": versions[arm], "model": settings.model,
                             "trace_id": tracer.trace_id}))       # no email, no message text: PII stays out of logs
        return TicketOut(ticket_id=ticket_id, disposition=disposition, reply=reply, escalation_queues=queues,
                         guardrail_flags=flags, model=settings.model, prompt_version=versions[arm],
                         trace_id=tracer.trace_id, cost_usd=round(cost, 6), latency_ms=round(latency * 1000))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.slots = asyncio.Semaphore(settings.max_concurrency)     # bound to the running event loop
        yield

    app = FastAPI(title="Kestrel support agent", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.metrics = settings, metrics

    @app.post("/v1/tickets", response_model=TicketOut)
    async def create_ticket(ticket: TicketIn, response: Response,
                            idempotency_key: str | None = Header(default=None)) -> TicketOut | dict:
        ticket_id = ticket.ticket_id or f"T-{uuid.uuid4().hex[:8]}"
        key = idempotency_key or ticket.ticket_id
        if key:
            state, stored = store.begin(key)
            if state == "done":
                response.headers["Idempotent-Replayed"] = "true"
                metrics.inc("idempotent_replays_total", help="Duplicate deliveries answered from the store")
                return stored
            if state == "pending":
                raise HTTPException(409, "A request with this Idempotency-Key is still being processed.")
        try:
            await asyncio.wait_for(app.state.slots.acquire(), timeout=settings.queue_timeout_s)
        except asyncio.TimeoutError:
            if key:
                store.abandon(key)
            metrics.inc("tickets_shed_total", help="Requests rejected because all agent slots were busy")
            raise HTTPException(503, "All agent slots are busy; retry later.", headers={"Retry-After": "5"})
        in_flight["n"] += 1
        metrics.set("agent_runs_in_flight", in_flight["n"], help="Agent runs currently executing")
        try:
            result = await run_in_threadpool(handle, ticket, ticket_id)   # the agent loop is synchronous
        except Exception:
            if key:
                store.abandon(key)
            raise
        finally:
            in_flight["n"] -= 1
            metrics.set("agent_runs_in_flight", in_flight["n"])
            app.state.slots.release()
        if key:
            store.finish(key, result.model_dump())
        return result

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness + readiness WITHOUT calling the model: a health check must be free and fast."""
        try:
            with closing(sqlite3.connect(erp_path)) as conn:
                conn.execute("SELECT 1 FROM customers LIMIT 1").fetchone()
            ready = True
        except sqlite3.Error:
            ready = False
        if not ready:
            raise HTTPException(503, "system of record unreachable")
        return {"status": "ok", "mode": mode(), "model": settings.model, "prompt_versions": versions,
                "canary_percent": settings.canary_percent, "max_concurrency": settings.max_concurrency,
                "in_flight": in_flight["n"]}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint() -> str:
        return metrics.render()

    return app


app = create_app()
