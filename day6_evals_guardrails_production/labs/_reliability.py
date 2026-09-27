"""_reliability - client-side resilience helpers (Day 6 labs 01 and 07, the lab 08 service).

Everything here is SDK *middleware* or plain Python, so it behaves the same against the live API
and the offline mock:

    AttemptLog          records every HTTP attempt the SDK makes (middleware runs once per attempt)
    ChaosMiddleware     client-side fault injection: raise synthetic 429/529/500/timeouts before the
                        request leaves the process - the SDK's real retry policy then handles them
    LatencyMiddleware   adds a fixed delay per attempt (to exercise deadlines and concurrency limits)
    DeadlineMiddleware  enforces an end-to-end deadline across all calls of one task
    strip_fallbacks     removes `fallbacks` from requests: simulates a platform without server-side
                        fallbacks, so refusals reach your code
    retry_with_backoff  application-level retry with capped exponential backoff and full jitter
    CircuitBreaker      stop calling a failing dependency; probe again after a cool-down
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar

import anthropic
import httpx2
from anthropic import Middleware

from labkit import LEDGER
from labkit.metering import UsageMeter

T = TypeVar("T")
RETRYABLE = (anthropic.RateLimitError, anthropic.OverloadedError, anthropic.InternalServerError,
             anthropic.APIConnectionError)          # APITimeoutError is a subclass of APIConnectionError


def with_middleware(client: anthropic.Anthropic, *extra: Any, **options: Any) -> anthropic.Anthropic:
    """A copy of `client` with extra middleware - keeping the course's usage meter first in the chain."""
    return client.with_options(middleware=[UsageMeter(LEDGER), *extra], **options)


# ------------------------------------------------------------------------------------------ attempt log
@dataclass
class Attempt:
    call: int            # logical call number (a new call starts when retries_taken == 0)
    retry: int           # the SDK's retries_taken for this attempt
    outcome: str         # HTTP status ("200", "429", ...) or exception class name
    at_s: float          # seconds since the log was created


class AttemptLog(Middleware):
    """Record every attempt the SDK makes, including the ones its retry loop hides from you.

    Intended for sequential calls (concurrent calls interleave and the call numbering loses meaning).
    """

    def __init__(self) -> None:
        self.attempts: list[Attempt] = []
        self._t0 = time.perf_counter()
        self._calls = -1
        self._lock = threading.Lock()

    def handle(self, request: Any, call_next: Any) -> Any:
        with self._lock:
            if request.retries_taken == 0:
                self._calls += 1
            call = self._calls
        try:
            response = call_next(request)
        except Exception as exc:
            self._record(call, request.retries_taken, str(getattr(exc, "status_code", "") or type(exc).__name__))
            raise
        self._record(call, request.retries_taken, str(response.http_response.status_code))
        return response

    def _record(self, call: int, retry: int, outcome: str) -> None:
        with self._lock:
            self.attempts.append(Attempt(call, retry, outcome, time.perf_counter() - self._t0))

    def calls(self) -> list[list[Attempt]]:
        grouped: dict[int, list[Attempt]] = {}
        for a in self.attempts:
            grouped.setdefault(a.call, []).append(a)
        return [grouped[k] for k in sorted(grouped)]

    def render(self) -> str:
        lines = []
        for attempts in self.calls():
            t0 = attempts[0].at_s
            chain = " -> ".join(f"{a.outcome} (+{a.at_s - t0:.2f}s)" for a in attempts)
            lines.append(f"  call {attempts[0].call}: {chain}")
        return "\n".join(lines)


# ------------------------------------------------------------------------------------------ fault injection
_FAULTS = {
    "429": (anthropic.RateLimitError, "rate_limit_error", {"retry-after-ms": "40"}),
    "500": (anthropic.InternalServerError, "api_error", {}),
    "529": (anthropic.OverloadedError, "overloaded_error", {}),
    "529-no-retry": (anthropic.OverloadedError, "overloaded_error", {"x-should-retry": "false"}),
}


