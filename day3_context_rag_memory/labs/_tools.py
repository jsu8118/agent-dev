"""Tool schemas and implementations for the Day 3 agents (helper module, not a lab).

Design choices worth copying:
* Retrieval as tools: `search_manuals` returns short snippets + identifiers (cheap to read), `read_section`
  returns one full section on demand ("just-in-time" context instead of stuffing everything up front).
* Data as tools: `query_telemetry` aggregates in code and returns a few hundred tokens of JSON instead of
  720 raw rows; bad samples (spikes, sensor dropouts) are filtered and *reported*, never silently dropped.
* Every result is self-describing (units, window, asset metadata) so it still makes sense if it is the only
  thing left in context after older turns were trimmed.
* Errors are instructions: a bad argument returns `is_error` with the list of valid values.
"""

from __future__ import annotations

import json
from typing import Any

from kestrel.kb import KnowledgeBase

import _day3 as d3

KB = KnowledgeBase()

# ------------------------------------------------------------------------------------------ diagnostic toolset
SEARCH_MANUALS = {
    "name": "search_manuals",
    "description": ("Keyword (BM25) search over Kestrel's product manuals and company policies. Returns the best "
                    "matching sections as {doc, section, score, snippet}. Use specific technical terms (part numbers, "
                    "fault codes, symptoms). Then call read_section to read a hit in full."),
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Search terms, e.g. 'KP-400 vibration limits flexible foundation'"},
        "top_k": {"type": "integer", "description": "Number of hits to return (1-8, default 5)"}},
        "required": ["query"], "additionalProperties": False},
}
READ_SECTION = {
    "name": "read_section",
    "description": ("Read one section of a manual or policy in full. `doc` is the doc id from search_manuals "
                    "(e.g. 'kp250_pump_iom'); `section` is the section heading as returned by search_manuals "
                    "(a prefix such as '7.2' or '5. Operating limits' also works)."),
    "input_schema": {"type": "object", "properties": {
        "doc": {"type": "string"}, "section": {"type": "string"}},
        "required": ["doc", "section"], "additionalProperties": False},
}
QUERY_TELEMETRY = {
    "name": "query_telemetry",
    "description": ("Aggregate hourly condition-monitoring data for ONE asset and ONE metric, computed in code. "
                    "agg='summary' (median, p10/p90, first vs last 7 days, last 24 h), 'daily' (daily medians), "
                    "'hourly_profile' (median and ±fluctuation by hour of day - reveals time-of-day patterns) or "
                    "'raw' (last 168 rows; avoid unless needed). Spikes > 50 mm/s and 0.0 sensor dropouts are "
                    "excluded from statistics and reported separately. Data covers 2026-08-16 to 2026-09-14."),
    "input_schema": {"type": "object", "properties": {
        "asset_id": {"type": "string", "description": "e.g. HF-KP250-03"},
        "metric": {"type": "string", "enum": list(d3.METRICS)},
        "start": {"type": "string", "description": "First day (YYYY-MM-DD), default 2026-08-16"},
        "end": {"type": "string", "description": "Last day (YYYY-MM-DD), default 2026-09-14"},
        "agg": {"type": "string", "enum": ["summary", "daily", "hourly_profile", "raw"]}},
        "required": ["asset_id", "metric"], "additionalProperties": False},
}
LIST_WORK_ORDERS = {
    "name": "list_work_orders",
    "description": "Asset master data (model, site, foundation, rated current, BEP flow, notes) and its maintenance "
                   "work-order history.",
    "input_schema": {"type": "object", "properties": {"asset_id": {"type": "string"}},
                     "required": ["asset_id"], "additionalProperties": False},
}
ISSUES = ["bearing_wear", "cavitation", "misalignment", "sensor_fault", "overload_right_of_bep", "no_fault",
          "undetermined"]
SUBMIT_DIAGNOSIS = {
    "name": "submit_diagnosis",
    "description": ("Record the final diagnosis for the asset (call exactly once, when the evidence is conclusive). "
                    "Evidence items must quote numbers from tool results; manual_references name the sections read."),
    "strict": True,
    "input_schema": {"type": "object", "properties": {
        "asset_id": {"type": "string"},
        "issue": {"type": "string", "enum": ISSUES},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "manual_references": {"type": "array", "items": {"type": "string"}}},
        "required": ["asset_id", "issue", "confidence", "evidence", "recommended_actions", "manual_references"],
        "additionalProperties": False},
}
DIAGNOSTIC_TOOLS = [SEARCH_MANUALS, READ_SECTION, QUERY_TELEMETRY, LIST_WORK_ORDERS, SUBMIT_DIAGNOSIS]

