"""Step 7: the pipeline as an internal HTTP service (Day 6, deployment).

The mail gateway POSTs every inbound email to /v1/emails and gets the Outcome back; staff read the
review queue and metrics. Production concerns handled here, each in a few lines:

* **Idempotency.** Gateways deliver at least once. `message_id` is the idempotency key: a repeat
  returns the stored outcome (and a concurrent repeat waits for the first run) instead of running
  the agent twice. `create_rma` is idempotent too; defense in depth.
* **Authentication.** A shared API key (constant-time comparison) when COPILOT_API_KEY is set.
* **Input limits.** Oversized bodies are rejected before they cost tokens.
* **Concurrency.** Sync endpoints run in FastAPI's thread pool; each request opens its own SQLite
  connection (SQLite serializes writers with file locks).
* **Observability.** Every Outcome carries its trace ID, cost and latency; /metrics aggregates them.

Run it:  uvicorn --app-dir day7_capstone/reference copilot.service:app --port 8080
"""

from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
import threading
from collections import Counter
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from labkit import get_client, mode, runs_dir
from labkit.data import data_path

from .config import DEFAULT, CopilotConfig
from .evals import p95
from .pipeline import handle_email
from .triage import InboundEmail


class EmailIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=200, description="The gateway's message ID (idempotency key)")
    from_email: str = Field(max_length=320, description="Sender address, verified by the gateway (SPF/DKIM)")
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20_000)

    @field_validator("from_email")
    @classmethod
    def _looks_like_email(cls, value: str) -> str:
        if value.count("@") != 1 or not value.split("@")[1]:
            raise ValueError("from_email must be a single email address")
        return value.strip().lower()


class OutcomeStore:
    """Processed outcomes keyed by message ID (in memory here; a table in production)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done: dict[str, dict] = {}
        self._running: dict[str, threading.Event] = {}

    def claim(self, message_id: str) -> tuple[bool, dict | None]:
        """(True, None) if the caller should process it; (False, outcome) if it was already processed."""
        with self._lock:
            if message_id in self._done:
                return False, self._done[message_id]
            event = self._running.get(message_id)
            if event is None:
                self._running[message_id] = threading.Event()
                return True, None
        event.wait(timeout=300)
        with self._lock:
            return False, self._done.get(message_id)

    def finish(self, message_id: str, outcome: dict) -> None:
        with self._lock:
            self._done[message_id] = outcome
            event = self._running.pop(message_id, None)
        if event:
            event.set()

    def abandon(self, message_id: str) -> None:
        """Processing failed before an outcome existed: let a retry try again."""
        with self._lock:
            event = self._running.pop(message_id, None)
        if event:
            event.set()

    def get(self, message_id: str) -> dict | None:
        with self._lock:
            return self._done.get(message_id)

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._done.values())


def create_app(client: anthropic.Anthropic | None = None, *, config: CopilotConfig = DEFAULT,
               db_path: Path | None = None, api_key: str | None = None) -> FastAPI:
    client = client or get_client()
    if db_path is None:                       # a private working copy of the ERP extract for this service
        db_path = runs_dir("capstone") / "service_ops.db"
        shutil.copyfile(data_path("kestrel_ops.db"), db_path)
    api_key = api_key if api_key is not None else os.environ.get("COPILOT_API_KEY") or None
    store = OutcomeStore()
    app = FastAPI(title="Kestrel Service Desk Copilot", version="1.0.0")

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if api_key and not (x_api_key and secrets.compare_digest(x_api_key, api_key)):
            raise HTTPException(status_code=401, detail="missing or invalid X-Api-Key")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "mode": mode(), "triage_model": config.triage_model, "agent_model": config.agent_model}

    @app.post("/v1/emails", dependencies=[Depends(require_key)])
    def process_email(email: EmailIn) -> dict:
        first, stored = store.claim(email.message_id)
        if not first:
            if stored is None:
                raise HTTPException(status_code=409, detail="still processing; retry later")
            return {**stored, "duplicate": True}
        try:
            conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                outcome = handle_email(client, InboundEmail(email.from_email, email.subject, email.body,
                                                            email.message_id), config=config, db=conn).to_dict()
            finally:
                conn.close()
        except Exception:
            store.abandon(email.message_id)
            raise
        store.finish(email.message_id, outcome)
        return {**outcome, "duplicate": False}

    @app.get("/v1/emails/{message_id}", dependencies=[Depends(require_key)])
    def get_outcome(message_id: str) -> dict:
        outcome = store.get(message_id)
        if outcome is None:
            raise HTTPException(status_code=404, detail="unknown message_id")
        return outcome

    @app.get("/v1/review-queue", dependencies=[Depends(require_key)])
    def review_queue() -> list[dict]:
        return [{"ticket_id": o["ticket_id"], "route": o["route"], "reasons": o["reasons"],
                 "guard_issues": o["guard_issues"], "error": o["error"], "draft_reply": o["reply"]}
                for o in store.all() if o["disposition"] == "review"]

    @app.get("/metrics", dependencies=[Depends(require_key)])
    def metrics() -> dict:
        outcomes = store.all()
        n = len(outcomes)
        return {
            "tickets": n,
            "by_route": dict(Counter(o["route"] for o in outcomes)),
            "by_disposition": dict(Counter(o["disposition"] for o in outcomes)),
            "quality_alerts": sum(len(o["quality_alerts"]) for o in outcomes),
            "errors": sum(1 for o in outcomes if o["error"]),
            "mean_cost_usd": round(sum(o["cost_usd"] for o in outcomes) / n, 5) if n else 0.0,
            "p95_latency_s": p95([o["latency_s"] for o in outcomes]),
        }

    return app


def __getattr__(name: str):
    """`uvicorn copilot.service:app` builds the app on first access (not at import time)."""
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
