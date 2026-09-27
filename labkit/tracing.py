"""A minimal, dependency-free tracer for agent runs (OpenTelemetry-flavoured).

Why trace an agent?  An agent run is a tree: one user request fans out into several
LLM calls and tool calls.  When a run is slow, expensive, or wrong, you need to see
*which step* - the per-request cost summary cannot tell you.  Production teams export
these spans to an OpenTelemetry backend (Jaeger, Grafana Tempo, Honeycomb, Langfuse,
...); this tracer writes the same information to JSON Lines so you can learn the
concepts with no infrastructure.  Attribute names follow the OpenTelemetry GenAI
semantic conventions (gen_ai.*), so the mapping to a real exporter is one-to-one.

    tracer = Tracer("support-agent")
    with tracer.span("agent.run", user_id="u-42"):
        with tracer.span("llm.call") as span:
            response = client.messages.create(...)
            span.record_llm(response)
        with tracer.span("tool.lookup_order", order_id="SO-10045") as span:
            ...
    print(tracer.render_tree())
    tracer.export()            # .runs/traces/<trace_id>.jsonl
"""

from __future__ import annotations

import contextvars
import json
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .config import runs_dir
from .pricing import _get, cost_usd

_current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("labkit_current_span", default=None)


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start: float
    end: float | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return ((self.end or time.time()) - self.start) * 1000

    def set(self, key: str, value: Any) -> "Span":
        self.attributes[key] = value
        return self

    def event(self, name: str, **attributes: Any) -> None:
        self.events.append({"name": name, "time": time.time(), **attributes})

    def error(self, exc: BaseException | str) -> None:
        self.status = "error"
        self.attributes["error.message"] = str(exc)
        if isinstance(exc, BaseException):
            self.attributes["error.type"] = type(exc).__name__

    def record_llm(self, message: Any) -> None:
        """Attach model, stop reason, token usage and cost from a Message."""
        usage = _get(message, "usage", None)
        model = _get(message, "model", "")
        self.attributes.update({
            "gen_ai.system": "anthropic",
            "gen_ai.response.model": model,
            "gen_ai.response.id": _get(message, "id", ""),
            "gen_ai.response.finish_reason": _get(message, "stop_reason", ""),
            "gen_ai.usage.input_tokens": _get(usage, "input_tokens"),
            "gen_ai.usage.output_tokens": _get(usage, "output_tokens"),
            "gen_ai.usage.cache_read_input_tokens": _get(usage, "cache_read_input_tokens"),
            "gen_ai.usage.cache_creation_input_tokens": _get(usage, "cache_creation_input_tokens"),
            "cost_usd": round(_message_cost(usage, model), 6),
        })


def _message_cost(usage: Any, model: str) -> float:
    """Cost of one response. With `usage.iterations` (server-side fallbacks, compaction) each iteration is billed at
    the rates of the model that ran it; top-level usage covers only the final attempt."""
    iterations = _get(usage, "iterations", None) or []
    if iterations:
        return sum(cost_usd(it, _get(it, "model", None) or model) for it in iterations)
    return cost_usd(usage, model)


class Tracer:
    def __init__(self, service: str, *, trace_id: str | None = None) -> None:
        self.service = service
        self.trace_id = trace_id or secrets.token_hex(8)
        self.spans: list[Span] = []
        self._lock = threading.Lock()

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        parent = _current_span.get()
        span = Span(name=name, trace_id=self.trace_id, span_id=secrets.token_hex(4),
                    parent_id=parent.span_id if parent and parent.trace_id == self.trace_id else None,
                    start=time.time(), attributes={"service.name": self.service, **attributes})
        with self._lock:
            self.spans.append(span)
        token = _current_span.set(span)
        try:
            yield span
        except BaseException as exc:
            span.error(exc)
            raise
        finally:
            span.end = time.time()
            _current_span.reset(token)

    # -------------------------------------------------------------- reporting
    def totals(self) -> dict[str, float]:
        llm = [s for s in self.spans if "gen_ai.usage.input_tokens" in s.attributes]
        return {
            "llm_calls": len(llm),
            "tool_calls": sum(1 for s in self.spans if s.name.startswith("tool.")),
            "input_tokens": sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in llm),
            "output_tokens": sum(s.attributes.get("gen_ai.usage.output_tokens", 0) for s in llm),
            "cost_usd": round(sum(s.attributes.get("cost_usd", 0.0) for s in llm), 6),
            "errors": sum(1 for s in self.spans if s.status == "error"),
        }

    def render_tree(self) -> str:
        children: dict[str | None, list[Span]] = {}
        for s in self.spans:
            children.setdefault(s.parent_id, []).append(s)
        lines = [f"trace {self.trace_id} ({self.service})"]

        def walk(parent: str | None, depth: int) -> None:
            for s in sorted(children.get(parent, []), key=lambda x: x.start):
                extra = ""
                if "gen_ai.usage.input_tokens" in s.attributes:
                    a = s.attributes
                    extra = (f"  in={a['gen_ai.usage.input_tokens']} out={a['gen_ai.usage.output_tokens']} "
                             f"stop={a.get('gen_ai.response.finish_reason')} ${a.get('cost_usd', 0):.5f}")
                flag = "  !! " + s.attributes.get("error.message", "") if s.status == "error" else ""
                lines.append(f"{'  ' * depth}- {s.name} [{s.duration_ms:.0f} ms]{extra}{flag}")
                walk(s.span_id, depth + 1)

        walk(None, 1)
        t = self.totals()
        lines.append(f"  totals: llm_calls={t['llm_calls']} tool_calls={t['tool_calls']} in={t['input_tokens']} "
                     f"out={t['output_tokens']} cost=${t['cost_usd']:.5f} errors={t['errors']}")
        return "\n".join(lines)

    def export(self, path: Path | None = None) -> Path:
        path = path or runs_dir("traces") / f"{self.service}-{self.trace_id}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for s in self.spans:
                fh.write(json.dumps(asdict(s), default=str) + "\n")
        return path


def load_trace(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
