"""M5 - deterministic checks on the agent's reply before it is sent.

Any issue holds the draft for a human (disposition "review"); nothing is deleted. One check is
provided as a pattern. Add the others listed in the README (milestone M5): other customers' order
IDs, third-party email addresses, payment data, internal quality information (lot IDs, incident
numbers, "quality hold", "recall"), and admissions of liability or promises beyond policy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

MAX_CHARS = 2500


@dataclass
class GuardIssue:
    check: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def review_reply(reply: str, *, from_email: str, email_text: str, tool_calls: list[dict],
                 db: sqlite3.Connection) -> list[GuardIssue]:
    issues: list[GuardIssue] = []
    if not reply.strip():
        return [GuardIssue("empty_reply", "the agent produced no reply")]
    if len(reply) > MAX_CHARS:                       # provided example check
        issues.append(GuardIssue("too_long", f"{len(reply)} characters (limit {MAX_CHARS})"))
    # TODO(M5): foreign_order_ids, third_party_contact, payment_data, internal_quality_info, liability_or_promise
    return issues
