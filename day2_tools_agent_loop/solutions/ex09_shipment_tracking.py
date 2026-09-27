"""Solution to exercise 9 - add a `get_shipment_tracking` tool to the lab 02 agent.

Design decisions (explained in solutions/README.md):
* A NEW, narrow tool rather than more fields on get_order_status: tracking questions are the most common
  ticket type (38% of volume), and a tracking-shaped result answers them in one call.
* The tool COMPUTES lateness in business days (policy arithmetic belongs in code, not in the model).
* The description says when to prefer it over get_order_status; the result is curated JSON; failures are
  is_error results that say what to do next.  The tool is read-only, so it is parallel-safe.

Run
    python day2_tools_agent_loop/solutions/ex09_shipment_tracking.py
"""

# test: expect=get_shipment_tracking

from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _order_desk import SYSTEM_PROMPT, TOOLS, OrderDesk, _tool  # noqa: E402
from labkit import get_client, header, step, wrap  # noqa: E402

lab02 = importlib.import_module("02_agent_loop_from_scratch")

TODAY = dt.date(2026, 9, 15)
TRACKING_URLS = {"NorthLine Freight": "https://track.northline.example/{n}",
                 "SwiftParcel": "https://swiftparcel.example/t/{n}",
                 "BlueRiver Logistics": "https://blueriver.example/track?id={n}"}

GET_SHIPMENT_TRACKING = _tool(
    "get_shipment_tracking",
    "Get the shipment tracking view of ONE order: carrier, tracking number and link, ship date, ETA or delivered "
    "date, carrier exceptions, and how many business days the (expected) arrival is after the promised date. "
    "Prefer it over get_order_status for 'where is it / when will it arrive / is it late / is it stuck' questions. "
    "Order IDs look like SO-10234.",
    {"order_id": {"type": "string", "description": "Sales order ID, e.g. SO-10234"}},
    ["order_id"])


def business_days_between(start: dt.date, end: dt.date) -> int:
    """Business days after `start` up to and including `end` (0 if end <= start)."""
    days, current = 0, start
    while current < end:
        current += dt.timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


class TrackingDesk(OrderDesk):
    tool_names = OrderDesk.tool_names | {"get_shipment_tracking"}

    def get_shipment_tracking(self, order_id: str) -> dict:
        order = self.get_order_status(order_id)          # reuses validation + instructive errors
        ship = order.get("shipment")
        if not ship:
            # "Not shipped yet" is a valid ANSWER, not a failure: return it as data, not as is_error.
            return {"order_id": order["order_id"], "status": order["status"],
                    "promised_date": order["promised_date"], "shipment": None,
                    "note": "Not shipped yet, so there is no tracking number; quote the promised date."}
        arrival = dt.date.fromisoformat(ship.get("delivered_date") or ship["eta_date"])
        promised = dt.date.fromisoformat(order["promised_date"])
        url = TRACKING_URLS.get(ship["carrier"], "").format(n=ship["tracking_number"]) or None
        return {
            "order_id": order["order_id"], "status": order["status"], "promised_date": order["promised_date"],
            "shipment": {**ship, "tracking_url": url},
            "lateness": {"expected_arrival": arrival.isoformat(),
                         "business_days_late": business_days_between(promised, arrival),
                         "arrived": bool(ship.get("delivered_date"))},
        }


def main() -> None:
    client = get_client()
    header("Exercise 9 - a get_shipment_tracking tool")
    tools = [*TOOLS, GET_SHIPMENT_TRACKING]
    questions = [
        "Fiona at Coastal Shipyards says tracking for SO-10300 hasn't updated since the 10th - is it stuck in customs?",
        "Is SO-10290 late, and by how many business days?",
        "When will SO-10312 arrive?",
    ]
    for number, question in enumerate(questions, start=1):
        step(number, question)
        desk = TrackingDesk()
        run = lab02.run_agent(client, question, desk, tools=tools, system=SYSTEM_PROMPT)
        print("  tool calls: " + ", ".join(f"{c['name']}({c['input'].get('order_id')})"
                                          + (" -> is_error" if c["is_error"] else "") for c in desk.calls))
        print(wrap(run.answer))

    step(len(questions) + 1, "The tool's result, as the model sees it")
    content, _ = TrackingDesk().run("get_shipment_tracking", {"order_id": "SO-10290"})
    print(content)


if __name__ == "__main__":
    main()
