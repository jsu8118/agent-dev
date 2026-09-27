"""_telemetry - turn agent traces into metrics, audit them for PII, export them (lab 05, lab 08 service).

    rows = [trace_metrics(tracer.spans) for tracer in tracers]      # one row per ticket
    board = dashboard(rows)                                          # p50/p95, cost per resolved ticket, ...
    hits = find_pii(tracer.spans)                                    # what should NOT be in your logs
    otlp = to_otlp(tracer.spans, "support-agent")                    # POST-able to an OTel collector /v1/traces
    Metrics().inc("tickets_total", outcome="answered"); ...render()  # Prometheus text exposition

Attribute names follow the OpenTelemetry GenAI semantic conventions used by labkit.tracing (gen_ai.*).
Those conventions are still evolving (names have been renamed between versions), so pin the version your
backend expects in one place - here, the exporter - rather than across the code base.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

from _evalkit import percentile

# Expected refusals (policy, validation, identity) are the tools doing their job; everything else - timeouts,
# outages, bugs, unknown tools - is what you alert on.  This regex must track every ToolError message in
# kestrel/support_tools.py, and it silently goes stale when one is reworded: have tools return an error CODE instead.
EXPECTED_TOOL_ERROR = re.compile(r"approval limit|not eligible|not verified|not a verified|already been refunded|"
                                 r"not permitted|guardrail|Invalid arguments|not found|must be|not a valid|"
                                 r"is not on order|Cannot open a warranty RMA|issued only after|Partial refunds",
                                 re.I)
_QUEUE = re.compile(r"'queue': '(\w+)'")
_PRIORITY = re.compile(r"'priority': '(P\d)'")


def _as_dict(span: Any) -> dict:
    return asdict(span) if is_dataclass(span) else dict(span)


def _duration_s(span: dict) -> float:
    return max(0.0, (span.get("end") or span["start"]) - span["start"])


def trace_metrics(spans: Iterable[Any]) -> dict:
    """One row of metrics for one agent run (one trace)."""
    spans = [_as_dict(s) for s in spans]
    root = next((s for s in spans if s["name"] == "agent.run"), spans[0])
    llm = [s for s in spans if s["name"] == "llm.call"]
    tools = [s for s in spans if s["name"].startswith("tool.")]
    attr = lambda s, k: s["attributes"].get(k, 0) or 0          # noqa: E731
    escalations = [s for s in tools if s["name"] == "tool.escalate_to_human" and s["status"] == "ok"]
    queues = [m.group(1) for s in escalations if (m := _QUEUE.search(s["attributes"].get("tool.input", "")))]
    priorities = [m.group(1) for s in escalations if (m := _PRIORITY.search(s["attributes"].get("tool.input", "")))]
    inp = sum(attr(s, "gen_ai.usage.input_tokens") for s in llm)
    cache_r = sum(attr(s, "gen_ai.usage.cache_read_input_tokens") for s in llm)
    cache_w = sum(attr(s, "gen_ai.usage.cache_creation_input_tokens") for s in llm)
    return {
        "trace_id": root["trace_id"], "ticket": root["attributes"].get("ticket", ""),
        "latency_s": _duration_s(root), "llm_calls": len(llm), "tool_calls": len(tools),
        "tool_errors": sum(s["status"] == "error" for s in tools),
        "tool_errors_unexpected": sum(s["status"] == "error" and not EXPECTED_TOOL_ERROR.search(
            str(s["attributes"].get("error.message", ""))) for s in tools),
        "llm_latency_s": [_duration_s(s) for s in llm],
        "tool_latency_s": sum(_duration_s(s) for s in tools),
        "input_tokens": inp, "cache_read_tokens": cache_r, "cache_write_tokens": cache_w,
        "output_tokens": sum(attr(s, "gen_ai.usage.output_tokens") for s in llm),
        "cost_usd": round(sum(attr(s, "cost_usd") for s in llm), 6),
        "stop_reason": llm[-1]["attributes"].get("gen_ai.response.finish_reason", "") if llm else "",
        "models": sorted({s["attributes"].get("gen_ai.response.model", "") for s in llm}),
        "escalated": bool(escalations), "queues": queues, "priorities": priorities,
        "errors": sum(s["status"] == "error" for s in spans if not s["name"].startswith("tool.")),
    }


def dashboard(rows: list[dict]) -> dict:
    """Fleet-level numbers a support-ops dashboard would show."""
    n = len(rows)
    latencies = [r["latency_s"] for r in rows]
    costs = [r["cost_usd"] for r in rows]
    per_call = [x for r in rows for x in r["llm_latency_s"]]
    tool_calls = sum(r["tool_calls"] for r in rows)
    resolved = [r for r in rows if not r["escalated"]]
    prompt = sum(r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] for r in rows)
    return {
        "tickets": n,
        "latency_p50_s": percentile(latencies, 50), "latency_p95_s": percentile(latencies, 95),
        "llm_call_p95_s": percentile(per_call, 95),
        "llm_calls_p95": percentile([r["llm_calls"] for r in rows], 95),
        "cost_mean_usd": sum(costs) / n if n else 0.0, "cost_p95_usd": percentile(costs, 95),
        "cost_total_usd": sum(costs),
        "cost_per_resolved_usd": sum(costs) / len(resolved) if resolved else None,
        "automation_rate": len(resolved) / n if n else 0.0,
        "escalation_rate": 1 - len(resolved) / n if n else 0.0,
        "tool_error_rate": sum(r["tool_errors"] for r in rows) / tool_calls if tool_calls else 0.0,
        "unexpected_tool_error_rate": sum(r["tool_errors_unexpected"] for r in rows) / tool_calls if tool_calls else 0.0,
        "cache_read_share": sum(r["cache_read_tokens"] for r in rows) / prompt if prompt else 0.0,
        "output_tokens_mean": sum(r["output_tokens"] for r in rows) / n if n else 0.0,
        "stop_reasons": dict(Counter(r["stop_reason"] for r in rows)),
        "models": dict(Counter(m for r in rows for m in r["models"])),
    }


# ------------------------------------------------------------------------------------------ PII audit
PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@(?!kestrel-pumps\.example)[\w-]+(?:\.[\w-]+)+\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b"),
    "phone": re.compile(r"(?<![\w-])\+?\d{1,3}[ .-]\(?\d{2,4}\)?[ .-]\d{3,4}[ .-]\d{3,4}\b"),
    "street_address": re.compile(r"\b\d{2,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Pkwy|Parkway|Street|St|Avenue|Ave|Road|Rd|"
                                 r"Blvd|Drive|Dr|Lane|Ln|Way)\b"),
}


def _strings(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            yield from _strings(value, f"{path}[{i}]")


def find_pii(spans: Iterable[Any]) -> list[dict]:
    """Every PII-looking value in span names, attributes and events (the things you export and retain)."""
    hits = []
    for span in spans:
        span = _as_dict(span)
        for path, text in _strings({"attributes": span["attributes"], "events": span["events"]}):
            for kind, pattern in PII_PATTERNS.items():
                for m in pattern.finditer(text):
                    hits.append({"span": span["name"], "field": path, "kind": kind, "value": m.group(0)})
    return hits


# ------------------------------------------------------------------------------------------ OTLP export
def _otlp_value(value: Any) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}             # OTLP/JSON encodes 64-bit ints as strings
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def to_otlp(spans: Iterable[Any], service: str) -> dict:
    """labkit spans -> an OTLP/JSON ExportTraceServiceRequest (what an OTel collector accepts on /v1/traces)."""
    out = []
    for span in spans:
        s = _as_dict(span)
        attributes = dict(s["attributes"])
        if s["name"] == "llm.call":
            attributes.setdefault("gen_ai.operation.name", "chat")
        elif s["name"].startswith("tool."):
            attributes.setdefault("gen_ai.operation.name", "execute_tool")
            attributes.setdefault("gen_ai.tool.name", s["name"][5:])
        out.append({
            # OTLP wants 16-byte trace ids and 8-byte span ids (hex); labkit's are shorter, so left-pad.
            "traceId": s["trace_id"].rjust(32, "0"), "spanId": s["span_id"].rjust(16, "0"),
            **({"parentSpanId": s["parent_id"].rjust(16, "0")} if s["parent_id"] else {}),
            "name": s["name"], "kind": 3 if s["name"] == "llm.call" else 1,          # CLIENT for model calls
            "startTimeUnixNano": str(int(s["start"] * 1e9)),
            "endTimeUnixNano": str(int((s["end"] or s["start"]) * 1e9)),
            "attributes": [{"key": k, "value": _otlp_value(v)} for k, v in attributes.items()],
            "events": [{"name": e["name"], "timeUnixNano": str(int(e["time"] * 1e9))} for e in s["events"]],
            "status": {"code": 2, "message": attributes.get("error.message", "")} if s["status"] == "error"
            else {"code": 1},
        })
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name",
                                                             "value": {"stringValue": service}}]},
                               "scopeSpans": [{"scope": {"name": "labkit.tracing"}, "spans": out}]}]}


# ------------------------------------------------------------------------------------------ Prometheus
class Metrics:
    """A tiny in-process metrics registry rendered in the Prometheus text format (no dependencies).

    Per-process by design: with N worker processes, Prometheus scrapes each and sums - so never put
    per-request state here, only counters and histograms.
    """

    BUCKETS = (0.5, 1, 2.5, 5, 10, 20, 30, 60)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = {}
        self.histograms: dict[tuple[str, tuple], list[float]] = {}
        self.gauges: dict[tuple[str, tuple], float] = {}
        self.help: dict[str, str] = {}

    def set(self, name: str, value: float, *, help: str = "", **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.gauges[key] = value
            if help:
                self.help[name] = help

    def inc(self, name: str, value: float = 1.0, *, help: str = "", **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.counters[key] = self.counters.get(key, 0.0) + value
            if help:
                self.help[name] = help

    def observe(self, name: str, value: float, *, help: str = "", **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.histograms.setdefault(key, []).append(value)
            if help:
                self.help[name] = help

    @staticmethod
    def _labels(pairs: tuple, extra: dict | None = None) -> str:
        items = list(pairs) + list((extra or {}).items())
        return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}" if items else ""

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name in sorted({k[0] for k in self.counters}):
                lines += [f"# HELP {name} {self.help.get(name, name)}", f"# TYPE {name} counter"]
                for (n, labels), value in sorted(self.counters.items()):
                    if n == name:
                        lines.append(f"{name}{self._labels(labels)} {value:g}")
            for name in sorted({k[0] for k in self.gauges}):
                lines += [f"# HELP {name} {self.help.get(name, name)}", f"# TYPE {name} gauge"]
                for (n, labels), value in sorted(self.gauges.items()):
                    if n == name:
                        lines.append(f"{name}{self._labels(labels)} {value:g}")
            for name in sorted({k[0] for k in self.histograms}):
                lines += [f"# HELP {name} {self.help.get(name, name)}", f"# TYPE {name} histogram"]
                for (n, labels), values in sorted(self.histograms.items()):
                    if n != name:
                        continue
                    for bound in self.BUCKETS:
                        count = sum(v <= bound for v in values)
                        lines.append(f"{name}_bucket{self._labels(labels, {'le': f'{bound:g}'})} {count}")
                    lines.append(f"{name}_bucket{self._labels(labels, {'le': '+Inf'})} {len(values)}")
                    lines.append(f"{name}_sum{self._labels(labels)} {sum(values):.6f}")
                    lines.append(f"{name}_count{self._labels(labels)} {len(values)}")
        return "\n".join(lines) + "\n"
