"""M3 - the pipeline: one inbound email in, one auditable Outcome out.

    screen (code) -> triage (LLM) -> gate (code) -> handler (code | agent) -> quality (code)
                                                                         -> output guard (code) -> send / review

Provided: the Outcome record, a SupportDesk that also records tool outputs, and the skeleton of
handle_email with its bookkeeping (trace, cost, latency, error handling). Fill in the TODO steps.
The acceptance suite calls handle_email(client, email, config=..., db=..., publish=False).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field

import anthropic

from kestrel.support_agent import run_support_agent            # noqa: F401  (you'll need it)
from kestrel.support_tools import SupportDesk
from labkit.data import scratch_db
from labkit.tracing import Tracer

from . import gate                                              # noqa: F401
from .config import DEFAULT, CopilotConfig
from .output_guard import review_reply                          # noqa: F401
from .quality import detect_quality_signals, publish_alerts     # noqa: F401
from .screen import screen_email                                # noqa: F401
from .triage import InboundEmail, triage_email                  # noqa: F401


class CopilotDesk(SupportDesk):
    """The agent's tool backend, also recording each tool's output (for the guard, the detector and evals)."""

    def run(self, name: str, tool_input: dict) -> tuple[str, bool]:
        content, is_error = super().run(name, tool_input)
        try:
            self.calls[-1]["output"] = json.loads(content)
        except ValueError:
            self.calls[-1]["output"] = content
        return content, is_error


@dataclass
class Outcome:
    ticket_id: str
    route: str
    disposition: str                 # sent | review | quarantined
    reply: str
    reasons: list[str] = field(default_factory=list)
    triage: dict | None = None
    screen: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    escalations: list[dict] = field(default_factory=list)
    quality_alerts: list[dict] = field(default_factory=list)
    guard_issues: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    trace_id: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def handle_email(client: anthropic.Anthropic, email: InboundEmail, *, config: CopilotConfig = DEFAULT,
                 db: sqlite3.Connection | None = None, tracer: Tracer | None = None,
                 publish: bool = True) -> Outcome:
    start = time.perf_counter()
    tracer = tracer or Tracer("copilot")
    db = db if db is not None else scratch_db("copilot_starter_adhoc.db")
    desk = CopilotDesk(email.from_email, db=db, actor="copilot", ticket_ref=email.ticket_id)
    outcome = Outcome(ticket_id=email.ticket_id, route="", disposition="review", reply="", trace_id=tracer.trace_id)

    with tracer.span("copilot.handle", ticket=email.ticket_id) as root:
        try:
            # TODO(M3) 1. screen the email (wrap each step in tracer.span(...) so it shows in the trace);
            #             store screen.to_dict() in outcome.screen.
            # TODO(M3) 2. triage it with triage_email(client, email, config=config, tracer=tracer) - but should a
            #             model ever read an email the screen flagged as suspicious? Store triage.model_dump().
            # TODO(M3) 3. decide the route (gate.decide_route); set outcome.route and outcome.reasons.
            # TODO(M3) 4. run the handler. For "agent": run_support_agent(client, email.as_text(), email.from_email,
            #             model=config.agent_model, desk=desk, max_turns=config.agent_max_turns, tracer=tracer,
            #             ticket_ref=email.ticket_id), then review_reply(...) (M5). Set outcome.reply and
            #             outcome.disposition ("sent", "review" or "quarantined").
            # TODO(M4) 5. unless quarantined: detect_quality_signals(...) -> outcome.quality_alerts; publish if `publish`.
            raise NotImplementedError("M3: handle_email")
        except Exception as exc:                       # one broken ticket must not stop the queue
            root.error(exc)
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.disposition = "review"

    outcome.tool_calls = desk.calls
    outcome.escalations = [{"queue": c["input"].get("queue"), "priority": c["input"].get("priority"),
                            "escalation_id": (c.get("output") or {}).get("escalation_id")}
                           for c in desk.calls if c["name"] == "escalate_to_human" and not c["is_error"]]
    totals = tracer.totals()
    outcome.llm_calls = int(totals["llm_calls"])
    outcome.cost_usd = totals["cost_usd"]
    outcome.latency_s = round(time.perf_counter() - start, 3)
    return outcome
