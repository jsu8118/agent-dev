"""Token and cost metering for every API call a lab makes.

`UsageMeter` is Anthropic SDK *middleware* (`anthropic.Anthropic(middleware=[...])`).
The SDK invokes middleware once per HTTP attempt, so it sees every call - plain
`messages.create`, `messages.parse`, streams, the tool runner, batches - without the
lab code having to remember to record anything.  This is the same idea as metering
at an API gateway in production: cost accounting is a cross-cutting concern, so put
it at the boundary instead of sprinkling it through business logic.

* JSON responses: read the body (httpx2 caches it, so the SDK can still parse it).
* SSE streams: wrap the byte stream and sniff `message_start` / `message_delta`
  events as they flow past - the caller still receives tokens incrementally.
* Batch results (JSONL): sniff each line's usage and bill it at the 50% batch rate.
"""

from __future__ import annotations

import atexit
import json
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterator

import httpx2
from anthropic import Middleware

from . import config
from .pricing import _get, cost_usd


@dataclass
class ModelTotals:
    calls: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclass
class UsageLedger:
    """Process-wide running totals, keyed by model."""

    by_model: dict[str, ModelTotals] = field(default_factory=dict)
    count_token_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, model: str, usage: Any, *, batch: bool = False) -> None:
        with self._lock:
            totals = self.by_model.setdefault(model or "unknown", ModelTotals())
            totals.calls += 1
            totals.input_tokens += int(_get(usage, "input_tokens"))
            totals.cache_write_tokens += int(_get(usage, "cache_creation_input_tokens"))
            totals.cache_read_tokens += int(_get(usage, "cache_read_input_tokens"))
            totals.output_tokens += int(_get(usage, "output_tokens"))
            totals.cost += cost_usd(usage, model, batch=batch)

    def record_message(self, message: dict, request_model: str | None, *, batch: bool = False) -> None:
        """Record a Message JSON object, splitting server-side fallback attempts by model."""
        usage = message.get("usage") or {}
        iterations = usage.get("iterations") or []
        if any(it.get("type") in ("fallback_message", "advisor_message") for it in iterations):
            for it in iterations:
                model = it.get("model") or request_model or message.get("model", "")
                self.record(model, it, batch=batch)
        else:
            self.record(message.get("model") or request_model or "", usage, batch=batch)

    @property
    def total_cost(self) -> float:
        return sum(t.cost for t in self.by_model.values())

    @property
    def total_calls(self) -> int:
        return sum(t.calls for t in self.by_model.values())

    def reset(self) -> None:
        with self._lock:
            self.by_model.clear()
            self.count_token_calls = 0

    def summary(self) -> str:
        if not self.by_model:
            return "No Messages API calls were made."
        mode = "SIMULATED (mock mode)" if config.is_mock() else "estimated from list prices"
        lines = [f"Usage summary - {mode}"]
        for model, t in sorted(self.by_model.items()):
            lines.append(
                f"  {model:<20} calls={t.calls:<4} in={t.input_tokens:>9,} "
                f"cache_w={t.cache_write_tokens:>8,} cache_r={t.cache_read_tokens:>9,} "
                f"out={t.output_tokens:>8,}  ${t.cost:.4f}"
            )
        lines.append(f"  {'TOTAL':<20} calls={self.total_calls:<4} {'':>62}${self.total_cost:.4f}")
        return "\n".join(lines)


LEDGER = UsageLedger()


class _SSEUsageSniffer:
    """Incrementally parses SSE bytes and reports the final usage of a streamed message."""

    def __init__(self, on_done: Callable[[dict], None]) -> None:
        self._buffer = b""
        self._message: dict = {}
        self._usage: dict = {}
        self._done = False
        self._on_done = on_done

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "message_start":
                self._message = event.get("message") or {}
                self._usage = dict(self._message.get("usage") or {})
            elif kind == "message_delta":
                for key, value in (event.get("usage") or {}).items():
                    if value is not None:
                        self._usage[key] = value
            elif kind == "message_stop":
                self.finish()

    def finish(self) -> None:
        if self._done or not self._message:
            return
        self._done = True
        message = dict(self._message)
        message["usage"] = self._usage
        self._on_done(message)


class _SniffingStream(httpx2.SyncByteStream):
    def __init__(self, inner: Any, sniffer: _SSEUsageSniffer) -> None:
        self._inner, self._sniffer = inner, sniffer

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            self._sniffer.feed(chunk)
            yield chunk
        self._sniffer.finish()

    def close(self) -> None:
        self._sniffer.finish()
        close = getattr(self._inner, "close", None)
        if close:
            close()


