"""Solution to exercise 12 - per-customer rate limits for the support-agent service.

Why per CUSTOMER (sender domain) and not per IP: every email reaches the service from the same mail gateway,
so an IP limit would throttle everyone at once; one noisy or compromised customer mailbox must not be able
to burn the whole model budget or starve other customers.

Design
  * a token bucket per customer: capacity = burst, refill = sustained rate; checked BEFORE any model call
  * implemented as ASGI middleware around the lab 08 app (no change to app.py): it buffers the request body,
    reads from_email, and either replays the body to the app or answers 429 with Retry-After
  * idempotent redeliveries are not charged: a request whose Idempotency-Key was already answered is passed
    through (the app serves it from its store at zero model cost)
  * an injectable clock, so the refill is testable without sleeping

Run
    python day6_evals_guardrails_production/solutions/ex12_rate_limits.py
"""

# test: expect=rate limit test passed

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient

LABS = Path(__file__).resolve().parents[1] / "labs"
sys.path.insert(0, str(LABS))

from labkit import header, runs_dir, step  # noqa: E402


def load_service():
    spec = importlib.util.spec_from_file_location("kestrel_service", LABS / "08_service" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class TokenBucketLimiter:
    capacity: float = 3.0                 # burst
    refill_per_s: float = 1 / 20          # sustained: one ticket every 20 s per customer
    clock: Callable[[], float] = time.monotonic
    buckets: dict[str, tuple[float, float]] = field(default_factory=dict)   # key -> (tokens, last refill time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def try_acquire(self, key: str) -> tuple[bool, float]:
        """(allowed, seconds until the next token)."""
        with self._lock:
            now = self.clock()
            tokens, last = self.buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_s)
            if tokens >= 1:
                self.buckets[key] = (tokens - 1, now)
                return True, 0.0
            self.buckets[key] = (tokens, now)
            return False, (1 - tokens) / self.refill_per_s


class CustomerRateLimit:
    """ASGI middleware: per-customer limits on POST /v1/tickets."""

    def __init__(self, app, limiter: TokenBucketLimiter, already_answered: Callable[[str], bool]) -> None:
        self.app, self.limiter, self.already_answered = app, limiter, already_answered
        self.rejected: dict[str, int] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/v1/tickets":
            return await self.app(scope, receive, send)
        body, more = b"", True
        while more:                                   # buffer the body so we can read it AND replay it
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        key = headers.get("idempotency-key")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        customer = str(payload.get("from_email", "")).rsplit("@", 1)[-1].lower() or "unknown"
        if not (key and self.already_answered(key)):
            allowed, wait = self.limiter.try_acquire(customer)
            if not allowed:
                self.rejected[customer] = self.rejected.get(customer, 0) + 1
                reply = json.dumps({"detail": f"Rate limit for {customer}: retry in {wait:.0f}s"}).encode()
                await send({"type": "http.response.start", "status": 429,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"retry-after", str(max(1, round(wait))).encode())]})
                await send({"type": "http.response.body", "body": reply})
                return
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


def main() -> None:
    header("Exercise 12 - per-customer rate limits (token bucket) around the service")
    service = load_service()
    now = {"t": 0.0}
    limiter = TokenBucketLimiter(capacity=3, refill_per_s=1 / 20, clock=lambda: now["t"])
    state_dir = runs_dir("day6_service", "ex12", uuid.uuid4().hex[:8])
    app = service.create_app(service.Settings(state_dir=state_dir))
    store = service.IdempotencyStore(state_dir / "idempotency.db")
    # status() is read-only: probing with begin() would CLAIM the key and make the app answer 409.
    guarded = CustomerRateLimit(app, limiter, already_answered=lambda k: store.status(k) == "done")
    ok = True

    def post(api, email: str, key: str | None = None):
        body = {"ticket_id": f"RL-{uuid.uuid4().hex[:6]}", "from_email": email, "subject": "Tracking",
                "body": "Tracking number for SO-10303, please." if "greenvalley" in email else
                        "We over-ordered MS-250 seal kits on SO-10283. Can we return 4 unopened kits?"}
        headers = {"Idempotency-Key": key} if key else {}
        return api.post("/v1/tickets", json=body, headers=headers)

    with TestClient(guarded) as api:
        step(1, "A burst of 5 tickets from one customer (capacity 3, then 1 every 20 s)")
        codes = [post(api, "aisha.karim@harborfoods.example") for _ in range(5)]
        print("  harborfoods:  " + ", ".join(f"{r.status_code}" + (f" (Retry-After {r.headers['retry-after']}s)"
                                                                     if r.status_code == 429 else "") for r in codes))
        ok &= [r.status_code for r in codes] == [200, 200, 200, 429, 429]

        step(2, "Another customer is unaffected")
        other = post(api, "jorge.medina@greenvalley-coop.example")
        print(f"  greenvalley:  {other.status_code}")
        ok &= other.status_code == 200

        step(3, "Time passes: the bucket refills")
        now["t"] += 20.0
        refilled = post(api, "aisha.karim@harborfoods.example")
        again = post(api, "aisha.karim@harborfoods.example")
        print(f"  harborfoods after 20 s: {refilled.status_code}, then {again.status_code}")
        ok &= refilled.status_code == 200 and again.status_code == 429

        step(4, "A redelivered ticket (same Idempotency-Key, already answered) is not charged")
        now["t"] += 20.0
        first = post(api, "aisha.karim@harborfoods.example", key="gw-redelivery-1")
        second = post(api, "aisha.karim@harborfoods.example", key="gw-redelivery-1")
        print(f"  first delivery {first.status_code}; redelivery {second.status_code} "
              f"(replayed={second.headers.get('idempotent-replayed')}) with an empty bucket")
        ok &= first.status_code == 200 and second.status_code == 200

    step(5, "Summary")
    print(f"  rejections by customer: {guarded.rejected}")
    print("  In production: keep buckets in Redis (shared by all workers and replicas), export rejections as a metric\n"
          "  per customer TIER (not per domain - label cardinality), and add a daily token/cost budget per customer.")
    shutil.rmtree(state_dir, ignore_errors=True)
    print("rate limit test passed" if ok else "RATE LIMIT TEST FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
