"""Shared helpers for the Day 3 labs (a helper, not a lab: files starting with "_" are not run by the tests).

What lives here and why:
* corpus loading - the manuals and policies every retrieval lab works on;
* telemetry access - hourly condition-monitoring data, and the *aggregations in code* that the diagnostic
  tools expose (the model should never do arithmetic over 720 raw rows when Python can do it exactly);
* usage accounting - dollars per call, including compaction iterations that top-level `usage` omits;
* a small, reusable agent loop - so labs 04 and 06 (and the solutions) measure the same thing the same way.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from labkit import DATA_DIR
from labkit.models import get_spec
from labkit.pricing import _get, cost_usd

DAY_DIR = Path(__file__).resolve().parents[1]
TODAY = "2026-09-15"
DATA_END = "2026-09-14"          # last full day of telemetry (hourly rows up to 23:00Z)

METRICS = {
    "vibration_mm_s": "mm/s",
    "bearing_temp_c": "°C",
    "discharge_pressure_bar": "bar",
    "flow_m3h": "m³/h",
    "motor_current_a": "A",
}
# Vibration guide CM-GUIDE-01 §4: a single sample above 50 mm/s is electrical noise, and an exact 0.0 while
# the pump runs is a sensor fault. Filtering them in code keeps them from masquerading as machine faults.
SPIKE_MM_S = 50.0


# --------------------------------------------------------------------------------------------- corpus
@dataclass
class Doc:
    doc_id: str          # file stem, e.g. "kp250_pump_iom"
    kind: str            # "manual" | "policy"
    title: str           # first H1
    text: str

    @property
    def code(self) -> str:
        """Short document code used in references, e.g. IOM-KP250 / WAR-001 (falls back to the file stem)."""
        import re
        m = re.search(r"\b(?:Document|Policy|SOP)\s+([A-Z]{2,4}-[A-Z0-9]+(?:-\d+)?)", self.text[:400]) or \
            re.search(r"\(Policy ([A-Z]{2,4}-\d{3}|[A-Z]{2,4}-[A-Z]{2,4}-\d{3})", self.text[:300]) or \
            re.search(r"\((SOP-[A-Z]+-\d+)", self.text[:300])
        return m.group(1) if m else self.doc_id


def load_docs(kinds: tuple[str, ...] = ("manual", "policy")) -> list[Doc]:
    docs: list[Doc] = []
    folders = {"manual": DATA_DIR / "manuals", "policy": DATA_DIR / "company" / "policies"}
    for kind in kinds:
        for path in sorted(folders[kind].glob("*.md")):
            text = path.read_text(encoding="utf-8")
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
            docs.append(Doc(path.stem, kind, title, text))
    return docs


def library_text(docs: list[Doc]) -> str:
    """Documents wrapped in XML-ish tags: a stable, deterministic rendering (same bytes every run = cacheable)."""
    parts = []
    for d in docs:
        tag = "manual" if d.kind == "manual" else "policy"
        parts.append(f'<{tag} id="{d.code}" file="{d.doc_id}">\n{d.text.strip()}\n</{tag}>')
    return "\n\n".join(parts)


# --------------------------------------------------------------------------------------------- telemetry
@lru_cache(maxsize=1)
def telemetry() -> list[dict]:
    rows: list[dict] = []
    with (DATA_DIR / "maintenance" / "telemetry.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append({"timestamp": r["timestamp"], "asset_id": r["asset_id"], "running": r["running"] == "1",
                         **{m: float(r[m]) for m in METRICS}})
    return rows


@lru_cache(maxsize=1)
def assets() -> dict[str, dict]:
    with (DATA_DIR / "maintenance" / "assets.csv").open(encoding="utf-8") as fh:
        return {r["asset_id"]: dict(r) for r in csv.DictReader(fh)}


@lru_cache(maxsize=1)
def _work_orders() -> tuple[dict, ...]:
    with (DATA_DIR / "maintenance" / "work_orders.csv").open(encoding="utf-8") as fh:
        return tuple(dict(r) for r in csv.DictReader(fh))


def work_orders(asset_id: str | None = None) -> list[dict]:
    return [dict(w) for w in _work_orders() if asset_id is None or w["asset_id"] == asset_id]


def ground_truth() -> dict:
    return json.loads((DATA_DIR / "maintenance" / "ground_truth.json").read_text(encoding="utf-8"))


def telemetry_csv(asset_id: str | None = None, *, start: str | None = None, end: str | None = None,
                  with_asset_column: bool = False) -> str:
    """Raw hourly rows as CSV text: what 'dump the data into the prompt' actually sends."""
    cols = ["timestamp"] + (["asset_id"] if with_asset_column else []) + ["running", *METRICS]
    lines = [",".join(cols)]
    for r in telemetry():
        if asset_id and r["asset_id"] != asset_id:
            continue
        day = r["timestamp"][:10]
        if (start and day < start) or (end and day > end):
            continue
        vals = [r["timestamp"]] + ([r["asset_id"]] if with_asset_column else []) + [
            "1" if r["running"] else "0"] + [f"{r[m]:g}" for m in METRICS]
        lines.append(",".join(vals))
    return "\n".join(lines) + "\n"


def add_days(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def days_before(end: str, days: int) -> str:
    """First day of the `days`-day window that ends on `end` (inclusive)."""
    return add_days(end, -(days - 1))


def _q(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..1) without numpy."""
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _r(x: float, nd: int = 2) -> float:
    return round(float(x), nd)


