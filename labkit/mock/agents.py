"""Managed Agents, simulated: agents, environments, sessions, events, custom tools and coordinator rosters.

What the real product does: you create an *agent* (model, system prompt, tools), an *environment*
(container template) and a *session*; you send `user.message` events; Anthropic runs the agent loop and
the container, and the session emits an event stream (`agent.message`, `agent.tool_use`,
`agent.custom_tool_use`, `session.status_idle`, ...).  A custom tool pauses the session
(`stop_reason: requires_action`) until you send `user.custom_tool_result`.  A coordinator agent has a
roster of agents it delegates to through threads.

What the mock does: the same objects and events, served at the same paths, with the agent loop driven by
the scenario policies (`labkit.mock.registry.dispatch`), so a session behaves like the scenario written
for it.  Built-in `agent_toolset_20260401` tools (bash, read, write, edit, glob, grep) run in a per-session
workspace directory.  Processing is synchronous: `events.send` returns once the agent is idle again, and
`events.stream` replays the history as SSE (it waits briefly for new events, so a stream opened before a
send from another thread also sees them).  Session budgets pause the session at the cap.
"""

from __future__ import annotations

import datetime as dt
import glob as globlib
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs

import httpx2

from ..config import runs_dir
from ..models import get_spec, known_model
from ..pricing import cost_usd
from .errors import ApiError, bad_request
from .render import next_id
from .request import MockRequest

BETA = "managed-agents-2026-04-01"
MAX_REQUESTS_PER_TURN = 12
BUILTIN_TOOLS: list[dict] = [
    {"name": "bash", "description": "Run a shell command in the session container.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read", "description": "Read a file from the container.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write", "description": "Write a file in the container.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit", "description": "Replace text in a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_str": {"type": "string"},
                                                       "new_str": {"type": "string"}}, "required": ["path", "old_str", "new_str"]}},
    {"name": "glob", "description": "Find files by pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "grep", "description": "Search file contents.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                      "required": ["pattern"]}},
]
COORDINATOR_TOOLS: list[dict] = [
    {"name": "list_agents", "description": "List the agents in the roster you can delegate to.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_to_agent", "description": "Send a task or message to a roster agent (starts a thread, or continues "
                                             "one); returns the agent's reply.",
     "input_schema": {"type": "object", "properties": {"agent": {"type": "string"}, "message": {"type": "string"},
                                                       "thread_id": {"type": "string"}}, "required": ["agent", "message"]}},
]


def _ts() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Thread:
    id: str
    agent: dict
    parent_id: str | None
    name: str
    conversation: list[dict] = field(default_factory=list)      # Messages-API-shaped history
    status: str = "idle"
    events: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0,
                                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
    cost: float = 0.0


@dataclass
class Session:
    id: str
    agent: dict
    environment_id: str
    title: str | None
    metadata: dict
    budget: dict | None
    created_at: str
    status: str = "idle"
    events: list[dict] = field(default_factory=list)
    threads: dict[str, Thread] = field(default_factory=dict)
    primary: Thread | None = None
    pending_tool_uses: dict[str, dict] = field(default_factory=dict)   # custom_tool_use event id -> {thread, block}
    archived_at: str | None = None
    workdir: Path | None = None
    cost: float = 0.0
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0,
                                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
    cond: threading.Condition = field(default_factory=threading.Condition)

    def dir(self) -> Path:
        if self.workdir is None:
            self.workdir = runs_dir("mock_sessions", self.id)
        return self.workdir

    def view(self) -> dict:
        return {
            "type": "session", "id": self.id, "title": self.title, "status": self.status,
            "created_at": self.created_at, "updated_at": _ts(), "archived_at": self.archived_at,
            "environment_id": self.environment_id, "agent": _session_agent(self.agent),
            "resources": [], "metadata": self.metadata, "vault_ids": [], "outcome_evaluations": [],
            "budget": self.budget, "deployment_id": None,
            "usage": {**self.usage, "list_cost": {"amount": str(int(round(self.cost * 100))), "currency": "USD"},
                      "active_seconds": 0.0},
            "stats": {"active_seconds": 0.0, "duration_seconds": 0.0},
        }


