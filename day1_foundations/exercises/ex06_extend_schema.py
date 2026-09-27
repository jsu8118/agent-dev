"""Starter for Exercise 6 - extend the triage schema.

Copy the TicketTriage model, add the two new fields, update the prompt so the model knows today's
date, and run it on a few tickets.  This starter runs as-is and prints the TODOs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _triage import TicketTriage  # noqa: E402

TODO = """
TODO 1: define ExtendedTriage(TicketTriage) with
          requested_action: Literal["information", "return", "repair_or_replace", "refund", "callback", "quote", "other"]
          customer_deadline: Optional[datetime.date]   (describe when to use null!)
TODO 2: build a system prompt = _triage.SYSTEM_PROMPT + today's date (2026-09-15) + rules for the new fields
TODO 3: call client.messages.parse(..., output_format=ExtendedTriage) for T-1101, T-1102, T-1004, T-1601
TODO 4: print ticket id, requested_action, customer_deadline; check stop_reason first
"""

if __name__ == "__main__":
    print("Current fields:", list(TicketTriage.model_fields))
    print(TODO)
