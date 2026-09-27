"""Solution - Exercise 11: a hard budget guard as SDK middleware.

Why middleware?  It sees every request the client makes (create, parse, tool runner...) without any
change to business code, and it can refuse to send a request before money is spent.  The guard is
checked *before* each request and updated *after* each response, so it can overshoot by at most one
request - size the budget with that in mind (or estimate the next request with count_tokens).

Limitation (by design, to keep it short): only non-streaming JSON responses are metered here; see
labkit/metering.py for how to meter SSE streams without buffering them.
"""

# test: expect=BudgetExceeded

import sys
import threading
from pathlib import Path

import anthropic
from anthropic import Middleware

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _triage import load_tickets, triage  # noqa: E402
from labkit import MODEL, header, is_mock  # noqa: E402
from labkit.mock.api import get_mock_api  # noqa: E402
from labkit.pricing import cost_usd  # noqa: E402


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard(Middleware):
    def __init__(self, max_usd: float) -> None:
        self.max_usd = max_usd
        self.spent = 0.0
        self._lock = threading.Lock()

    def handle(self, request, call_next):
        with self._lock:
            if self.spent >= self.max_usd:
                raise BudgetExceeded(f"budget ${self.max_usd:.2f} exhausted (spent ${self.spent:.4f})")
        response = call_next(request)
        http = response.http_response
        if http.status_code == 200 and "application/json" in http.headers.get("content-type", ""):
            http.read()
            body = http.json()
            if "usage" in body:
                with self._lock:
                    self.spent += cost_usd(body["usage"], body.get("model", ""))
        return response


def guarded_client(guard: BudgetGuard) -> anthropic.Anthropic:
    if is_mock():
        import httpx2
        return anthropic.Anthropic(api_key="mock-key", middleware=[guard], http_client=anthropic.DefaultHttpxClient(
            transport=httpx2.MockTransport(get_mock_api().handle), trust_env=False))
    return anthropic.Anthropic(middleware=[guard])


def main() -> None:
    guard = BudgetGuard(max_usd=0.05)
    client = guarded_client(guard)
    header("Exercise 11 - budget guard ($0.05)")
    done = 0
    try:
        for ticket in load_tickets():
            triage(client, ticket, model=MODEL)
            done += 1
    except BudgetExceeded as exc:
        print(f"BudgetExceeded after {done} tickets: {exc}")
    else:
        print(f"Finished all {done} tickets within budget (spent ${guard.spent:.4f}).")


if __name__ == "__main__":
    main()
