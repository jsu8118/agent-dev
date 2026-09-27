"""The Service Desk Copilot pipeline: one inbound email in, one auditable Outcome out.

    screen (code) -> triage (LLM) -> gate (code) -> handler (code | agent) -> quality detector (code)
                                                                          -> output guard (code) -> send / review

Only two steps use a model: triage and the support agent. Everything that must be *guaranteed*
(safety escalation, quarantine, identity, refund limits, what may leave the building) is code.
Each ticket gets its own trace; its cost and latency come from that trace, so concurrent tickets
never mix their numbers.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field

import anthropic

from kestrel.support_agent import run_support_agent
from kestrel.support_tools import SupportDesk
from labkit.data import scratch_db
from labkit.tracing import Tracer

from . import gate
from .config import DEFAULT, CopilotConfig
from .output_guard import review_reply
from .quality import detect_quality_signals, publish_alerts
from .screen import screen_email
from .triage import InboundEmail, triage_email

WRITE_TOOLS = ("create_rma", "issue_refund", "escalate_to_human")


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
    """Process one inbound email end to end. Never raises for a single bad ticket: failures become outcomes."""
    start = time.perf_counter()
    tracer = tracer or Tracer("copilot")
    db = db if db is not None else scratch_db("copilot_adhoc.db")
    desk = CopilotDesk(email.from_email, db=db, actor="copilot", ticket_ref=email.ticket_id)
    outcome = Outcome(ticket_id=email.ticket_id, route="", disposition="review", reply="", trace_id=tracer.trace_id)

    with tracer.span("copilot.handle", ticket=email.ticket_id) as root:
        try:
            with tracer.span("screen") as span:
                screen = screen_email(email.from_email, email.subject, email.body)
                span.set("security_flags", len(screen.security_flags)).set("safety_hits", len(screen.safety_hits))
            outcome.screen = screen.to_dict()

            # A suspicious email never reaches a model: the screen alone decides (safety still wins in the gate).
            triage = None if screen.suspicious else triage_email(client, email, config=config, tracer=tracer)
            outcome.triage = triage.model_dump() if triage else None

            decision = gate.decide_route(screen, triage)
            outcome.route, outcome.reasons = decision.route, list(decision.reasons)
            root.set("route", decision.route)

            with tracer.span(f"route.{decision.route}"):
                if decision.route == "safety":
                    outcome.reply = gate.handle_safety(desk, email, screen, triage, decision)
                    outcome.disposition = "sent"
                elif decision.route == "security":
                    outcome.reply = gate.handle_security(desk, email, screen)
                    outcome.disposition = "quarantined"
                elif decision.route == "human":
                    outcome.reply = gate.handle_human(desk, email, "triage unavailable")
                    outcome.disposition = "sent"
                else:
                    result = run_support_agent(client, email.as_text(), email.from_email, model=config.agent_model,
                                               desk=desk, max_turns=config.agent_max_turns, tracer=tracer,
                                               ticket_ref=email.ticket_id)
                    outcome.reply = result.reply
                    with tracer.span("output_guard") as span:
                        issues = review_reply(result.reply, from_email=email.from_email, email_text=email.as_text(),
                                              tool_calls=desk.calls, db=db)
                        span.set("issues", len(issues))
                    outcome.guard_issues = [i.to_dict() for i in issues]
                    rollout_hold = not config.auto_send or (
                        config.auto_send_categories is not None
                        and (triage is None or triage.category not in config.auto_send_categories))
                    if rollout_hold:
                        outcome.reasons.append("rollout: review required at this stage")
                    outcome.disposition = "review" if (decision.review or issues or rollout_hold) else "sent"

            if decision.route != "security":            # never act on quarantined content
                with tracer.span("quality") as span:
                    alerts = detect_quality_signals(email, screen, triage, desk.calls, db)
                    span.set("alerts", len(alerts))
                outcome.quality_alerts = [a.to_dict() for a in alerts]
                if publish:
                    publish_alerts(alerts)
        except Exception as exc:                       # one broken ticket must not stop the queue
            root.error(exc)
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.disposition = "review"
            outcome.reply = outcome.reply or ""

    outcome.tool_calls = desk.calls
    outcome.escalations = [{"queue": c["input"].get("queue"), "priority": c["input"].get("priority"),
                            "escalation_id": (c.get("output") or {}).get("escalation_id")}
                           for c in desk.calls if c["name"] == "escalate_to_human" and not c["is_error"]]
    totals = tracer.totals()
    outcome.llm_calls = int(totals["llm_calls"])
    outcome.cost_usd = totals["cost_usd"]
    outcome.latency_s = round(time.perf_counter() - start, 3)
    return outcome
