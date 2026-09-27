"""MockAnthropicAPI - an offline stand-in for the Claude API, served at the HTTP layer.

The Anthropic SDK is used unmodified: `labkit.get_client()` plugs this object into the
SDK as an httpx2 `MockTransport`, so requests are serialized, sent, parsed, retried and
streamed by the real SDK code paths.  Only the "model" is fake.

Endpoints: POST /v1/messages (JSON + SSE streaming), POST /v1/messages/count_tokens,
the Message Batches endpoints, and GET /v1/models[/id].

Teaching hooks:
  * `inject_faults(429, 529, ...)`  - the next N /v1/messages calls fail with those statuses
  * "[simulate:refusal]" in a user message makes refusal-capable models decline
    (and `fallbacks="default"` then re-runs the request on the fallback model)
  * `request_log` - every request body the mock received (handy in tests)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from typing import Any, Callable

import httpx2

from ..models import CATALOG, get_spec, known_model
from .cache import PromptCache, render_positions
from .context import apply_context_management
from .errors import ApiError, bad_request
from .registry import ScenarioError, dispatch
from .render import build_message, next_id, to_sse
from .reply import refuse
from .request import MockRequest
from .tokens import text_tokens
from .validate import validate_messages_request

REFUSAL_MARKER = "[simulate:refusal]"
FALLBACK_MODEL = "claude-opus-4-8"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class MockAnthropicAPI:
    def __init__(self, *, strict: bool | None = None, clock: Callable[[], float] = time.time,
                 batch_polls_before_done: int = 1) -> None:
        self.cache = PromptCache(clock)
        self.strict = (os.environ.get("LABKIT_MOCK_STRICT", "1") != "0") if strict is None else strict
        self.batch_polls_before_done = batch_polls_before_done
        self.request_log: list[dict] = []
        self.last_scenario: str | None = None
        self._faults: list[int] = []
        self._batches: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ controls
    def reset(self) -> None:
        with self._lock:
            self.cache.clear()
            self.request_log.clear()
            self._faults.clear()
            self._batches.clear()
            self.last_scenario = None

    def inject_faults(self, *statuses: int) -> None:
        """Make the next len(statuses) /v1/messages requests fail with these HTTP statuses."""
        with self._lock:
            self._faults.extend(statuses)

    # ------------------------------------------------------------------ transport entry point
    def handle(self, request: httpx2.Request) -> httpx2.Response:
        request_id = next_id("req_mock_")
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            body: Any = json.loads(request.content) if request.content else {}
        except ValueError:
            return self._error(ApiError(400, "Request body is not valid JSON"), request_id)
        path, method = request.url.path.rstrip("/"), request.method.upper()
        try:
            if path == "/v1/messages" and method == "POST":
                return self._messages(body, headers, request_id)
            if path == "/v1/messages/count_tokens" and method == "POST":
                return self._count_tokens(body, headers, request_id)
            if path == "/v1/messages/batches" and method == "POST":
                return self._json(self._batch_create(body, headers), request_id)
            if path == "/v1/messages/batches" and method == "GET":
                data = [self._batch_view(b) for b in self._batches.values()]
                return self._json({"data": data, "has_more": False,
                                   "first_id": data[0]["id"] if data else None,
                                   "last_id": data[-1]["id"] if data else None}, request_id)
            if path.startswith("/v1/messages/batches/"):
                return self._batch_route(path, method, request_id)
            if path == "/v1/models" and method == "GET":
                data = [self._model_info(m) for m in CATALOG]
                return self._json({"data": data, "has_more": False, "first_id": data[0]["id"],
                                   "last_id": data[-1]["id"]}, request_id)
            if path.startswith("/v1/models/") and method == "GET":
                model = path.rsplit("/", 1)[-1]
                if not known_model(model):
                    raise ApiError(404, f"model: {model}")
                return self._json(self._model_info(model), request_id)
            raise ApiError(404, f"Not found: {method} {path} (not implemented by the labkit mock)")
        except ApiError as exc:
            return self._error(exc, request_id)
        except ScenarioError as exc:
            return self._error(ApiError(500, f"[labkit mock] {exc}", headers={"x-should-retry": "false"}), request_id)

    # ------------------------------------------------------------------ /v1/messages
    def _messages(self, body: dict, headers: dict, request_id: str) -> httpx2.Response:
        with self._lock:
            fault = self._faults.pop(0) if self._faults else None
        if fault is not None:
            extra = {"retry-after-ms": "25", "retry-after": "0"} if fault == 429 else {}
            messages = {429: "Number of request tokens has exceeded your per-minute rate limit (simulated)",
                        529: "Overloaded (simulated)", 500: "Internal server error (simulated)"}
            raise ApiError(fault, messages.get(fault, f"Simulated failure {fault}"), headers=extra)
        message = self.create_message(body, headers)
        if body.get("stream"):
            return httpx2.Response(200, content=to_sse(message),
                                   headers={"content-type": "text/event-stream", "request-id": request_id})
        return self._json(message, request_id)

    def create_message(self, body: dict, headers: dict) -> dict:
        user_agent = headers.get("user-agent", "").lower()
        lenient = (not self.strict) or "claude-cli" in user_agent or "claude-code" in user_agent
        validate_messages_request(body, headers, lenient=lenient)
        with self._lock:
            self.request_log.append(body)
            del self.request_log[:-200]
        spec = get_spec(body["model"])

        effective, applied_edits, compaction, compaction_input = apply_context_management(body)
        cache = self.cache.process(effective, spec)
        if cache.total_tokens > spec.context_window:
            raise bad_request(f"prompt is too long: {cache.total_tokens} tokens > {spec.context_window} maximum")

        req = MockRequest(effective, headers)
        name, reply = dispatch(req)
        refusal_requested = REFUSAL_MARKER in (req.last_user_text + req.first_user_text)
        if refusal_requested and spec.refusal_classifiers and reply.stop_reason != "refusal":
            reply = refuse("cyber", "Simulated classifier decline (the lab asked for one).")
        self.last_scenario = name

        if reply.stop_reason == "refusal" and body.get("fallbacks"):
            return self._fallback(body, effective, headers, spec.id, reply, cache)

        message = build_message(req, reply, cache, cleared_edits=applied_edits, compaction=compaction)
        if compaction is not None:
            # Like the real API: the compaction pass is its own iteration, and top-level usage covers only the
            # message iteration. Bill by summing usage.iterations.
            summary_tokens = text_tokens(compaction["content"])
            usage = message["usage"]
            usage["iterations"] = [
                {"type": "compaction", "input_tokens": compaction_input, "output_tokens": summary_tokens,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                {"type": "message", "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                 "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
                 "cache_read_input_tokens": usage["cache_read_input_tokens"]},
            ]
        return message

    def _fallback(self, body: dict, effective: dict, headers: dict, declined_model: str,
                  declined_reply: Any, declined_cache: Any) -> dict:
        """Simulate server-side fallback: the fallback model re-runs the same request."""
        fb_body = dict(effective)
        fb_body["model"] = FALLBACK_MODEL
        fb_req = MockRequest(fb_body, headers)
        _, fb_reply = dispatch(fb_req)
        if fb_reply.stop_reason == "refusal":           # the policy refused again: pretend the fallback accepted
            from .reply import say
            fb_reply = say("(fallback model) I can help with that - here is a careful, policy-compliant answer.")
        fb_cache = self.cache.process(fb_body, get_spec(FALLBACK_MODEL))
        message = build_message(fb_req, fb_reply, fb_cache, model=FALLBACK_MODEL)
        category = (declined_reply.stop_details or {}).get("category")
        message["content"].insert(0, {"type": "fallback", "from": {"model": declined_model},
                                      "to": {"model": FALLBACK_MODEL},
                                      "trigger": {"type": "refusal", "category": category}})
        served = message["usage"]
        served_iteration = {k: served[k] for k in ("input_tokens", "output_tokens",
                                                     "cache_creation_input_tokens", "cache_read_input_tokens")}
        message["usage"]["iterations"] = [
            {"type": "message", "model": declined_model, "input_tokens": declined_cache.uncached_tokens,
             "output_tokens": 0, "cache_creation_input_tokens": declined_cache.write_tokens,
             "cache_read_input_tokens": declined_cache.read_tokens},
            {"type": "fallback_message", "model": FALLBACK_MODEL, **served_iteration},
        ]
        return message

    def _count_tokens(self, body: dict, headers: dict, request_id: str) -> httpx2.Response:
        if "model" not in body or "messages" not in body:
            raise bad_request("model and messages are required")
        if not known_model(body["model"]):
            raise ApiError(404, f"model: {body['model']}")
        total = sum(tokens for _, tokens, _ in render_positions(body))
        return self._json({"input_tokens": total}, request_id)

    # ------------------------------------------------------------------ batches
    def _batch_create(self, body: dict, headers: dict) -> dict:
        requests = body.get("requests")
        if not isinstance(requests, list) or not requests:
            raise bad_request("requests: at least one request is required")
        ids = [r.get("custom_id") for r in requests]
        if len(set(ids)) != len(ids):
            raise bad_request("requests: custom_id values must be unique within a batch")
        batch_id = next_id("msgbatch_mock_")
        results = []
        for item in requests:
            params = dict(item.get("params") or {})
            params.pop("stream", None)
            try:
                if "fallbacks" in params:
                    raise bad_request("fallbacks: not supported on the Message Batches API")
                message = self.create_message(params, headers)
                results.append({"custom_id": item["custom_id"], "result": {"type": "succeeded", "message": message}})
            except ApiError as exc:
                results.append({"custom_id": item["custom_id"], "result": {
                    "type": "errored", "error": {"type": "error", "error": {"type": exc.err_type, "message": exc.message}}}})
        created = _dt.datetime.now(_dt.timezone.utc)
        batch = {"id": batch_id, "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "expires_at": (created + _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "results": results[::-1],          # results arrive in ANY order - key them by custom_id
                 "polls": 0, "canceled": False}
        with self._lock:
            self._batches[batch_id] = batch
        return self._batch_view(batch)

    def _batch_view(self, batch: dict) -> dict:
        done = batch["polls"] > self.batch_polls_before_done or batch["canceled"]
        succeeded = sum(1 for r in batch["results"] if r["result"]["type"] == "succeeded")
        errored = len(batch["results"]) - succeeded
        view = {
            "id": batch["id"], "type": "message_batch",
            "processing_status": "ended" if done else "in_progress",
            "request_counts": {"processing": 0 if done else len(batch["results"]),
                               "succeeded": succeeded if done and not batch["canceled"] else 0,
                               "errored": errored if done and not batch["canceled"] else 0,
                               "canceled": len(batch["results"]) if batch["canceled"] else 0, "expired": 0},
            "created_at": batch["created_at"], "expires_at": batch["expires_at"],
            "ended_at": _now_iso() if done else None, "archived_at": None,
            "cancel_initiated_at": _now_iso() if batch["canceled"] else None,
            "results_url": f"https://api.anthropic.com/v1/messages/batches/{batch['id']}/results" if done else None,
        }
        return view

    def _batch_route(self, path: str, method: str, request_id: str) -> httpx2.Response:
        parts = path.split("/")          # ['', 'v1', 'messages', 'batches', id, (action)]
        batch_id = parts[4]
        batch = self._batches.get(batch_id)
        if batch is None:
            raise ApiError(404, f"batch not found: {batch_id}")
        action = parts[5] if len(parts) > 5 else None
        if action is None and method == "GET":
            with self._lock:
                batch["polls"] += 1
            return self._json(self._batch_view(batch), request_id)
        if action == "cancel" and method == "POST":
            batch["canceled"] = True
            return self._json(self._batch_view(batch), request_id)
        if action == "results" and method == "GET":
            if not (batch["polls"] > self.batch_polls_before_done or batch["canceled"]):
                raise bad_request("Batch is still processing; results are available once processing_status is 'ended'")
            rows = batch["results"]
            if batch["canceled"]:
                rows = [{"custom_id": r["custom_id"], "result": {"type": "canceled"}} for r in rows]
            content = "".join(json.dumps(r) + "\n" for r in rows).encode()
            return httpx2.Response(200, content=content, headers={"content-type": "application/x-jsonl",
                                                                   "request-id": request_id})
        raise ApiError(404, f"Not found: {method} {path}")

    # ------------------------------------------------------------------ models
    @staticmethod
    def _model_info(model: str) -> dict:
        spec = get_spec(model)
        return {
            "type": "model", "id": spec.id, "display_name": spec.display_name,
            "created_at": "2026-01-01T00:00:00Z",
            "max_input_tokens": spec.context_window, "max_tokens": spec.max_output,
            "capabilities": {
                "thinking": {"supported": True, "types": {
                    "adaptive": {"supported": spec.supports_adaptive},
                    "enabled": {"supported": spec.budget_tokens != "removed"}}},
                "effort": {"supported": bool(spec.effort_levels),
                           **{lvl: {"supported": lvl in spec.effort_levels}
                              for lvl in ("low", "medium", "high", "xhigh", "max")}},
                "structured_outputs": {"supported": True},
                "image_input": {"supported": True},
            },
        }

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _json(payload: dict, request_id: str) -> httpx2.Response:
        return httpx2.Response(200, json=payload, headers={"request-id": request_id})

    @staticmethod
    def _error(exc: ApiError, request_id: str) -> httpx2.Response:
        headers = {"request-id": request_id, **exc.headers}
        return httpx2.Response(exc.status, json=exc.body(request_id), headers=headers)


_API: MockAnthropicAPI | None = None
_API_LOCK = threading.Lock()


def get_mock_api() -> MockAnthropicAPI:
    """The process-wide mock (shared so the prompt cache persists across clients)."""
    global _API
    with _API_LOCK:
        if _API is None:
            _API = MockAnthropicAPI()
        return _API