class ToolInputError(ValueError):
    """Bad tool arguments; the message tells the model how to fix the call."""


def query_telemetry(asset_id: str, metric: str, start: str | None = None, end: str | None = None,
                    agg: str = "summary") -> dict:
    """Aggregate one metric for one asset in code. agg: summary | daily | hourly_profile | raw."""
    if asset_id not in assets():
        raise ToolInputError(f"Unknown asset_id {asset_id!r}. Known assets: {', '.join(assets())}.")
    if metric not in METRICS:
        raise ToolInputError(f"Unknown metric {metric!r}. Use one of: {', '.join(METRICS)}.")
    if agg not in ("summary", "daily", "hourly_profile", "raw"):
        raise ToolInputError("agg must be one of: summary, daily, hourly_profile, raw.")
    start = start or "2026-08-16"
    end = end or DATA_END
    for label, value in (("start", start), ("end", end)):
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            raise ToolInputError(f"{label} must be a date like 2026-09-01 (got {value!r}).") from None

    rows = [r for r in telemetry() if r["asset_id"] == asset_id and start <= r["timestamp"][:10] <= end]
    running = [r for r in rows if r["running"]]
    spikes = [r for r in running if metric == "vibration_mm_s" and r[metric] > SPIKE_MM_S]
    zeros = [r for r in running if metric == "vibration_mm_s" and r[metric] == 0.0]
    bad = {id(r) for r in spikes + zeros}
    clean = [r for r in running if id(r) not in bad]
    out: dict[str, Any] = {"asset_id": asset_id, "metric": metric, "unit": METRICS[metric],
                           "window": {"start": start, "end": end}, "hours_in_window": len(rows),
                           "running_hours": len(running)}
    if spikes:
        out["spikes_excluded"] = [{"timestamp": r["timestamp"], "value": r[metric]} for r in spikes[:5]]
        out["spikes_note"] = "single-sample readings > 50 mm/s excluded as electrical noise (CM-GUIDE-01 §4)"
    if zeros:
        out["zero_readings_while_running"] = {"count": len(zeros), "first": zeros[0]["timestamp"],
                                              "last": zeros[-1]["timestamp"],
                                              "note": "exact 0.0 while running = sensor fault, excluded"}
    if not clean:
        out["note"] = "no valid running data in this window (standby unit or sensor fault)"
        return out
    values = [r[metric] for r in clean]

    if agg == "summary":
        first = [r[metric] for r in clean if r["timestamp"][:10] <= add_days(start, 6)]
        last = [r[metric] for r in clean if r["timestamp"][:10] >= days_before(end, 7)]
        last24 = [r[metric] for r in clean if r["timestamp"][:10] == end]
        out.update({"median": _r(statistics.median(values)), "p10": _r(_q(values, .1)), "p90": _r(_q(values, .9)),
                    "min": _r(min(values)), "max": _r(max(values)), "mean": _r(statistics.fmean(values))})
        if first and last:
            f, l = statistics.median(first), statistics.median(last)
            out.update({"first_7d_median": _r(f), "last_7d_median": _r(l), "change_pct": _r((l - f) / f * 100, 1)})
        if last24:
            out["last_24h_median"] = _r(statistics.median(last24))
    elif agg == "daily":
        by_day: dict[str, list[float]] = {}
        for r in clean:
            by_day.setdefault(r["timestamp"][:10], []).append(r[metric])
        out["daily"] = [{"date": d, "median": _r(statistics.median(v)), "max": _r(max(v)), "n": len(v)}
                        for d, v in sorted(by_day.items())]
    elif agg == "hourly_profile":
        by_hour: dict[int, list[float]] = {}
        for r in clean:
            by_hour.setdefault(int(r["timestamp"][11:13]), []).append(r[metric])
        profile = []
        for hour, v in sorted(by_hour.items()):
            med = statistics.median(v)
            # "± x %" around the typical value, the way IOM-KP250 §5 states pressure-fluctuation limits
            fluct = max(_q(v, .9) - med, med - _q(v, .1)) / med * 100 if med else 0.0
            profile.append({"hour_utc": hour, "median": _r(med), "p90": _r(_q(v, .9)),
                            "fluctuation_pct": _r(fluct, 1)})
        out["hourly_profile"] = profile
    else:  # raw
        cap = 168
        out["rows"] = [{"timestamp": r["timestamp"], "value": r[metric]} for r in clean[-cap:]]
        if len(clean) > cap:
            out["truncated"] = f"showing the last {cap} of {len(clean)} rows; use summary/daily/hourly_profile"
    return out