def make_fault(kind: str) -> Exception:
    """A synthetic error, shaped exactly like the SDK's own (status, headers, error body)."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if kind == "timeout":
        return anthropic.APITimeoutError(request=request)
    if kind == "connection":
        return anthropic.APIConnectionError(request=request)
    cls, error_type, headers = _FAULTS[kind]
    status = int(kind[:3])
    body = {"type": "error", "error": {"type": error_type, "message": f"Injected fault ({kind})"}}
    response = httpx2.Response(status, headers=headers, json=body, request=request)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


class ChaosMiddleware(Middleware):
    """Fail the next attempts with the queued faults (optionally only when `when(request)` is true).

    Raising a typed APIStatusError / APIConnectionError from middleware opts into the SDK's retry
    policy, so this exercises the SDK's real backoff, `retry-after-ms` and `x-should-retry` handling.
    """

    def __init__(self, faults: Iterable[str] = (), *, when: Callable[[Any], bool] | None = None) -> None:
        self._queue = list(faults)
        self._when = when
        self._lock = threading.Lock()
        self.injected: list[str] = []

    def inject(self, *faults: str) -> None:
        with self._lock:
            self._queue.extend(faults)

    def handle(self, request: Any, call_next: Any) -> Any:
        with self._lock:
            fault = self._queue.pop(0) if self._queue and (self._when is None or self._when(request)) else None
            if fault:
                self.injected.append(fault)
        if fault:
            raise make_fault(fault)
        return call_next(request)


class LatencyMiddleware(Middleware):
    """Add `delay_s` to every attempt - a stand-in for a slow network or a slow model."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s

    def handle(self, request: Any, call_next: Any) -> Any:
        time.sleep(self.delay_s)
        return call_next(request)


class DeadlineExceeded(Exception):
    """Not an SDK error type, so the SDK does NOT retry it: the task is out of time, stop."""


class DeadlineMiddleware(Middleware):
    """One end-to-end deadline for all the calls of a task (e.g. the 30-second reply budget).

    Before each attempt: refuse to start when the budget is spent, otherwise cap the attempt's
    timeout at the time remaining - so the SDK's own timeout x retries can never overrun it.
    """

    def __init__(self, budget_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.deadline = clock() + budget_s

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def handle(self, request: Any, call_next: Any) -> Any:
        left = self.remaining()
        if left <= 0:
            raise DeadlineExceeded(f"task deadline exceeded by {-left:.2f}s")
        return call_next(request.copy(timeout=left))


def strip_fallbacks(request: Any, call_next: Any) -> Any:
    """Drop `fallbacks` from the request body: behaves like a platform without server-side fallbacks."""
    body = request.json
    if isinstance(body, dict) and "fallbacks" in body:
        request = request.copy(body={k: v for k, v in body.items() if k != "fallbacks"})
    return call_next(request)


# ------------------------------------------------------------------------------------------ app-level retry
@dataclass
class RetryRecord:
    attempt: int
    error: str
    sleep_s: float


def retry_with_backoff(fn: Callable[[], T], *, attempts: int = 4, base_s: float = 0.25, cap_s: float = 8.0,
                       retry_on: tuple[type[BaseException], ...] = RETRYABLE, deadline: float | None = None,
                       rng: random.Random | None = None, sleep: Callable[[float], None] = time.sleep,
                       log: list[RetryRecord] | None = None) -> T:
    """Call fn(); on a retryable error sleep U(0, min(cap, base * 2**n)) ("full jitter") and try again.

    Full jitter spreads a thundering herd of clients over the whole backoff window instead of
    synchronising their retries.  The server's `x-should-retry: false` always wins, and a deadline
    (time.monotonic() value) stops retrying when the next sleep would overrun it.
    """
    rng = rng or random.Random()
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.headers.get("x-should-retry") == "false":
                raise
            if attempt == attempts:
                raise
            delay = rng.uniform(0, min(cap_s, base_s * 2 ** (attempt - 1)))
            if deadline is not None and time.monotonic() + delay > deadline:
                raise
            if log is not None:
                log.append(RetryRecord(attempt, type(exc).__name__, delay))
            sleep(delay)
    raise RuntimeError("unreachable")


# ------------------------------------------------------------------------------------------ circuit breaker
@dataclass
class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (cool-down) -> half_open -> one probe -> closed/open."""

    failure_threshold: int = 3
    cooldown_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    state: str = "closed"
    failures: int = 0
    opened_at: float = 0.0
    transitions: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _set(self, state: str) -> None:
        if state != self.state:
            self.transitions.append(f"{self.state}->{state}")
            self.state = state

    def allow(self) -> bool:
        with self._lock:
            if self.state == "open" and self.clock() - self.opened_at >= self.cooldown_s:
                self._set("half_open")          # let exactly one probe through
                return True
            return self.state == "closed"

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self._set("closed")

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.failure_threshold:
                self.opened_at = self.clock()
                self._set("open")
