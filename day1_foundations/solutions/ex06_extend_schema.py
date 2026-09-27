"""Solution - Exercise 6: extend the triage schema with requested_action and customer_deadline.

Key points
* New enum field: the model can only pick an allowed action; "other" is the explicit escape hatch.
* Dates: the model does not know "today" - put it in the prompt, or relative dates ("the 18th")
  resolve to garbage. Say what to do when it is ambiguous (null), so the model has a legal way out.
* The schema's descriptions are part of the prompt.
"""

# test: expect=customer_deadline

import datetime as dt
import sys
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _triage import SYSTEM_PROMPT, TicketTriage, load_tickets, ticket_prompt  # noqa: E402
from labkit import MODEL, get_client, header  # noqa: E402


class ExtendedTriage(TicketTriage):
    requested_action: Literal["information", "return", "repair_or_replace", "refund", "callback", "quote", "other"] = \
        Field(description="What the customer wants Kestrel to do")
    customer_deadline: Optional[dt.date] = Field(
        description="Date the customer says they need the outcome by (e.g. an installation date), resolved against "
                    "today's date; null if no deadline is stated or it cannot be resolved unambiguously")


EXTENDED_SYSTEM = SYSTEM_PROMPT + """

Additional fields:
- requested_action: the action the customer is asking for. If they ask for a refund, use "refund" even when the
  category is return_request. Use "callback" if they explicitly ask to be called.
- customer_deadline: today is 2026-09-15. Resolve relative dates ("the 18th", "next Monday") against today. If the
  customer gives no deadline, or it is ambiguous, use null. Never invent one."""


def main() -> None:
    client = get_client()
    header("Exercise 6 - extended triage")
    tickets = {t["ticket_id"]: t for t in load_tickets()}
    for tid in ("T-1101", "T-1102", "T-1004", "T-1601"):
        message = client.messages.parse(model=MODEL, max_tokens=4000, output_format=ExtendedTriage,
                                        system=[{"type": "text", "text": EXTENDED_SYSTEM,
                                                 "cache_control": {"type": "ephemeral"}}],
                                        messages=[{"role": "user", "content": ticket_prompt(tickets[tid])}])
        if message.stop_reason != "end_turn":
            print(f"{tid}: no usable result (stop_reason={message.stop_reason})")
            continue
        r = message.parsed_output
        print(f"{tid}: category={r.category:<16} requested_action={r.requested_action:<18} "
              f"customer_deadline={r.customer_deadline}")


if __name__ == "__main__":
    main()