def asset_record(asset_id: str) -> dict:
    if asset_id not in assets():
        raise ToolInputError(f"Unknown asset_id {asset_id!r}. Known assets: {', '.join(assets())}.")
    return assets()[asset_id]


# --------------------------------------------------------------------------------------------- money
def usage_parts(usage: Any) -> dict[str, int]:
    """Billed token counts for one response. Compaction responses report the summarization pass in
    `usage.iterations`; the top-level fields exclude it, so we sum the iterations when present."""
    iterations = _get(usage, "iterations", None) or []
    items = iterations if iterations else [usage]
    parts = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for it in items:
        parts["input"] += int(_get(it, "input_tokens"))
        parts["cache_write"] += int(_get(it, "cache_creation_input_tokens"))
        parts["cache_read"] += int(_get(it, "cache_read_input_tokens"))
        parts["output"] += int(_get(it, "output_tokens"))
    parts["prompt"] = parts["input"] + parts["cache_write"] + parts["cache_read"]
    return parts


def response_cost(response: Any, model: str | None = None) -> float:
    usage = _get(response, "usage", None)
    model = model or _get(response, "model", "")
    iterations = _get(usage, "iterations", None) or []
    if iterations:
        return sum(cost_usd(it, model) for it in iterations)
    return cost_usd(usage, model)


def prompt_size(usage: Any) -> int:
    """Tokens the model actually read on this request (uncached + written + read from cache)."""
    return (int(_get(usage, "input_tokens")) + int(_get(usage, "cache_creation_input_tokens"))
            + int(_get(usage, "cache_read_input_tokens")))


def input_price(model: str) -> float:
    return get_spec(model).input_price / 1_000_000


def output_price(model: str) -> float:
    return get_spec(model).output_price / 1_000_000


