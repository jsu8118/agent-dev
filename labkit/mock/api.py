"""MockAnthropicAPI - an offline stand-in for the Claude API, served at the HTTP layer.

The Anthropic SDK is used unmodified: `labkit.get_client()` plugs this object into the
SDK as an httpx2 `MockTransport`, so requests are serialized, sent, parsed, retried and
streamed by the real SDK code paths.  Only the "model" is fake.

Endpoints: POST /v1/messages (JSON + SSE streaming), POST /v1/messages/count_tokens,
the Message Batches endpoints, GET /v1/models[/id], the Files API (/v1/files), and the
Managed Agents endpoints (/v1/agents, /v1/environments, /v1/sessions[...]/events[/stream]).

Server tools simulated inside /v1/messages: tool search (with deferred loading), code
execution (Python cells, bash, the text editor) and programmatic tool calling (a cell that
awaits your client tools pauses the response with `caller`-tagged tool_use blocks).

Teaching hooks:
  * `inject_faults(429, 529, ...)`  - the next N /v1/messages calls fail with those statuses
  * "[simulate:refusal]" in a user message makes refusal-capable models decline
    (and `fallbacks="default"` then re-runs the request on the fallback model)
  * `request_log` - every request body the mock received (handy in tests)
  * `cache.ready_delay` - seconds before a written cache entry can be read (concurrency lessons)
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import math
import os
import re
import threading
import time
from typing import Any, Callable

import httpx2

from ..models import CATALOG, get_spec, known_model
from . import signing
from .cache import PromptCache, render_positions
from .context import apply_context_management
from .errors import ApiError, bad_request
from .files import get_file_store
from .registry import ScenarioError, dispatch
from .render import build_message, next_id, to_sse
from .reply import Reply, refuse
from .request import MockRequest
from .sandbox import CALLER_TYPE, get_sandbox
from .tokens import text_tokens
from .validate import BINDING_BETA, validate_messages_request

REFUSAL_MARKER = "[simulate:refusal]"
FALLBACK_MODEL = "claude-opus-4-8"
CODE_RESULT_TYPES = {"code_execution": "code_execution_tool_result",
                     "bash_code_execution": "bash_code_execution_tool_result",
                     "text_editor_code_execution": "text_editor_code_execution_tool_result"}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


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
        self._agents = None            # the Managed Agents mock, created on first use

    # ------------------------------------------------------------------ controls
    def reset(self) -> None:
        with self._lock:
            self.cache.clear()
            self.cache.ready_delay = 0.0
            self.request_log.clear()
            self._faults.clear()
            self._batches.clear()
            self.last_scenario = None
        get_sandbox().reset()
        get_file_store().clear()
        if self._agents is not None:
            self._agents.reset()

    def inject_faults(self, *statuses: int) -> None:
        """Make the next len(statuses) /v1/messages requests fail with these HTTP statuses."""
        with self._lock:
            self._faults.extend(statuses)

    @property
    def agents(self):
        if self._agents is None:
            from .agents import ManagedAgentsMock
            self._agents = ManagedAgentsMock(self)
        return self._agents

    # ------------------------------------------------------------------ transport entry point
    def handle(self, request: httpx2.Request) -> httpx2.Response:
        request_id = next_id("req_mock_")
        headers = {k.lower(): v for k, v in request.headers.items()}
        path, method = request.url.path.rstrip("/"), request.method.upper()
        try:
            if path.startswith("/v1/files"):
                return get_file_store().route(request, request_id)
            if path.startswith(("/v1/agents", "/v1/environments", "/v1/sessions")):
                return self.agents.route(request, request_id)
            try:
                body: Any = json.loads(request.content) if request.content else {}
            except ValueError:
                return self._error(ApiError(400, "Request body is not valid JSON"), request_id)
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
            raise ApiError(404, f"Not found: {method} {path}")
        except ApiError as exc:
            return self._error(exc, request_id)
        except ScenarioError as exc:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": str(exc)},
                                              "request_id": request_id},
                                   headers={"request-id": request_id, "x-should-retry": "false"})

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
        verdict = validate_messages_request(body, headers, lenient=lenient)
        with self._lock:
            self.request_log.append(body)
            del self.request_log[:-200]
        spec = get_spec(body["model"])
        betas = {b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()}

        # Blocks the API drops before generation (model / prefix binding) don't reach the model or the bill.
        received = _without_dropped(body, verdict.dropped) if verdict.dropped else body
        effective, applied_edits, compaction, compaction_input = apply_context_management(received)
        cache = self.cache.process(effective, spec)
        if cache.total_tokens > spec.context_window:
            raise bad_request(f"prompt is too long: {cache.total_tokens} tokens > {spec.context_window} maximum")

        req = MockRequest(effective, headers, raw_body=body)
        if body.get("max_tokens") == 0:                 # cache pre-warming: prefill only, no generation
            message = build_message(req, Reply(content=[], stop_reason="max_tokens"), cache)
            message["content"] = []
            message["usage"]["output_tokens"] = 0
            return self._finish(message, betas, verdict, spec)

        sandbox = get_sandbox()
        uploads = [b.get("file_id") for b in _blocks(req.messages[-1].get("content")) if b.get("type") == "container_upload"]
        container = sandbox.container(req.container_id) if (req.container_id or uploads) else None
        if uploads and container is not None:
            sandbox.mount_files(container, uploads)

        # A paused code cell resumes when its client tool results arrive.
        if container is not None and container.paused_cell and req.pending_code_calls:
            sandbox.provide_results(container, [(c.name, c.input, c.result or "") for c in req.pending_code_calls])
            outcome = sandbox.resume(container)
            cell = container.paused_cell or {}
            if outcome.paused:                        # the cell asked for more tools: no model work, so no thinking
                reply = Reply(content=self._pending_tool_uses(cell.get("id") or "", outcome.calls), stop_reason="tool_use",
                              complexity=0.0)
                reply.container = container.info()
                message = build_message(req, reply, cache, cleared_edits=applied_edits)
                return self._finish(message, betas, verdict, spec)
            result_block = {"type": "code_execution_tool_result", "tool_use_id": self._last_cell_id(req),
                            "content": {"type": "code_execution_result", "stdout": outcome.stdout,
                                        "stderr": outcome.stderr, "return_code": outcome.return_code, "content": []}}
            req._completed_code = result_block
            name, reply = dispatch(req)
            self.last_scenario = name
            reply.content = [result_block] + list(reply.content)
            reply.container = container.info()
            message = build_message(req, reply, cache, cleared_edits=applied_edits)
            return self._finish(message, betas, verdict, spec)

        name, reply = dispatch(req)
        refusal_requested = REFUSAL_MARKER in (req.last_user_text + req.first_user_text)
        if refusal_requested and spec.refusal_classifiers and reply.stop_reason != "refusal":
            reply = refuse("cyber", "Simulated classifier decline (the lab asked for one).")
        self.last_scenario = name

        if reply.stop_reason == "refusal" and body.get("fallbacks"):
            return self._finish(self._fallback(body, effective, headers, spec.id, reply, cache), betas, verdict, spec)

        reply = self._run_server_tools(req, reply, container)
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
        return self._finish(message, betas, verdict, spec)

    @staticmethod
    def _finish(message: dict, betas: set[str], verdict, spec) -> dict:
        if BINDING_BETA in betas and spec.thinking_default != "off":
            message["input_transformations"] = list(verdict.transformations)
        return message

    @staticmethod
    def _last_cell_id(req: MockRequest) -> str:
        for m in reversed(req.messages):
            if m.get("role") == "assistant":
                for b in reversed(_blocks(m.get("content"))):
                    if b.get("type") == "server_tool_use" and b.get("name") == "code_execution":
                        return b.get("id", "")
        return ""

    @staticmethod
    def _pending_tool_uses(cell_id: str, calls: list[dict]) -> list[dict]:
        blocks = []
        for call in calls:
            seed = json.dumps([cell_id, call["name"], call["input"]], sort_keys=True, default=str)
            blocks.append({"type": "tool_use", "id": signing.TOOL_ID_PREFIX + _short_hash(seed), "name": call["name"],
                           "input": call["input"], "caller": {"type": CALLER_TYPE, "tool_id": cell_id}})
        return blocks

    # ------------------------------------------------------------------ server tools inside a reply
    def _run_server_tools(self, req: MockRequest, reply: Reply, container, depth: int = 0,
                          prior: list[dict] | None = None) -> Reply:
        """Execute the server_tool_use blocks a scenario put in its reply, in place, and append their results.

        Like the API's server-side loop, generation continues in the same response after a server tool
        returns: when the reply ends on a server-tool result, the scenario is asked again (with the result
        visible in the history) for what the model says or calls next.
        """
        if not any(b.get("type") == "server_tool_use" for b in reply.content):
            return reply
        sandbox = get_sandbox()
        prior = list(prior or [])                          # blocks generated earlier in this same response
        out: list[dict] = []
        results: list[dict] = []
        loaded = set(req.loaded_tool_names)
        for block in prior:                                # tools discovered earlier in this response count as loaded
            if block.get("type") == "tool_search_tool_result":
                loaded |= {r["tool_name"] for r in (block.get("content") or {}).get("tool_references") or []}
        paused = False
        for block in reply.content:
            if paused:
                break
            if block.get("type") != "server_tool_use":
                if block.get("type") == "tool_use" and req.deferred_tool_names and block.get("name") in req.deferred_tool_names \
                        and block.get("name") not in loaded:
                    raise ScenarioError(f"scenario called deferred tool {block.get('name')!r} before discovering it "
                                        "(search for it with search_tools() first)")
                out.append(block)
                continue
            name = block.get("name")
            cell_id = signing.SERVER_TOOL_ID_PREFIX + _short_hash(json.dumps([len(req.messages), len(out), block], default=str))
            if name == "tool_search":
                search_tool = req.server_tools.get("tool_search")
                if search_tool is None:
                    raise ScenarioError("scenario used search_tools() but the request declares no tool search tool")
                variant = "regex" if "regex" in search_tool["type"] else "bm25"
                query = block["input"].get("query", "")
                limit = block["input"].get("limit")
                use = {"type": "server_tool_use", "id": cell_id, "name": search_tool.get("name") or f"tool_search_tool_{variant}",
                       "input": {("pattern" if variant == "regex" else "query"): query, **({"limit": limit} if limit else {})}}
                result = _search_tools(req, variant, query, limit or 5)
                out.append(use)
                out.append({"type": "tool_search_tool_result", "tool_use_id": cell_id, "content": result})
                if result.get("type") == "tool_search_tool_search_result":
                    loaded |= {r["tool_name"] for r in result["tool_references"]}
                results.append(out[-1])
                continue
            if req.server_tools.get("code_execution") is None:
                raise ScenarioError(f"scenario used {name} but the request declares no code execution tool")
            if container is None:
                container = sandbox.container(req.container_id)
            reply.container = container.info()
            use = {"type": "server_tool_use", "id": cell_id, "name": name, "input": block["input"]}
            out.append(use)
            if name == "code_execution":
                outcome = sandbox.run_cell(container, cell_id, block["input"].get("code", ""), req.code_callable_tools)
                if outcome.paused:
                    out.extend(self._pending_tool_uses(cell_id, outcome.calls))
                    paused = True
                    continue
                result_block = {"type": "code_execution_tool_result", "tool_use_id": cell_id,
                                "content": {"type": "code_execution_result", "stdout": outcome.stdout,
                                            "stderr": outcome.stderr, "return_code": outcome.return_code, "content": []}}
            elif name == "bash_code_execution":
                result_block = {"type": "bash_code_execution_tool_result", "tool_use_id": cell_id,
                                "content": sandbox.run_bash(container, block["input"].get("command", ""))}
            elif name == "text_editor_code_execution":
                inputs = dict(block["input"])
                command, path = inputs.pop("command", "view"), inputs.pop("path", "")
                result_block = {"type": "text_editor_code_execution_tool_result", "tool_use_id": cell_id,
                                "content": sandbox.run_editor(container, command, path, **inputs)}
            else:
                raise ScenarioError(f"unknown server tool {name!r} in scenario reply")
            out.append(result_block)
            results.append(result_block)
        new = Reply(content=out, stop_reason="tool_use" if paused else reply.stop_reason,
                    stop_details=reply.stop_details, thinking_summary=reply.thinking_summary,
                    complexity=reply.complexity, progress=reply.progress, container=reply.container)
        if paused:
            return new
        ends_on_result = bool(out) and out[-1].get("type") in ("tool_search_tool_result", *CODE_RESULT_TYPES.values())
        if reply.continuation is not None:
            follow = reply.continuation(results)
        elif ends_on_result and depth < 6:
            # The model keeps going after a server tool returns: ask the scenario what comes next.
            body = {**req.body, "messages": list(req.messages) + [{"role": "assistant", "content": prior + out}]}
            follow_req = MockRequest(body, req.headers, raw_body=req.raw_body)
            follow_req._partial_response = prior + out
            if out[-1].get("type") == "code_execution_tool_result":
                follow_req._completed_code = out[-1]
            _, follow = dispatch(follow_req)
        else:
            return new
        follow = self._run_server_tools(req, follow, container, depth + 1, prior=prior + out)
        new.content.extend(follow.content)
        new.stop_reason = follow.stop_reason
        new.container = new.container or follow.container
        new.progress = (new.progress or []) + (follow.progress or []) or None
        return new

    # ------------------------------------------------------------------ fallbacks
    def _fallback(self, body: dict, effective: dict, headers: dict, declined_model: str,
                  declined_reply: Any, declined_cache: Any) -> dict:
        """Simulate server-side fallback: the fallback model re-runs the same request."""
        fb_body = dict(effective)
        fb_body["model"] = FALLBACK_MODEL
        fb_req = MockRequest(fb_body, headers, raw_body={**body, "model": FALLBACK_MODEL})
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
        effective = apply_context_management({**body, "max_tokens": 1})[0]
        total = sum(tokens for _, tokens, _ in render_positions(effective))
        payload: dict[str, Any] = {"input_tokens": total}
        if effective is not body:
            payload["context_management"] = {"original_input_tokens": sum(t for _, t, _ in render_positions(body))}
        return self._json(payload, request_id)

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
                "tool_search": {"supported": spec.tool_search},
                "code_execution": {"supported": spec.code_execution},
                "programmatic_tool_calling": {"supported": spec.programmatic_tool_calling},
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


# ---------------------------------------------------------------------------- helpers
def _short_hash(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()[:20]


def _without_dropped(body: dict, dropped: set[tuple[int, int]]) -> dict:
    """The request minus the thinking blocks the API drops (they never reach the model or the bill)."""
    new = dict(body)
    messages = []
    for i, m in enumerate(body.get("messages") or []):
        content = m.get("content")
        if isinstance(content, list) and any(x == i for x, _ in dropped):
            kept = [b for j, b in enumerate(content) if (i, j) not in dropped]
            messages.append({**m, "content": kept})
        else:
            messages.append(m)
    new["messages"] = messages
    return new


def _haystack(tool: dict) -> str:
    parts = [tool.get("name", ""), tool.get("description", "")]
    for arg, schema in ((tool.get("input_schema") or {}).get("properties") or {}).items():
        parts.append(arg)
        if isinstance(schema, dict):
            parts.append(str(schema.get("description", "")))
    return " ".join(parts)


def _search_tools(req: MockRequest, variant: str, query: str, limit: int) -> dict:
    """Run a tool search over the deferred catalog like the API: regex (Python re.search, case-insensitive) or BM25."""
    catalog = [t for t in req.tools if t.get("defer_loading") and t.get("name")]
    if variant == "regex":
        if len(query) > 200:
            return {"type": "tool_search_tool_result_error", "error_code": "invalid_tool_input",
                    "error_message": "pattern exceeds the 200-character maximum"}
        try:
            pattern = re.compile(query, re.I)
        except re.error as exc:
            return {"type": "tool_search_tool_result_error", "error_code": "invalid_tool_input",
                    "error_message": f"Invalid regular expression pattern: {exc}"}
        hits = [t["name"] for t in catalog if pattern.search(_haystack(t))]
    else:
        if len(query) > 500:
            return {"type": "tool_search_tool_result_error", "error_code": "invalid_tool_input",
                    "error_message": "query exceeds the 500-character maximum"}
        terms = re.findall(r"[a-z0-9_]+", query.lower())
        docs = [(t["name"], re.findall(r"[a-z0-9_]+", _haystack(t).lower())) for t in catalog]
        n = len(docs) or 1
        avg = sum(len(d) for _, d in docs) / n if docs else 1
        df = {term: sum(1 for _, d in docs if term in d) for term in set(terms)}
        scored = []
        for name, doc in docs:
            score = 0.0
            for term in terms:
                tf = doc.count(term)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(doc) / (avg or 1)))
            if score > 0:
                scored.append((score, name))
        hits = [name for _, name in sorted(scored, key=lambda x: (-x[0], x[1]))]
    return {"type": "tool_search_tool_search_result",
            "tool_references": [{"type": "tool_reference", "tool_name": name} for name in hits[:limit]]}


_API: MockAnthropicAPI | None = None
_API_LOCK = threading.Lock()


def get_mock_api() -> MockAnthropicAPI:
    global _API
    with _API_LOCK:
        if _API is None:
            _API = MockAnthropicAPI()
        return _API
