"""Settings for the Recall Campaign Orchestrator (reference)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from labkit import FAST_MODEL, MODEL, REPO_ROOT

RECALL_DATA = REPO_ROOT / "advanced" / "data" / "recall"
SECURITY_DATA = REPO_ROOT / "advanced" / "data" / "security"
CAMPAIGN_START = dt.date(2026, 9, 16)              # the recall is issued the day after the dataset's "today"


@dataclass(frozen=True)
class Settings:
    model: str = MODEL                             # the agents (outreach, inbound)
    fast_model: str = FAST_MODEL                   # cheap classification where a rule is not enough
    auto_send: bool = True                         # False = every outbound email is held for a person (shadow mode)
    model_spend_cap_usd: float = 25.0              # campaign.json budget.model_spend_cap_usd
    per_run_max_turns: int = 8
    per_run_max_tokens: int = 4000
    reminder_after_business_days: int = 2
    approval_threshold_usd: float = 500.0          # goodwill credits above this need a support manager
    circuit_breaker_window: int = 6                # tool calls
    circuit_breaker_error_rate: float = 0.5        # trip when half the window failed
    loop_repeat_limit: int = 3                     # identical tool calls in one run
    worker: str = "orchestrator-1"
    runs_dir_name: str = "advanced_capstone"

    def with_(self, **changes) -> "Settings":
        return Settings(**{**self.__dict__, **changes})


DEFAULT = Settings()

# Which tools each role may call (least privilege per phase). Names must exist in tools.TOOLS.
ROLE_TOOLS: dict[str, list[str]] = {
    "outreach": ["get_campaign_brief", "get_contacts", "list_units", "send_email"],
    "inbound": ["get_campaign_brief", "get_contacts", "list_units", "get_thread", "get_engineer_slots", "get_parts_stock",
                "get_service_history", "send_email", "propose_slots", "book_visit", "request_goodwill_credit",
                "schedule_retry", "escalate", "mark_remediated"],
    "orchestrator": ["get_campaign_brief", "list_units", "get_sla_status", "pause_campaign", "escalate"],
}

# Business-day arithmetic for SLAs (Kestrel's campaign calendar has no holidays in the window).
def add_business_days(day: dt.date, n: int) -> dt.date:
    while n > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def business_days_between(a: dt.date, b: dt.date) -> int:
    n, day = 0, a
    while day < b:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            n += 1
    return n
