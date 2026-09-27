"""Client factory: the ONLY place where live vs mock is decided.

    from labkit import get_client, MODEL
    client = get_client()                   # anthropic.Anthropic
    client.messages.create(model=MODEL, ...)

Live mode returns a normal `anthropic.Anthropic()` (credentials from ANTHROPIC_API_KEY
or ANTHROPIC_AUTH_TOKEN).  Mock mode returns the same class, wired to labkit's offline
mock API through an httpx2 MockTransport - the SDK code path is identical, which is why
mock mode is also how the course's automated tests run.

Both modes install `UsageMeter` middleware so every run ends with a token/cost summary.
"""

from __future__ import annotations

import sys

import anthropic
import httpx2
from anthropic import DefaultAsyncHttpxClient, DefaultHttpxClient

from . import config
from .metering import UsageMeter, register_exit_summary
from .mock.api import get_mock_api

_banner_shown = False


def _banner(mode: str) -> None:
    global _banner_shown
    if _banner_shown or config.quiet():
        return
    _banner_shown = True
    if mode == "mock":
        print("[labkit] MOCK MODE - no API key found, so responses come from labkit's offline mock of the "
              "Claude API (rule-based, not a real model). Set ANTHROPIC_API_KEY to run against Claude.",
              file=sys.stderr)
    else:
        print(f"[labkit] LIVE MODE - calling the Claude API (default model: {config.MODEL}).", file=sys.stderr)


def get_client(*, mode: str | None = None, max_retries: int = 2, timeout: float | None = None,
               meter: bool = True) -> anthropic.Anthropic:
    """Return a sync Anthropic client for the current mode ("live" or "mock")."""
    mode = mode or config.mode()
    _banner(mode)
    middleware = [UsageMeter()] if meter else []
    if meter:
        register_exit_summary()
    kwargs: dict = {"max_retries": max_retries, "middleware": middleware}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if mode == "mock":
        transport = httpx2.MockTransport(get_mock_api().handle)
        return anthropic.Anthropic(api_key="mock-key", base_url="https://api.anthropic.com",
                                   http_client=DefaultHttpxClient(transport=transport, trust_env=False), **kwargs)
    return anthropic.Anthropic(**kwargs)


def get_async_client(*, mode: str | None = None, max_retries: int = 2, timeout: float | None = None,
                     meter: bool = True) -> anthropic.AsyncAnthropic:
    """Return an async Anthropic client for the current mode ("live" or "mock")."""
    mode = mode or config.mode()
    _banner(mode)
    middleware = [UsageMeter()] if meter else []
    if meter:
        register_exit_summary()
    kwargs: dict = {"max_retries": max_retries, "middleware": middleware}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if mode == "mock":
        transport = httpx2.MockTransport(get_mock_api().handle)
        return anthropic.AsyncAnthropic(api_key="mock-key", base_url="https://api.anthropic.com",
                                        http_client=DefaultAsyncHttpxClient(transport=transport, trust_env=False),
                                        **kwargs)
    return anthropic.AsyncAnthropic(**kwargs)


def mock_api():
    """Access the process-wide mock API (e.g. to inject faults or inspect requests)."""
    return get_mock_api()