DIAGNOSTIC_SYSTEM = """\
<day3_diagnostic_agent>
You are Kestrel's reliability assistant. Technicians ask you to diagnose pumps from remote condition-monitoring \
data. Today is 2026-09-15; telemetry covers 2026-08-16 to 2026-09-14 (hourly).

How to work
- Gather facts with tools before concluding: maintenance history and asset data (list_work_orders), aggregated \
telemetry (query_telemetry - prefer summary, daily and hourly_profile over raw rows), and the relevant manual \
sections (search_manuals, then read_section). Call independent tools in parallel.
- Judge readings against the limits in the manual for THAT model and foundation type; never from memory.
- Treat single-sample spikes and 0.0 dropouts as sensor/electrical artefacts, as the vibration guide says.
- When the evidence is conclusive, call submit_diagnosis once. Then reply with a short summary for the technician: \
diagnosis, the 2-4 key numbers, and the next actions.
</day3_diagnostic_agent>"""


def search_manuals(inp: dict) -> str:
    query = str(inp.get("query", "")).strip()
    if not query:
        raise d3.ToolInputError("query must be a non-empty string of search terms.")
    k = min(max(int(inp.get("top_k") or 5), 1), 8)
    hits = KB.search(query, k)
    return json.dumps({"query": query, "results": [
        {"doc": c.doc, "title": c.title, "section": c.section, "score": round(s, 2),
         "snippet": " ".join(c.text.split())[:160]} for s, c in hits]}, ensure_ascii=False)


def read_section(inp: dict) -> str:
    doc, wanted = str(inp.get("doc", "")), str(inp.get("section", "")).strip()
    chunks = [c for c in KB.chunks if c.doc == doc]
    if not chunks:
        docs = sorted({c.doc for c in KB.chunks})
        raise d3.ToolInputError(f"Unknown doc {doc!r}. Valid doc ids: {', '.join(docs)}.")
    low = wanted.lower()
    match = ([c for c in chunks if c.section.lower() == low]
             or [c for c in chunks if c.section.lower().startswith(low) or f"> {low}" in c.section.lower()]
             or [c for c in chunks if low and low in c.section.lower()])
    if not match:
        raise d3.ToolInputError(f"No section {wanted!r} in {doc}. Sections: " + "; ".join(c.section for c in chunks))
    text = "\n\n".join((f"### {c.section}\n" if len(match) > 1 else "") + c.text for c in match)
    return json.dumps({"doc": doc, "title": match[0].title, "section": match[0].section if len(match) == 1 else wanted,
                       "text": text}, ensure_ascii=False)


def query_telemetry(inp: dict) -> str:
    return json.dumps(d3.query_telemetry(inp.get("asset_id", ""), inp.get("metric", ""), inp.get("start"),
                                         inp.get("end"), inp.get("agg") or "summary"), ensure_ascii=False)


def list_work_orders(inp: dict) -> str:
    asset = d3.asset_record(str(inp.get("asset_id", "")))
    return json.dumps({"asset": asset, "work_orders": d3.work_orders(asset["asset_id"])}, ensure_ascii=False)


class DiagnosisDesk:
    """Where submitted diagnoses go (a real system would open a work order here)."""

    def __init__(self) -> None:
        self.submitted: list[dict] = []

    def submit(self, inp: dict) -> str:
        missing = [k for k in SUBMIT_DIAGNOSIS["input_schema"]["required"] if k not in inp]
        if missing:
            raise d3.ToolInputError(f"Missing fields: {', '.join(missing)}.")
        if inp["issue"] not in ISSUES:
            raise d3.ToolInputError(f"issue must be one of {ISSUES}.")
        self.submitted.append(dict(inp))
        return json.dumps({"status": "recorded", "diagnosis_id": f"DX-{len(self.submitted):04d}"})

    def tools(self) -> dict[str, Any]:
        return {"search_manuals": search_manuals, "read_section": read_section, "query_telemetry": query_telemetry,
                "list_work_orders": list_work_orders, "submit_diagnosis": self.submit}


