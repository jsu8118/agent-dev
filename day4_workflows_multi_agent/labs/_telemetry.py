"""Fleet condition-monitoring data for Lab 06: the `query_telemetry` tool backend.

Tool-design choices worth copying:
    * Views, not raw rows.  30 days x 24 h = 720 readings per pump; dumping them into a context
      window is expensive and makes the model do arithmetic.  The tool returns small, pre-computed
      views (weekly summary, daily means, hour-of-day profile, work orders) - code does the maths,
      the model does the judgement.
    * Scoped schemas.  Each analyst's tool definition lists only ITS pumps in the `asset_id` enum,
      so an analyst cannot wander into a colleague's assignment (no duplicated work), and the harness
      re-checks the scope anyway.
    * Units and limits travel with the data (mm/s, degC, % of BEP, alert/alarm zones), so the model
      never has to remember which foundation class uses which ISO limit.
"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

from labkit.data import data_path, load_json

WINDOWS = [("W1", "2026-08-16", "2026-08-22"), ("W2", "2026-08-23", "2026-08-29"),
           ("W3", "2026-08-30", "2026-09-05"), ("W4", "2026-09-06", "2026-09-12"),
           ("last_2d", "2026-09-13", "2026-09-14")]
SPIKE_MM_S = 50.0            # CM-GUIDE-01 §4: isolated single-sample spikes above 50 mm/s are electrical noise
# CM-GUIDE-01 §2 zone limits by foundation class (alert = B/C boundary, alarm = C/D boundary).
LIMITS = {"rigid": {"alert_mm_s": 2.8, "alarm_mm_s": 4.5}, "flexible": {"alert_mm_s": 3.5, "alarm_mm_s": 7.1}}
VIEWS = ("summary", "daily", "hourly_profile", "work_orders")


@dataclass
class Reading:
    ts: str
    running: bool
    vib: float
    temp: float
    pressure: float
    flow: float
    current: float


def _f(x: str) -> float:
    return float(x) if x not in ("", None) else float("nan")


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, dict], dict[str, list[Reading]], dict[str, list[dict]]]:
    with data_path("maintenance", "assets.csv").open(encoding="utf-8") as fh:
        assets = {row["asset_id"]: row for row in csv.DictReader(fh)}
    readings: dict[str, list[Reading]] = defaultdict(list)
    with data_path("maintenance", "telemetry.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            readings[row["asset_id"]].append(Reading(row["timestamp"], row["running"] == "1", _f(row["vibration_mm_s"]),
                                                     _f(row["bearing_temp_c"]), _f(row["discharge_pressure_bar"]),
                                                     _f(row["flow_m3h"]), _f(row["motor_current_a"])))
    work_orders: dict[str, list[dict]] = defaultdict(list)
    with data_path("maintenance", "work_orders.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            work_orders[row["asset_id"]].append(row)
    return assets, dict(readings), dict(work_orders)


def asset_ids() -> list[str]:
    return list(_load()[0])


def fleet_table() -> list[dict]:
    """What the lead sees up front: identity and context of each pump, no telemetry."""
    keep = ("asset_id", "site", "model", "criticality", "foundation", "install_date", "notes")
    return [{k: row[k] for k in keep} for row in _load()[0].values()]


def ground_truth() -> dict[str, dict]:
    return load_json("maintenance", "ground_truth.json")


def _r(x: float, n: int = 2) -> float:
    return round(x, n)


def _clean_vib(rows: list[Reading]) -> list[float]:
    return [r.vib for r in rows if 0.0 < r.vib < SPIKE_MM_S]


def _stats(rows: list[Reading], asset: dict) -> dict:
    run = [r for r in rows if r.running]
    if not run:
        return {"running_h": 0}
    vib = _clean_vib(run)
    pressure = [r.pressure for r in run]
    bep, rated = float(asset["bep_flow_m3h"]), float(asset["rated_current_a"])
    flow = statistics.fmean(r.flow for r in run)
    current = statistics.fmean(r.current for r in run)
    return {
        "running_h": len(run),
        "vib_mean_mm_s": _r(statistics.fmean(vib)) if vib else None,
        "vib_sd_mm_s": _r(statistics.pstdev(vib)) if len(vib) > 1 else None,
        "vib_max_mm_s": _r(max(vib)) if vib else None,
        "bearing_temp_mean_c": _r(statistics.fmean(r.temp for r in run), 1),
        "pressure_mean_bar": _r(statistics.fmean(pressure)),
        "pressure_cv_pct": _r(statistics.pstdev(pressure) / statistics.fmean(pressure) * 100, 1),
        "flow_mean_m3h": _r(flow, 1), "flow_pct_bep": _r(flow / bep * 100, 1),
        "current_mean_a": _r(current, 1), "current_pct_rated": _r(current / rated * 100, 1),
    }


def _summary(asset_id: str) -> dict:
    assets, readings, _ = _load()
    asset, rows = assets[asset_id], readings[asset_id]
    run = [r for r in rows if r.running]
    zeros = [r.ts for r in run if r.vib == 0.0]
    spikes = [{"ts": r.ts, "vib_mm_s": r.vib} for r in rows if r.vib >= SPIKE_MM_S]
    frozen, streak = 0, 1
    for prev, cur in zip(rows, rows[1:]):
        streak = streak + 1 if cur.vib == prev.vib and cur.vib > 0 else 1
        frozen = max(frozen, streak)
    return {
        "asset": {k: asset[k] for k in ("asset_id", "site", "model", "criticality", "foundation", "rated_current_a",
                                         "bep_flow_m3h", "install_date", "notes")},
        "limits": {**LIMITS[asset["foundation"]], "source": "CM-GUIDE-01 §2 (IOM limits take precedence)"},
        "period": "2026-08-16..2026-09-14, hourly readings; statistics over running hours; vibration statistics "
                  f"exclude zeros and single-sample spikes >= {SPIKE_MM_S:g} mm/s (reported under anomalies)",
        "windows": {name: _stats([r for r in rows if start <= r.ts[:10] <= end], asset) for name, start, end in WINDOWS},
        "anomalies": {
            "zero_vibration_while_running": {"count": len(zeros), "first": zeros[0] if zeros else None,
                                             "last": zeros[-1] if zeros else None},
            "spikes_over_50_mm_s": spikes,
            "longest_frozen_nonzero_run_h": frozen,
        },
    }


def _daily(asset_id: str) -> dict:
    assets, readings, _ = _load()
    by_day: dict[str, list[Reading]] = defaultdict(list)
    for r in readings[asset_id]:
        by_day[r.ts[:10]].append(r)
    rows = []
    for day, day_rows in sorted(by_day.items()):
        s = _stats(day_rows, assets[asset_id])
        rows.append({"date": day, **{k: s.get(k) for k in ("running_h", "vib_mean_mm_s", "bearing_temp_mean_c",
                                                          "flow_mean_m3h", "current_mean_a", "pressure_mean_bar")}})
    return {"asset_id": asset_id, "daily": rows}


def _hourly(asset_id: str) -> dict:
    _, readings, _ = _load()
    by_hour: dict[int, list[Reading]] = defaultdict(list)
    for r in readings[asset_id]:
        if r.running:
            by_hour[int(r.ts[11:13])].append(r)
    rows = []
    for hour in sorted(by_hour):
        hr = by_hour[hour]
        vib = _clean_vib(hr)
        rows.append({"hour_utc": hour, "vib_mean_mm_s": _r(statistics.fmean(vib)) if vib else None,
                     "vib_sd_mm_s": _r(statistics.pstdev(vib)) if len(vib) > 1 else None,
                     "pressure_sd_bar": _r(statistics.pstdev([r.pressure for r in hr]), 3),
                     "flow_mean_m3h": _r(statistics.fmean(r.flow for r in hr), 1)})
    return {"asset_id": asset_id, "hour_of_day_profile": rows}


def _work_orders(asset_id: str) -> dict:
    _, _, work_orders = _load()
    keep = ("work_order_id", "date", "type", "description", "parts_used")
    return {"asset_id": asset_id, "work_orders": [{k: w[k] for k in keep} for w in work_orders.get(asset_id, [])]}


def query_telemetry(asset_id: str, view: str) -> dict:
    """Backend of the query_telemetry tool. Raises KeyError/ValueError for bad input (-> tool error)."""
    if asset_id not in _load()[0]:
        raise KeyError(f"unknown asset_id {asset_id!r}")
    handlers = {"summary": _summary, "daily": _daily, "hourly_profile": _hourly, "work_orders": _work_orders}
    if view not in handlers:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    return handlers[view](asset_id)


def telemetry_tool(allowed_assets: list[str]) -> dict:
    """The tool definition, scoped to the pumps this agent may look at."""
    return {
        "name": "query_telemetry",
        "description": (
            "Condition-monitoring data for ONE pump (30 days of hourly readings to 2026-09-14). Views: "
            "'summary' - asset context, vibration zone limits, weekly statistics (W1..W4, last_2d) and anomalies "
            "(sensor zeros, spikes, frozen values); start here for every pump. 'daily' - daily means, to date a step "
            "change or a trend. 'hourly_profile' - hour-of-day means and spread, for patterns that come and go "
            "(e.g. at night). 'work_orders' - maintenance history, to explain a change. Several calls may be made in "
            "parallel in one turn."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string", "enum": list(allowed_assets)},
                           "view": {"type": "string", "enum": list(VIEWS)}},
            "required": ["asset_id", "view"],
            "additionalProperties": False,
        },
    }