class _AsyncSniffingStream(httpx2.AsyncByteStream):
    def __init__(self, inner: Any, sniffer: _SSEUsageSniffer) -> None:
        self._inner, self._sniffer = inner, sniffer

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._sniffer.feed(chunk)
            yield chunk
        self._sniffer.finish()

    async def aclose(self) -> None:
        self._sniffer.finish()
        aclose = getattr(self._inner, "aclose", None)
        if aclose:
            await aclose()


class _JSONLUsageSniffer:
    """Batch results arrive as JSON Lines; each succeeded line carries a full Message."""

    def __init__(self, ledger: UsageLedger) -> None:
        self._buffer = b""
        self._ledger = ledger

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._line(line)

    def _line(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            row = json.loads(line)
        except ValueError:
            return
        result = row.get("result") or {}
        if result.get("type") == "succeeded":
            message = result.get("message") or {}
            self._ledger.record_message(message, message.get("model"), batch=True)

    def finish(self) -> None:
        if self._buffer.strip():
            self._line(self._buffer)
        self._buffer = b""


class UsageMeter(Middleware):
    """SDK middleware that feeds every Messages API response into a UsageLedger."""

    def __init__(self, ledger: UsageLedger = LEDGER) -> None:
        self.ledger = ledger

    # -- helpers -----------------------------------------------------------------
    def _observe(self, request: Any, http: httpx2.Response, *, is_async: bool) -> None:
        path = http.request.url.path if http.request is not None else str(request.url)
        if http.status_code != 200:
            return
        body = request.json if isinstance(request.json, dict) else {}
        model = body.get("model", "")
        ctype = http.headers.get("content-type", "")
        if path.endswith("/messages/count_tokens"):
            self.ledger.count_token_calls += 1
            return
        # An in-memory body (e.g. from a MockTransport) is already fully read, so iterating
        # the response never touches `.stream`; parse such bodies directly instead.
        preloaded = getattr(http, "_content", None)
        if path.endswith("/results") and "/batches/" in path:
            sniffer = _JSONLUsageSniffer(self.ledger)
            if preloaded is not None:
                sniffer.feed(preloaded)
                sniffer.finish()
            else:
                http.stream = (_AsyncJSONL if is_async else _SyncJSONL)(http.stream, sniffer)
            return
        if not path.endswith("/messages"):
            return
        if "text/event-stream" in ctype:
            sniffer = _SSEUsageSniffer(lambda msg: self.ledger.record_message(msg, model))
            if preloaded is not None:
                sniffer.feed(preloaded)
                sniffer.finish()
            else:
                http.stream = (_AsyncSniffingStream if is_async else _SniffingStream)(http.stream, sniffer)

    def handle(self, request: Any, call_next: Any) -> Any:
        response = call_next(request)
        http = response.http_response
        self._observe(request, http, is_async=False)
        if http.status_code == 200 and "application/json" in http.headers.get("content-type", ""):
            path = http.request.url.path if http.request is not None else ""
            if path.endswith("/messages"):
                http.read()
                self.ledger.record_message(http.json(), (request.json or {}).get("model"))
        return response

    async def handle_async(self, request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        http = response.http_response
        self._observe(request, http, is_async=True)
        if http.status_code == 200 and "application/json" in http.headers.get("content-type", ""):
            path = http.request.url.path if http.request is not None else ""
            if path.endswith("/messages"):
                await http.aread()
                self.ledger.record_message(http.json(), (request.json or {}).get("model"))
        return response


class _SyncJSONL(httpx2.SyncByteStream):
    def __init__(self, inner: Any, sniffer: _JSONLUsageSniffer) -> None:
        self._inner, self._sniffer = inner, sniffer

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            self._sniffer.feed(chunk)
            yield chunk
        self._sniffer.finish()

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close:
            close()


class _AsyncJSONL(httpx2.AsyncByteStream):
    def __init__(self, inner: Any, sniffer: _JSONLUsageSniffer) -> None:
        self._inner, self._sniffer = inner, sniffer

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._sniffer.feed(chunk)
            yield chunk
        self._sniffer.finish()

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose:
            await aclose()


_summary_registered = False


def register_exit_summary() -> None:
    """Print the ledger summary when the process exits (once per process)."""
    global _summary_registered
    if _summary_registered:
        return
    _summary_registered = True

    def _print() -> None:
        if LEDGER.total_calls and not config.quiet():
            print("\n" + LEDGER.summary())

    atexit.register(_print)
