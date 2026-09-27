"""Configuration and the decisions behind it (see day7_capstone/reference/DESIGN.md)."""

from __future__ import annotations

from dataclasses import dataclass

from labkit import MODEL


@dataclass(frozen=True)
class CopilotConfig:
    # Front-door triage: Claude Opus 5 at low effort. Triage is a single structured call; low effort keeps
    # cost and latency down while the safety gate (P1 recall) is protected by a deterministic backstop
    # (screen.SAFETY_RULES). Re-measure with Day 1's harness before switching to a cheaper model.
    triage_model: str = MODEL
    triage_effort: str | None = "low"
    # Resolution agent: the default model at default effort (judgement-heavy, tool-using).
    agent_model: str = MODEL
    agent_max_turns: int = 12
    # Leadership's constraints (data/company/company_profile.md): mean cost per ticket, reply latency
    max_cost_per_ticket_usd: float = 0.40
    max_p95_latency_s: float = 30.0
    # Acceptance criteria for go-live (evals.py)
    min_overall_pass_rate: float = 0.90
    gate_types: tuple[str, ...] = ("safety", "security", "privacy")     # must be 100%
    # Rollout switches (GO_LIVE_MEMO.md s.5). They only affect agent replies: safety and security handling
    # is deterministic and always live. auto_send=False is shadow mode (and the rollback switch).
    auto_send: bool = True
    auto_send_categories: tuple[str, ...] | None = None                # None = every category (stage 3)


# Supplier lots on quality hold and the incident that put them there (data/quality/incident_reports).
QUALITY_HOLDS = {
    "PS-2608-B": {"part": "MS-250 mechanical seal kit", "supplier": "Precision Seals GmbH", "incident": "INC-P2-0419",
                  "risk": "hairline cracks at the O-ring groove; field leaks possible on pumps built 2026-08-05..19"},
    "VD-2607-C": {"part": "KC-2 main board", "supplier": "VoltDrive Electronics", "incident": "INC-P3-0214",
                  "risk": "solder voids under MCU; intermittent F20 (0x3A) above ~35 C ambient"},
}

DEFAULT = CopilotConfig()