def _session_agent(agent: dict, *, with_roster: bool = True) -> dict:
    snapshot = {k: agent[k] for k in ("id", "name", "description", "model", "system", "tools", "mcp_servers", "skills",
                                      "version")}
    snapshot["type"] = "agent"
    if agent.get("multiagent") and with_roster:
        members = []
        for entry in agent.get("_roster") or []:
            if entry.get("type") == "advisor":
                members.append(entry)
            else:            # a full agent dict (roster member) or the coordinator itself ("self")
                members.append(_session_agent(entry, with_roster=False))
        snapshot["multiagent"] = {"type": "coordinator", "agents": members}
    return snapshot


class ManagedAgentsMock:
    def __init__(self, api) -> None:
        self.api = api
        self.agents: dict[str, dict] = {}
        self.environments: dict[str, dict] = {}
        self.sessions: dict[str, Session] = {}
        self.stream_grace = 0.4                     # seconds a stream waits for new events before closing
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.agents.clear()
            self.environments.clear()
            self.sessions.clear()

    # ------------------------------------------------------------------ routing
    def route(self, request: httpx2.Request, request_id: str) -> httpx2.Response:
        path, method = request.url.path.rstrip("/"), request.method.upper()
        query = parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes) else str(request.url.query))
        headers = {k.lower(): v for k, v in request.headers.items()}
        betas = {b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()}
        if BETA not in betas:
            raise bad_request(f"Managed Agents endpoints require the anthropic-beta header {BETA!r}")
        body: Any = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                raise bad_request("Request body is not valid JSON")
        parts = path.split("/")[1:]                 # ['v1', 'agents', ...]
        resource = parts[1] if len(parts) > 1 else ""
        rid = parts[2] if len(parts) > 2 else None
        action = parts[3] if len(parts) > 3 else None

        if resource == "agents":
            return self._json(self._agents_route(method, rid, action, body), request_id)
        if resource == "environments":
            return self._json(self._environments_route(method, rid, action, body), request_id)
        if resource == "sessions":
            if rid and action == "events":
                sub = parts[4] if len(parts) > 4 else None
                session = self._session(rid)
                if sub == "stream" and method == "GET":
                    deltas = query.get("event_deltas[]", []) + query.get("event_deltas", [])
                    return httpx2.Response(200, stream=_EventStream(self, session, bool(deltas)),
                                           headers={"content-type": "text/event-stream", "request-id": request_id})
                if method == "GET":
                    return self._json({"data": list(session.events), "next_page": None}, request_id)
                if method == "POST":
                    return self._json({"data": self.send_events(session, body.get("events") or [])}, request_id)
            return self._json(self._sessions_route(method, rid, action, body), request_id)
        raise ApiError(404, f"Not found: {method} {path}")

    @staticmethod
    def _json(payload: dict, request_id: str) -> httpx2.Response:
        return httpx2.Response(200, json=payload, headers={"request-id": request_id})

    # ------------------------------------------------------------------ agents
    def _agents_route(self, method: str, rid: str | None, action: str | None, body: dict) -> dict:
        if rid is None and method == "POST":
            return self.create_agent(body)
        if rid is None and method == "GET":
            return {"data": [{k: v for k, v in a.items() if not k.startswith("_")} for a in self.agents.values()],
                    "next_page": None}
        agent = self.agents.get(rid or "")
        if agent is None:
            raise ApiError(404, f"agent not found: {rid}")
        public = {k: v for k, v in agent.items() if not k.startswith("_")}
        if action == "archive" and method == "POST":
            agent["archived_at"] = _ts()
            return {**public, "archived_at": agent["archived_at"]}
        if action == "versions" and method == "GET":
            return {"data": [public], "next_page": None}
        if action is None and method == "POST":
            for key in ("name", "description", "system", "tools", "model", "mcp_servers", "skills", "metadata"):
                if key in body:
                    agent[key] = self._normalize_model(body[key]) if key == "model" else body[key]
            agent["version"] += 1
            agent["updated_at"] = _ts()
            return {k: v for k, v in agent.items() if not k.startswith("_")}
        if action is None and method == "GET":
            return public
        raise ApiError(404, f"Not found: {method} /v1/agents/{rid}")

    @staticmethod
    def _normalize_model(model: Any) -> dict:
        config = {"id": model} if isinstance(model, str) else dict(model or {})
        if not known_model(config.get("id", "")):
            raise bad_request(f"model: unknown model {config.get('id')!r}")
        effort = config.get("effort")
        if isinstance(effort, str):
            config["effort"] = {"type": effort}
        config.setdefault("speed", "standard")
        config.setdefault("inference_geo", None)
        return config

    def create_agent(self, body: dict) -> dict:
        name = body.get("name")
        if not name or not isinstance(name, str) or len(name) > 256:
            raise bad_request("name: String should have at most 256 characters")
        if "model" not in body:
            raise bad_request("model: Field required")
        tools = body.get("tools") or []
        if len(tools) > 128:
            raise bad_request("tools: at most 128 entries")
        for i, t in enumerate(tools):
            kind = t.get("type") if isinstance(t, dict) else None
            if kind == "custom":
                if not t.get("name") or not isinstance(t.get("input_schema"), dict):
                    raise bad_request(f"tools.{i}: custom tools need a name and an input_schema")
            elif kind not in ("agent_toolset_20260401", "mcp_toolset"):
                raise bad_request(f"tools.{i}.type: expected 'agent_toolset_20260401', 'mcp_toolset' or 'custom' (got {kind!r})")
        multiagent = body.get("multiagent")
        resolved: list[Any] = []
        roster: list[Any] = []
        if multiagent:
            entries = multiagent.get("agents") or []
            if not 1 <= len(entries) <= 20:
                raise bad_request("multiagent.agents: 1 to 20 entries")
            for entry in entries:
                if isinstance(entry, str):
                    entry = {"type": "agent", "id": entry}
                if entry.get("type") == "agent":
                    member = self.agents.get(entry.get("id", ""))
                    if member is None:
                        raise bad_request(f"multiagent.agents: unknown agent {entry.get('id')!r}")
                    if member.get("multiagent"):
                        raise bad_request("multiagent.agents: only one level of delegation is allowed; "
                                          f"{member['name']!r} carries its own roster")
                    resolved.append({**entry, "version": entry.get("version") or member["version"]})
                    roster.append(member)
                elif entry.get("type") in ("self", "advisor"):
                    resolved.append(entry)
                    roster.append(entry)
                else:
                    raise bad_request("multiagent.agents: entries are agent ids, {type: agent}, {type: self} or {type: advisor}")
        agent = {
            "type": "agent", "id": next_id("agent_mock_"), "name": name, "description": body.get("description"),
            "model": self._normalize_model(body["model"]), "system": body.get("system"),
            "tools": [{"type": "agent_toolset_20260401", "configs": [], "default_config": {"permission_policy": {"type": "always_allow"}}}
                      if t.get("type") == "agent_toolset_20260401" else t for t in tools],
            "mcp_servers": body.get("mcp_servers") or [], "skills": body.get("skills") or [],
            "metadata": body.get("metadata") or {}, "version": 1, "created_at": _ts(), "updated_at": _ts(),
            "archived_at": None,
            "multiagent": {"type": "coordinator", "agents": resolved} if multiagent else None,
        }
        # Full member definitions for delegation and session snapshots (not part of the API object).
        agent["_roster"] = [agent if isinstance(m, dict) and m.get("type") == "self" else m for m in roster]
        with self._lock:
            self.agents[agent["id"]] = agent
        return {k: v for k, v in agent.items() if not k.startswith("_")}

    # ------------------------------------------------------------------ environments
    def _environments_route(self, method: str, rid: str | None, action: str | None, body: dict) -> dict:
        if rid is None and method == "POST":
            if not body.get("name"):
                raise bad_request("name: Field required")
            config = body.get("config") or {"type": "cloud"}
            if config.get("type") == "cloud":
                config = {"type": "cloud", "networking": config.get("networking") or {"type": "unrestricted"},
                          "packages": config.get("packages") or {}}
            env = {"type": "environment", "id": next_id("env_mock_"), "name": body["name"],
                   "description": body.get("description"), "config": config, "metadata": body.get("metadata") or {},
                   "created_at": _ts(), "updated_at": _ts(), "archived_at": None, "scope": "organization"}
            with self._lock:
                self.environments[env["id"]] = env
            return env
        if rid is None and method == "GET":
            return {"data": list(self.environments.values()), "next_page": None}
        env = self.environments.get(rid or "")
        if env is None:
            raise ApiError(404, f"environment not found: {rid}")
        if action == "archive":
            env["archived_at"] = _ts()
        if method == "DELETE":
            del self.environments[rid]
            return {"id": rid, "type": "environment_deleted"}
        return env

    # ------------------------------------------------------------------ sessions
    def _session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise ApiError(404, f"session not found: {session_id}")
        return session

    def _sessions_route(self, method: str, rid: str | None, action: str | None, body: dict) -> dict:
        if rid is None and method == "POST":
            return self.create_session(body).view()
        if rid is None and method == "GET":
            return {"data": [s.view() for s in self.sessions.values()], "next_page": None}
        session = self._session(rid or "")
        if action == "archive" and method == "POST":
            session.archived_at = _ts()
            return session.view()
        if action == "threads" and method == "GET":
            return {"data": [self._thread_view(t) for t in session.threads.values()], "next_page": None}
        if action is None and method == "DELETE":
            del self.sessions[session.id]
            return {"id": session.id, "type": "session_deleted"}
        if action is None and method == "POST":
            if "title" in body:
                session.title = body["title"]
            if "metadata" in body:
                session.metadata = body["metadata"]
            if "budget" in body:
                session.budget = body["budget"]
            self._emit(session, {"type": "session.updated", **{k: body[k] for k in ("title", "metadata", "budget") if k in body}})
            return session.view()
        return session.view()

    def create_session(self, body: dict) -> Session:
        agent_ref = body.get("agent")
        if not agent_ref:
            raise bad_request("agent: Field required (create the agent first with client.beta.agents.create)")
        env_id = body.get("environment_id")
        if not env_id or env_id not in self.environments:
            raise bad_request(f"environment_id: unknown environment {env_id!r}")
        overrides: dict = {}
        if isinstance(agent_ref, dict):
            if agent_ref.get("type") == "agent_with_overrides":
                overrides = {k: v for k, v in agent_ref.items() if k in ("model", "system", "tools", "mcp_servers", "skills")}
            agent_ref = agent_ref.get("id")
        base = self.agents.get(agent_ref or "")
        if base is None:
            raise bad_request(f"agent: unknown agent {agent_ref!r}")
        if base.get("archived_at"):
            raise bad_request("agent: this agent is archived; new sessions cannot reference it")
        agent = dict(base)
        for key, value in overrides.items():
            agent[key] = self._normalize_model(value) if key == "model" else value
        budget = body.get("budget")
        if budget is not None:
            amount = (budget.get("max_list_cost") or {}).get("amount")
            if budget.get("type") != "limit" or not isinstance(amount, str) or not amount.isdigit():
                raise bad_request('budget: expected {"type": "limit", "max_list_cost": {"amount": "<cents>", "currency": "USD"}}')
        session = Session(id=next_id("session_mock_"), agent=agent, environment_id=env_id, title=body.get("title"),
                          metadata=body.get("metadata") or {}, budget=budget, created_at=_ts())
        primary = Thread(id=next_id("sthread_mock_"), agent=agent, parent_id=None, name=agent["name"])
        session.primary = primary
        session.threads[primary.id] = primary
        with self._lock:
            self.sessions[session.id] = session
        initial = body.get("initial_events") or []
        if len(initial) > 50:
            raise bad_request("initial_events: at most 50 events")
        for event in initial:
            if event.get("type") not in ("user.message", "user.define_outcome"):
                raise bad_request("initial_events: only user.message and user.define_outcome are accepted")
        if initial:
            self.send_events(session, initial)
        return session

    def _thread_view(self, thread: Thread) -> dict:
        return {"type": "session_thread", "id": thread.id, "status": thread.status, "parent_thread_id": thread.parent_id,
                "agent": {**_session_agent(thread.agent), "type": "agent"}, "archived_at": None,
                "usage": {**thread.usage, "list_cost": {"amount": str(int(round(thread.cost * 100))), "currency": "USD"}},
                "stats": {"active_seconds": 0.0}}

    # ------------------------------------------------------------------ events
    def _emit(self, session: Session, event: dict, *, processed: bool = True) -> dict:
        event = {"id": next_id("sevt_mock_"), **event}
        if processed:
            event.setdefault("processed_at", _ts())
        with session.cond:
            session.events.append(event)
            session.cond.notify_all()
        return event

    def send_events(self, session: Session, events: list[dict]) -> list[dict]:
        if session.archived_at:
            raise bad_request("session is archived (read-only)")
        echoed: list[dict] = []
        for event in events:
            kind = event.get("type")
            if kind == "user.message":
                content = event.get("content") or []
                if not content:
                    raise bad_request("events: user.message needs content")
                if session.status == "idle" and session.pending_tool_uses:
                    raise bad_request("the session is waiting for user.custom_tool_result events (stop_reason "
                                      "requires_action); resolve them before sending a user.message")
                stored = self._emit(session, {"type": "user.message", "content": content})
                echoed.append(stored)
                self._run_turn(session, [{"role": "user", "content": content}])
            elif kind == "user.custom_tool_result":
                use_id = event.get("custom_tool_use_id")
                pending = session.pending_tool_uses.pop(use_id or "", None)
                if pending is None:
                    raise bad_request(f"custom_tool_use_id: no pending custom tool use {use_id!r}")
                stored = self._emit(session, {"type": "user.custom_tool_result", "custom_tool_use_id": use_id,
                                              "content": event.get("content") or [], "is_error": bool(event.get("is_error"))})
                echoed.append(stored)
                thread = pending["thread"]
                text = "\n".join(b.get("text", "") for b in event.get("content") or [] if isinstance(b, dict))
                thread.conversation.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": pending["block"]["id"], "content": text,
                     "is_error": bool(event.get("is_error"))}]})
                if not session.pending_tool_uses:
                    self._run_turn(session, [], thread=thread)
            elif kind == "system.message":
                content = event.get("content") or []
                if not 1 <= len(content) <= 1000:
                    raise bad_request("system.message: content accepts 1-1000 text items")
                if not get_spec(session.agent["model"]["id"]).mid_conversation_system:
                    raise bad_request("model_does_not_support_mid_conversation_system")
                stored = self._emit(session, {"type": "system.message", "content": content})
                echoed.append(stored)
                session.primary.conversation.append({"role": "system", "content": content})
            elif kind == "user.interrupt":
                echoed.append(self._emit(session, {"type": "user.interrupt"}))
            elif kind == "user.define_outcome":
                if not event.get("rubric"):
                    raise bad_request("user.define_outcome: rubric is required")
                stored = self._emit(session, {"type": "user.define_outcome", "description": event.get("description"),
                                              "rubric": event["rubric"], "max_iterations": event.get("max_iterations", 3),
                                              "outcome_id": next_id("outcome_mock_")})
                echoed.append(stored)
            else:
                raise bad_request(f"events: unknown event type {kind!r}")
        return echoed

    # ------------------------------------------------------------------ the agent loop
    def _budget_cents(self, session: Session) -> int | None:
        if not session.budget:
            return None
        return int((session.budget.get("max_list_cost") or {}).get("amount", "0"))

    def _run_turn(self, session: Session, new_messages: list[dict], *, thread: Thread | None = None) -> None:
        thread = thread or session.primary
        thread.conversation.extend(new_messages)
        session.status = thread.status = "running"
        self._emit(session, {"type": "session.status_running"})
        if thread is not session.primary:
            self._emit(session, {"type": "session.thread_status_running", "session_thread_id": thread.id,
                                 "agent_name": thread.name})
        stop = self._loop(session, thread)
        thread.status = "idle"
        if thread is not session.primary:
            self._emit(session, {"type": "session.thread_status_idle", "session_thread_id": thread.id,
                                 "agent_name": thread.name, "stop_reason": stop})
        session.status = "idle"
        self._emit(session, {"type": "session.status_idle", "stop_reason": stop})

    def _loop(self, session: Session, thread: Thread) -> dict:
        agent = thread.agent
        tools = self._tool_definitions(agent)
        for _ in range(MAX_REQUESTS_PER_TURN):
            cap = self._budget_cents(session)
            if cap is not None and session.cost * 100 >= cap:
                return {"type": "budget_reached"}
            body = {"model": agent["model"]["id"], "max_tokens": 8000, "system": agent.get("system") or "",
                    "tools": tools, "messages": list(thread.conversation)}
            if (agent["model"].get("effort") or {}).get("type"):
                body["output_config"] = {"effort": agent["model"]["effort"]["type"]}
            start = self._emit(session, {"type": "span.model_request_start"})
            message = self.api.create_message(body, {"anthropic-beta": BETA, "user-agent": "labkit-managed-agents"})
            usage = message["usage"]
            for key in thread.usage:
                thread.usage[key] += int(usage.get(key) or 0)
                session.usage[key] += int(usage.get(key) or 0)
            cost = cost_usd(usage, message["model"])
            thread.cost += cost
            session.cost += cost
            self._emit(session, {"type": "span.model_request_end", "model_request_start_id": start["id"], "is_error": False,
                                 "model_usage": {k: int(usage.get(k) or 0) for k in
                                                 ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                  "cache_read_input_tokens")}})
            self._emit(session, {"type": "session.usage", "usage": {**session.usage, "list_cost": {
                "amount": str(int(round(session.cost * 100))), "currency": "USD"}}, "budget": session.budget})
            thread.conversation.append({"role": "assistant", "content": message["content"]})
            texts = [b["text"] for b in message["content"] if b.get("type") == "text" and b.get("text")]
            uses = [b for b in message["content"] if b.get("type") == "tool_use"]
            if any(b.get("type") == "thinking" for b in message["content"]):
                self._emit(session, {"type": "agent.thinking"})
            if texts:
                self._emit(session, {"type": "agent.message", "content": [{"type": "text", "text": t} for t in texts]})
            if message.get("stop_reason") != "tool_use" or not uses:
                return {"type": "end_turn"}
            results: list[dict] = []
            waiting: list[str] = []
            for block in uses:
                kind = self._tool_kind(agent, block["name"])
                if kind == "custom":
                    event = self._emit(session, {"type": "agent.custom_tool_use", "name": block["name"], "input": block["input"],
                                                 **({"session_thread_id": thread.id} if thread is not session.primary else {})})
                    session.pending_tool_uses[event["id"]] = {"thread": thread, "block": block}
                    waiting.append(event["id"])
                elif kind == "coordinator":
                    results.append({"type": "tool_result", "tool_use_id": block["id"],
                                    "content": self._coordinate(session, thread, block)})
                else:
                    event = self._emit(session, {"type": "agent.tool_use", "name": block["name"], "input": block["input"],
                                                 "evaluated_permission": "allow",
                                                 "evaluation": {"type": "always_allow"}})
                    text, is_error = self._run_builtin(session, block["name"], block["input"])
                    self._emit(session, {"type": "agent.tool_result", "tool_use_id": event["id"], "is_error": is_error,
                                         "content": [{"type": "text", "text": text}]})
                    results.append({"type": "tool_result", "tool_use_id": block["id"], "content": text, "is_error": is_error})
            if waiting:
                if results:            # results for the tools that ran now go back with the custom ones later
                    thread.conversation.append({"role": "user", "content": results})
                    thread.conversation[-1]["_partial"] = True
                return {"type": "requires_action", "event_ids": waiting}
            thread.conversation.append({"role": "user", "content": results})
        return {"type": "end_turn"}

    @staticmethod
    def _tool_definitions(agent: dict) -> list[dict]:
        tools: list[dict] = []
        for t in agent.get("tools") or []:
            if t.get("type") == "custom":
                tools.append({"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"]})
            elif t.get("type") == "agent_toolset_20260401":
                tools.extend(BUILTIN_TOOLS)
        if agent.get("multiagent"):
            tools.extend(COORDINATOR_TOOLS)
        return tools

    @staticmethod
    def _tool_kind(agent: dict, name: str) -> str:
        for t in agent.get("tools") or []:
            if t.get("type") == "custom" and t.get("name") == name:
                return "custom"
        if agent.get("multiagent") and name in ("list_agents", "send_to_agent"):
            return "coordinator"
        return "builtin"

    def _run_builtin(self, session: Session, name: str, args: dict) -> tuple[str, bool]:
        workdir = session.dir()
        try:
            if name == "bash":
                proc = subprocess.run(["bash", "-c", args.get("command", "")], cwd=workdir, capture_output=True,
                                      text=True, timeout=30)
                out = (proc.stdout + proc.stderr)[-8000:]
                return out or f"(exit {proc.returncode})", proc.returncode != 0
            path = workdir / str(args.get("path", "")).lstrip("/")
            if name == "read":
                return (path.read_text(encoding="utf-8")[:20000], False) if path.exists() else (f"{args.get('path')} not found", True)
            if name == "write":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(args.get("content", ""), encoding="utf-8")
                return f"wrote {args.get('path')}", False
            if name == "edit":
                text = path.read_text(encoding="utf-8")
                if args.get("old_str", "") not in text:
                    return "old_str not found", True
                path.write_text(text.replace(args["old_str"], args.get("new_str", ""), 1), encoding="utf-8")
                return f"edited {args.get('path')}", False
            if name == "glob":
                matches = sorted(str(Path(p).relative_to(workdir)) for p in globlib.glob(str(workdir / args.get("pattern", "*")), recursive=True))
                return "\n".join(matches) or "(no matches)", False
            if name == "grep":
                pattern = re.compile(args.get("pattern", ""))
                base = workdir / str(args.get("path", "")).lstrip("/")
                files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
                hits = [f"{p.relative_to(workdir)}:{n}:{line}" for p in files
                        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
                        if pattern.search(line)]
                return "\n".join(hits[:200]) or "(no matches)", False
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}", True
        return f"unknown tool {name}", True

    def _coordinate(self, session: Session, coordinator: Thread, block: dict) -> str:
        members: list[dict] = [m for m in session.agent.get("_roster") or [] if m.get("type") != "advisor"]
        if block["name"] == "list_agents":
            return json.dumps([{"name": a["name"], "id": a["id"], "description": a.get("description")} for a in members])
        target_name = block["input"].get("agent", "")
        target = next((a for a in members if a["name"] == target_name or a["id"] == target_name), None)
        if target is None:
            return json.dumps({"error": f"unknown agent {target_name!r}; call list_agents"})
        thread_id = block["input"].get("thread_id")
        thread = session.threads.get(thread_id or "")
        if thread is None:
            thread = Thread(id=next_id("sthread_mock_"), agent=target, parent_id=coordinator.id, name=target["name"])
            session.threads[thread.id] = thread
            self._emit(session, {"type": "session.thread_created", "session_thread_id": thread.id, "agent_name": target["name"]})
        content = [{"type": "text", "text": block["input"].get("message", "")}]
        self._emit(session, {"type": "agent.thread_message_sent", "to_session_thread_id": thread.id,
                             "to_agent_name": target["name"], "content": content})
        thread.conversation.append({"role": "user", "content": content})
        thread.status = "running"
        self._emit(session, {"type": "session.thread_status_running", "session_thread_id": thread.id, "agent_name": thread.name})
        stop = self._loop(session, thread)
        thread.status = "idle"
        self._emit(session, {"type": "session.thread_status_idle", "session_thread_id": thread.id,
                             "agent_name": thread.name, "stop_reason": stop})
        reply = ""
        for m in reversed(thread.conversation):
            if m.get("role") == "assistant":
                reply = "\n".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
                break
        self._emit(session, {"type": "agent.thread_message_received", "from_session_thread_id": thread.id,
                             "from_agent_name": target["name"], "content": [{"type": "text", "text": reply}]})
        return json.dumps({"thread_id": thread.id, "agent": target["name"], "reply": reply})


class _EventStream(httpx2.SyncByteStream):
    """SSE replay of a session's events; waits a little for new ones before closing (stream-first clients)."""

    def __init__(self, mock: ManagedAgentsMock, session: Session, deltas: bool) -> None:
        self.mock, self.session, self.deltas = mock, session, deltas

    def __iter__(self) -> Iterator[bytes]:
        sent = 0
        deadline = time.time() + self.mock.stream_grace
        while True:
            with self.session.cond:
                events = list(self.session.events[sent:])
                if not events:
                    self.session.cond.wait(timeout=0.05)
            for event in events:
                sent += 1
                if self.deltas and event["type"] == "agent.message":
                    yield _sse("event_start", {"type": "event_start", "id": event["id"], "event_type": "agent.message"})
                    for block in event.get("content") or []:
                        yield _sse("event_delta", {"type": "event_delta", "id": event["id"],
                                                   "delta": {"type": "content_delta", "index": 0, "content": block}})
                yield _sse(event["type"], event)
                deadline = time.time() + self.mock.stream_grace
            if not events and time.time() > deadline and self.session.status != "running":
                return

    def close(self) -> None:
        pass


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