# --------------------------------------------------------------------------------------------- printing
def table(rows: list[list[Any]], headers: list[str], *, indent: str = "  ") -> None:
    """Print an aligned text table (numbers right-aligned)."""
    cells = [[str(h) for h in headers]] + [[f"{c:,}" if isinstance(c, int) else str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def fmt(row: list[str], is_header: bool = False) -> str:
        out = []
        for i, c in enumerate(row):
            numeric = not is_header and (c.replace(",", "").replace(".", "").replace("$", "").replace("%", "")
                                         .replace("-", "").replace("x", "").strip().isdigit())
            out.append(c.rjust(widths[i]) if numeric else c.ljust(widths[i]))
        return indent + "  ".join(out).rstrip()

    print(fmt(cells[0], True))
    print(indent + "  ".join("-" * w for w in widths))
    for row in cells[1:]:
        print(fmt(row))


def money(x: float) -> str:
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


# --------------------------------------------------------------------------------------------- agent loop
@dataclass
class Turn:
    number: int
    stop_reason: str
    tool_calls: list[tuple[str, dict]]
    prompt_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int
    cost: float
    applied_edits: list[dict] = field(default_factory=list)
    compacted: bool = False
    billed_prompt: int = 0      # prompt tokens billed, including a compaction pass (usage.iterations)


@dataclass
class AgentRun:
    messages: list[dict]
    turns: list[Turn] = field(default_factory=list)
    final_text: str = ""
    tool_log: list[dict] = field(default_factory=list)
    stopped: str = ""

    @property
    def cost(self) -> float:
        return sum(t.cost for t in self.turns)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(t.prompt_tokens for t in self.turns)

    @property
    def peak_prompt_tokens(self) -> int:
        return max((t.prompt_tokens for t in self.turns), default=0)


def _text(response: Any) -> str:
    return "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()


def turn_from(number: int, response: Any, model: str | None = None) -> Turn:
    """Everything a turn table needs from one response (context edits and compaction included)."""
    usage = response.usage
    parts = usage_parts(usage)
    edits = []
    cm = getattr(response, "context_management", None)
    if cm is not None and getattr(cm, "applied_edits", None):
        edits = [e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in cm.applied_edits]
    return Turn(number=number, stop_reason=response.stop_reason or "", prompt_tokens=prompt_size(usage),
                cache_read=parts["cache_read"], cache_write=parts["cache_write"], output_tokens=parts["output"],
                cost=response_cost(response, model),
                tool_calls=[(b.name, dict(b.input)) for b in response.content if getattr(b, "type", "") == "tool_use"],
                applied_edits=edits, compacted=any(getattr(b, "type", "") == "compaction" for b in response.content),
                billed_prompt=parts["prompt"])


def run_agent(create: Callable[..., Any], *, params: dict, messages: list[dict],
              tools: dict[str, Callable[[dict], Any]], max_turns: int = 12,
              before_request: Callable[[list[dict], int], None] | None = None,
              on_turn: Callable[[Turn, Any], None] | None = None) -> AgentRun:
    """A minimal, correct tool loop.

    * appends the FULL `response.content` (thinking and compaction blocks must survive),
    * answers every tool_use of a turn in ONE user message, tool_result blocks only,
    * never executes a tool call from a response that hit max_tokens (its input may be truncated),
    * `before_request(messages, turn)` may rewrite history in place (client-side trimming, budget guards).
    """
    run = AgentRun(messages=messages)
    for number in range(1, max_turns + 1):
        if before_request:
            before_request(messages, number)
        response = create(messages=messages, **params)
        tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        turn = turn_from(number, response, params.get("model"))
        run.turns.append(turn)
        if on_turn:
            on_turn(turn, response)
        if response.stop_reason == "refusal":
            run.stopped = "refusal"
            break
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "max_tokens":
            run.stopped = "max_tokens"      # a cut-off tool call must never run; the caller decides what to do
            break
        if response.stop_reason == "compaction":   # pause_after_compaction: continue the same turn
            continue
        if response.stop_reason != "tool_use" or not tool_uses:
            run.final_text = _text(response)
            run.stopped = response.stop_reason or "end_turn"
            break
        results = []
        for block in tool_uses:
            fn = tools.get(block.name)
            try:
                if fn is None:
                    raise ToolInputError(f"Unknown tool {block.name!r}.")
                content = fn(dict(block.input))
                is_error = False
            except ToolInputError as exc:
                content, is_error = f"Error: {exc}", True
            if not isinstance(content, (str, list)):
                content = json.dumps(content, ensure_ascii=False)
            run.tool_log.append({"name": block.name, "input": dict(block.input), "is_error": is_error})
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content,
                            **({"is_error": True} if is_error else {})})
        messages.append({"role": "user", "content": results})
    else:
        run.stopped = "max_turns"
    return run