# ------------------------------------------------------------------------------------------ fleet-review toolset
LIST_ASSETS = {
    "name": "list_assets",
    "description": "The 12 monitored pumps: asset_id, site, model, foundation, rated current, criticality.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
GET_TELEMETRY_ROWS = {
    "name": "get_telemetry_rows",
    "description": ("Raw hourly telemetry rows (CSV) for one asset over the last `days` days up to 2026-09-14. "
                    "Columns: timestamp, running, vibration_mm_s, bearing_temp_c, discharge_pressure_bar, flow_m3h, "
                    "motor_current_a. The first line describes the asset."),
    "input_schema": {"type": "object", "properties": {
        "asset_id": {"type": "string"}, "days": {"type": "integer", "description": "1-30, default 10"}},
        "required": ["asset_id"], "additionalProperties": False},
}
FLEET_TOOLS = [LIST_ASSETS, GET_TELEMETRY_ROWS]

FLEET_SYSTEM = """\
<day3_fleet_review>
You are Kestrel's reliability assistant running the weekly fleet review of 12 monitored pumps. Today is 2026-09-15.
Vibration limits (RMS mm/s, from the IOM manuals and CM-GUIDE-01): rigid foundation alert 2.8, alarm 4.5; \
flexible foundation alert 3.5, alarm 7.1.
Treat single-sample spikes above 50 mm/s as electrical noise and runs of exactly 0.0 mm/s while running as a sensor fault.
Work site by site. When a site's data arrives, write one note line per asset, in this exact format, before moving on:
- <asset_id> [<model>, <foundation>]: vib p50 <x> / p90 <y> mm/s (last 24 h <z>), temp p50 <t> °C (<change>), \
current p50 <c> A -> <OK|WATCH|ACTION|STANDBY>: <reason>
Finish with a fleet report that lists every asset with its status and key numbers.
</day3_fleet_review>"""
FLEET_REQUEST = ("Run the weekly fleet review. Go site by site, pull the last 14 days of hourly telemetry for each "
                 "pump, write your notes per site, then give me the fleet report.")


def list_assets(inp: dict) -> str:
    return json.dumps({"assets": [
        {"asset_id": a["asset_id"], "site": a["site"], "model": a["model"], "foundation": a["foundation"],
         "rated_current_a": float(a["rated_current_a"]), "criticality": a["criticality"]}
        for a in d3.assets().values()]}, ensure_ascii=False)


def get_telemetry_rows(inp: dict) -> str:
    asset = d3.asset_record(str(inp.get("asset_id", "")))
    days = min(max(int(inp.get("days") or 10), 1), 30)
    start = d3.days_before(d3.DATA_END, days)
    header = (f"# asset {asset['asset_id']} | site {asset['site']} | model {asset['model']} | foundation "
              f"{asset['foundation']} | rated current {asset['rated_current_a']} A | {start} to {d3.DATA_END}\n")
    return header + d3.telemetry_csv(asset["asset_id"], start=start, end=d3.DATA_END)


FLEET_TOOL_FUNCS = {"list_assets": list_assets, "get_telemetry_rows": get_telemetry_rows}


def digest_telemetry(result: str) -> str:
    """Collapse a raw get_telemetry_rows result to one line of statistics - computed in code, no model call.
    Used for client-side trimming: the model already read the rows and wrote its notes; keep the gist."""
    import re
    import statistics
    head = result.splitlines()[0]
    asset = re.search(r"\b[A-Z]{2}-KP\d{3}X?-\d{2}\b", head).group(0)
    rows = [line.split(",") for line in result.splitlines()[2:] if line.count(",") == 6]
    run = [r for r in rows if r[1] == "1"]
    vib = [float(r[2]) for r in run if 0.0 < float(r[2]) <= d3.SPIKE_MM_S]
    if len(vib) < 24:
        return f"[trimmed by client] {asset} ({len(rows)} h): ran {len(run)} h - standby, not enough data"
    temp = [float(r[3]) for r in run]
    cur = [float(r[6]) for r in run]
    zeros = sum(1 for r in run if float(r[2]) == 0.0)
    spikes = sum(1 for r in run if float(r[2]) > d3.SPIKE_MM_S)
    return (f"[trimmed by client] {asset} ({len(rows)} h): vib p50 {statistics.median(vib):.2f} / max {max(vib):.2f} "
            f"mm/s, temp p50 {statistics.median(temp):.1f} °C, current p50 {statistics.median(cur):.0f} A, "
            f"{zeros} zero readings, {spikes} spikes")
