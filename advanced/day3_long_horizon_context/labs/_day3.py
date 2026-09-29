"""Shared helpers for the advanced Day 3 labs (a helper, not a lab: files starting with "_" are not run).

What lives here and why:
* the running system - Kestrel's field-service agent: six site visits, the 40 technician messages of one working
  day (the same transcript every lab replays), and the probe questions that measure what the agent still knows;
* the agent's tools (site briefs, shift-log exports, manual sections, telemetry aggregates, the findings log),
  all answered from the course dataset so that the mock's replies are derived from real tool results;
* a small, correct tool loop that returns per-turn accounting (prompt size, cache reads/writes, output, cost);
* the prefix comparison every lab uses to say whether a request rewrote history (what preserved thinking checks);
* printing and money helpers.

Nothing here is lab logic: the strategies, budgets, reminders, memory tiers and cache experiments live in the labs.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
import re
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
DATA_END = "2026-09-14"            # last full day of telemetry
LOG_DAYS = 7                       # the historian export covers the past week
TECHNICIAN = "T. Brooks"           # the field technician in the maintenance history (WO-24490)
MARKER = "<adv_day3_field_agent>"

FIELD_SYSTEM = f"""\
{MARKER}
You are Kestrel's field-service assistant, riding along with technician {TECHNICIAN} for a full day of site visits \
on {TODAY}. Tools give you site briefs, shift-log and controller-log exports, manual sections, telemetry aggregates \
and a findings log. Rules: answer technical questions only from manual sections you have read (cite them as \
[IOM-KP250 §9]); pull data before diagnosing; record every finding with log_finding; never store customer personal \
data (Policy PRV-004 §3); on explosion-proof (ATEX) equipment, safety faults are escalated per SOP-SUP-007 - give no \
repair instructions. Keep answers short and concrete: a technician is reading them on a phone."""


# =============================================================================================== the six sites
@dataclass(frozen=True)
class Unit:
    unit_id: str          # asset id (monitored sites) or serial number
    model: str
    service: str
    notes: str = ""


@dataclass(frozen=True)
class Site:
    site_id: str
    customer_id: str
    customer: str
    plant: str
    units: tuple[Unit, ...]
    monitored: bool               # has condition-monitoring telemetry in data/maintenance
    hazard: str = ""
    critical: str = ""
    tickets: tuple[str, ...] = ()  # open tickets for sites without work orders in the maintenance history


# Units come from data/maintenance/assets.csv (monitored sites) and from the ERP's orders / build_records (the
# others); people are never listed here - contact details stay in the CRM (Policy PRV-004).
SITES: list[Site] = [
    Site("gbwd", "C-1019", "Granite Bay Water District", "GBWD North pump station",
         (Unit("GB-KP400-01", "KP-400-S", "duty pump, north reservoir supply"),
          Unit("GB-KP400-02", "KP-400-S", "duty/standby pair with -01; draws from the low-level reservoir at night"),
          Unit("GB-KP250-03", "KP-250-S", "booster pump", "coupling replaced 2026-08-28 (WO-24490)"),
          Unit("GB-KP250-04", "KP-250-S", "booster standby")),
         monitored=True, critical="drinking-water supply; duty/standby redundancy in place"),
    Site("harbor", "C-1005", "Harbor Foods Processing", "Harbor Foods Plant 2",
         (Unit("HF-KP250-01", "KP-250-S", "CIP supply"), Unit("HF-KP250-02", "KP-250-S", "process water"),
          Unit("HF-KP250-03", "KP-250-S", "chilled-water loop, runs 24/7", "bearing wear suspected (WO-24502)"),
          Unit("HF-KP100-04", "KP-100-S", "washdown")),
         monitored=True),
    Site("riverbend", "C-1009", "Riverbend Brewing Co.", "Riverbend brewhouse",
         (Unit("KC1-2603-0004", "KC-1", "wort transfer pump controller", "enclosure on the south wall of the brewhouse"),
          Unit("KP250-2609-0003", "KP-250-X", "spirits transfer (standby today)")),
         monitored=False,
         tickets=("T-31088 (2026-09-12): KC-1 trips F05 on hot afternoons - third report this month",
                  "T-31091 (2026-09-11): KC-1 F10 dry-run trip after the bright-beer tank ran empty")),
    Site("cedar", "C-1023", "Cedar Creek Dairy", "Cedar Creek Dairy, wash bay",
         (Unit("KP100-2604-0004", "KP-100-S", "washdown pump", "seal leak reported 2026-09-12"),
          Unit("KP100-2510-0001", "KP-100-S", "CIP return"),
          Unit("KC1-2510-0001", "KC-1", "washdown pump controller")),
         monitored=False,
         tickets=("T-31102 (2026-09-12): mechanical seal leak on the washdown pump after an F10 trip overnight",)),
    Site("cobalt", "C-1002", "Cobalt Chemical Works", "Cobalt Chemical main site",
         (Unit("CC-KP600-01", "KP-600-M", "boiler feed", "demand increased 2026-08-03 (WO-24466)"),
          Unit("CC-KP250X-02", "KP-250-X", "acid transfer, Zone 1 (ATEX)"),
          Unit("CC-KP250X-03", "KP-250-X", "acid transfer, Zone 1 (ATEX), standby"),
          Unit("CC-KP100-04", "KP-100-S", "cooling tower make-up", "VS-10 sensor replaced 2026-03-05 (WO-24261)")),
         monitored=True,
         hazard="CC-KP250X-02 and CC-KP250X-03 are KP-250-X units in a Zone 1 hazardous area: any fault is P1 per "
                "SOP-SUP-007 §1.4; escalate to the on-call FSE, give no repair instructions, do not restart before "
                "inspection. Gas-test certificate from the gatehouse required."),
    Site("westfield", "C-1021", "Westfield Hospital Group", "Westfield General, plant room B2",
         (Unit("KC1-WF-B2-A", "KC-1", "hot-water circulation pump A controller", "new; commissioning today"),
          Unit("KC1-WF-B2-B", "KC-1", "hot-water circulation pump B controller", "new; commissioning today"),
          Unit("WF-CIRC-A", "KP-250-S", "hot-water circulation pump A (pre-2024 install)"),
          Unit("WF-CIRC-B", "KP-250-S", "hot-water circulation pump B (pre-2024 install)")),
         monitored=False, critical="hospital: a circulation outage without redundancy is P1 (SOP-SUP-007 §1.5)",
         tickets=("SO-10289: 2 x KC-1 controllers delivered 2026-09-10; commissioning booked for today",)),
]
SITE_BY_ID = {s.site_id: s for s in SITES}
SITE_ORDER = [s.site_id for s in SITES]


def site_of(text: str) -> str | None:
    """The site a technician message refers to: '(site GBWD)' or the customer / plant name."""
    m = re.search(r"\(site ([A-Za-z]+)\)", text)
    if m and m.group(1).lower() in SITE_BY_ID:
        return m.group(1).lower()
    low = text.lower()
    for s in SITES:
        if s.customer.lower().split()[0] in low or s.plant.lower() in low:
            return s.site_id
    return None


# =============================================================================================== the day's script
@dataclass(frozen=True)
class Step:
    number: int
    site: str
    kind: str        # arrive | lookup | telemetry | log | depart | report | recall
    text: str


SCRIPT: list[Step] = [
    Step(1, "gbwd", "arrive", "Starting my day, 2026-09-15. First stop: Granite Bay Water District, North pump "
                              "station (site GBWD). Pull the site brief and the shift log before I go in."),
    Step(2, "gbwd", "lookup", "GB-KP250-03 had its coupling replaced on 28 Aug and the alignment check was skipped. "
                              "What is the alignment tolerance for a KP-250, and when must it be re-checked?"),
    Step(3, "gbwd", "telemetry", "Pull the 30-day vibration trend for GB-KP250-03 - I want to see whether anything "
                                 "changed after the coupling job."),
    Step(4, "gbwd", "log", "Log it: GB-KP250-03, misalignment suspected after the coupling replacement (step change "
                           "in vibration on 2026-08-29), action: laser alignment (SVC-ALIGN) to be booked, no parts."),
    Step(5, "gbwd", "lookup", "GB-KP400-02: operators still report knocking at night. What flow must a KP-400 stay "
                              "above to avoid suction recirculation, and what are its vibration limits on a flexible "
                              "foundation?"),
    Step(6, "gbwd", "lookup", "While I'm at the KP-400s: how much grease per bearing and at what interval do they get "
                              "regreased?"),
    Step(7, "gbwd", "depart", "Done at GBWD. Remember for next time: the GBWD shift supervisor wants a phone call "
                              "before any pump is stopped. Next stop: Harbor Foods."),
    Step(8, "harbor", "arrive", "Arrived at Harbor Foods Plant 2 (site HARBOR). Pull the site brief and the shift log."),
    Step(9, "harbor", "lookup", "I'm replacing the bearings on HF-KP250-03 today under WO-24502. Which bearings and "
                                "seal kit does a KP-250 take, and at what interval are they replaced?"),
    Step(10, "harbor", "lookup", "Torque values for reassembly, please: impeller nut, casing cover bolts, baseplate "
                                 "foundation bolts and the coupling hub set screws."),
    Step(11, "harbor", "lookup", "New bearings are in. How much grease goes into each bearing, and what is the most "
                                 "common cause of a bearing running hot right after maintenance?"),
    Step(12, "harbor", "telemetry", "Pull the 30-day bearing temperature trend for HF-KP250-03 so I have a baseline "
                                    "to compare against after the restart."),
    Step(13, "harbor", "log", "Log it: HF-KP250-03, bearings replaced (BRG-6309 x2) and seal kit MS-250 fitted, "
                              "baseplate foundation bolts re-torqued to 210 N·m, WO-24502 closed, parts used "
                              "BRG-6309 x2, MS-250 x1."),
    Step(14, "harbor", "depart", "Leaving Harbor Foods. Remember for next time: the chilled-water pump HF-KP250-03 "
                                 "may only be stopped on Sundays 06:00-10:00, and the site requires a hot-work "
                                 "permit from the shift supervisor before any grinding. Next stop: Riverbend Brewing."),
    Step(15, "riverbend", "arrive", "Arrived at Riverbend Brewing (site RIVERBEND). Pull the site brief and the "
                                    "controller log export."),
    Step(16, "riverbend", "lookup", "Their KC-1 (serial KC1-2603-0004) trips on F05 every hot afternoon. What are "
                                    "the probable causes and the remedy, and what does the application note say?"),
    Step(17, "riverbend", "lookup", "The fan runs, but the fan hour counter reads 34,120 h. Replace it proactively?"),
    Step(18, "riverbend", "lookup", "They also had an F10 last week when the bright-beer tank ran empty. What must I "
                                    "check before restarting after a dry-run trip?"),
    Step(19, "riverbend", "log", "Log it: KC1-2603-0004, recurring F05 on hot afternoons, action: heatsink cleaned "
                                 "and cooling fan replaced (KC-1-FAN x1), plus a seal inspection scheduled after the "
                                 "F10 dry-run event, parts used KC-1-FAN x1."),
    Step(20, "riverbend", "depart", "Done here. Remember for next time: Riverbend's brewhouse is a hearing-protection "
                                    "area and the plant-room key is held at the brewmaster's office. Next stop: "
                                    "Cedar Creek Dairy."),
    Step(21, "cedar", "arrive", "Arrived at Cedar Creek Dairy (site CEDAR). Pull the site brief and the controller "
                                "log export."),
    Step(22, "cedar", "lookup", "The KP-100 washdown pump (serial KP100-2604-0004) leaks at the mechanical seal. The "
                                "silicon-carbide face shows radial heat cracks and the O-rings are hard and charred. "
                                "What caused it, and is it covered under warranty?"),
    Step(23, "cedar", "lookup", "What does the pump manual say about seal leakage from dry running - what do I "
                                "replace, and what do I have to fix first?"),
    Step(24, "cedar", "lookup", "Their KC-1 logged an F10 the same night. Confirm what F10 means and what the manual "
                                "says to do before restarting."),
    Step(25, "cedar", "log", "Log it: KP100-2604-0004, mechanical seal failed by dry running after an F10 dry-run "
                             "trip, warranty excluded per SEAL-FA-02 §2, action: seal replaced (MS-100 x1) and a "
                             "low-level interlock recommended, parts used MS-100 x1."),
    Step(26, "cedar", "depart", "Leaving Cedar Creek. Remember for next time: Cedar Creek's wash bay is hosed down at "
                                "14:00 daily - no electrical work in the bay after 13:30. Next stop: Cobalt Chemical."),
    Step(27, "cobalt", "arrive", "Arrived at Cobalt Chemical main site (site COBALT). Pull the site brief and the "
                                 "shift log, and remind me of the hazardous-area rules here."),
    Step(28, "cobalt", "telemetry", "CC-KP600-01 boiler feed: operations raised the demand in August. Pull the 30-day "
                                    "motor current and flow trend."),
    Step(29, "cobalt", "lookup", "What does the manual say about motor overload from running beyond the end of the "
                                 "curve, and what is the corrective action?"),
    Step(30, "cobalt", "lookup", "The KC-1 on CC-KP250X-02 (Zone 1, ATEX) tripped F07 once this morning and the "
                                 "operator reset it. What is the rule for F07, and what do I do?"),
    Step(31, "cobalt", "log", "Log it: CC-KP600-01, motor current above rated since the demand increase (overload "
                              "right of BEP), action: throttle to the duty point and review resizing, no parts; and "
                              "CC-KP250X-02, F07 ground fault on an ATEX unit, escalated to the on-call FSE as P1 "
                              "per SOP-SUP-007, unit not to be restarted."),
    Step(32, "cobalt", "telemetry", "CC-KP100-04 had its VS-10 vibration sensor replaced in March. Pull its 30-day "
                                    "vibration summary and tell me whether the reading is valid."),
    Step(33, "cobalt", "depart", "Leaving Cobalt. Remember for next time: Cobalt issues a gas-test certificate at the "
                                 "gatehouse and it expires after 4 hours. Last stop: Westfield Hospital."),
    Step(34, "westfield", "arrive", "Arrived at Westfield General, plant room B2 (site WESTFIELD). Pull the site "
                                    "brief and the controller log export."),
    Step(35, "westfield", "lookup", "I'm commissioning a new KC-1 on their hot-water circulation pump. Which key "
                                    "parameters do I set, and what are the defaults?"),
    Step(36, "westfield", "lookup", "Which KC-1 faults are safety faults, and what is the reset rule for them?"),
    Step(37, "westfield", "lookup", "Regreasing the KP-250 circulation pumps while I'm here: what interval in "
                                    "operating hours, and how much grease per bearing?"),
    Step(38, "westfield", "log", "Log it: KC1-WF-B2-A, new KC-1 commissioned with P01 set from the motor nameplate "
                                 "and P10 at 4.0 bar, both KP-250 circulation pumps regreased with 15 g LUB-EP2 per "
                                 "bearing, action: none open, parts used LUB-EP2 x1."),
    Step(39, "westfield", "report", "End of the day. Give me the day's report: for each site, what we did, the open "
                                    "items, and the parts to order."),
    Step(40, "westfield", "recall", "And list everything I asked you to remember for next time, site by site."),
]
assert [s.number for s in SCRIPT] == list(range(1, 41))


@dataclass(frozen=True)
class Probe:
    key: str
    question: str
    expect: tuple[str, ...]      # regexes (all must match the answer, case-insensitive) for the probe to count
    lives_in: str                # where the fact was in the full transcript (for the lesson's table)


PROBE_PREFIX = "Quick check from today's session:"
PROBES: list[Probe] = [
    Probe("torque", f"{PROBE_PREFIX} what torque did we use on the baseplate foundation bolts at Harbor Foods?",
          (r"210\s*N·m",), "manual section (turn 10) and the finding (turn 13)"),
    Probe("grease400", f"{PROBE_PREFIX} how much grease per bearing, and at what interval, for the KP-400s at "
                       "Granite Bay?", (r"25 g", r"3,000"), "manual section (turn 6)"),
    Probe("f05", f"{PROBE_PREFIX} which fault code was recurring on Riverbend's KC-1, and what fixed it?",
          (r"F05", r"fan|heatsink"), "manual (turn 16) and the finding (turn 19)"),
    Probe("seal", f"{PROBE_PREFIX} what caused the seal failure at Cedar Creek, and is it covered by warranty?",
          (r"dry", r"excluded|not covered"), "seal guide (turn 22) and the finding (turn 25)"),
    Probe("atex", f"{PROBE_PREFIX} which unit at Cobalt was escalated as P1, and why?",
          (r"CC-KP250X-02", r"F07"), "the finding (turn 31)"),
    Probe("window", f"{PROBE_PREFIX} what is the stop window for the chilled-water pump at Harbor Foods?",
          (r"Sunday", r"06:00"), "the technician's words only (turn 14)"),
    Probe("heatsink", f"{PROBE_PREFIX} what heatsink temperature was logged at Riverbend's most recent F05 trip?",
          (r"\b9[0-9] ?°C",), "the controller log export only (turn 15)"),
    Probe("align", f"{PROBE_PREFIX} what parallel offset tolerance applies when re-aligning GB-KP250-03?",
          (r"0\.05 mm",), "manual section (turn 2)"),
]


def probe_passed(probe: Probe, answer: str) -> bool:
    return all(re.search(p, answer, re.I) for p in probe.expect)


# =============================================================================================== documents
MANUALS = {  # document code -> file stem (data/manuals and data/company/policies)
    "IOM-KP250": "manuals/kp250_pump_iom.md",
    "IOM-KP400": "manuals/kp400_pump_iom.md",
    "UM-KC1": "manuals/kc1_controller_manual.md",
    "SEAL-FA-02": "manuals/mechanical_seal_guide.md",
    "CM-GUIDE-01": "manuals/vibration_monitoring_guide.md",
    "SOP-SUP-007": "company/policies/safety_escalation_sop.md",
    "PRV-004": "company/policies/data_privacy_policy.md",
}
_HEADING = re.compile(r"^(#{2,3}) (\d+(?:\.\d+)?)\.?\s+(.+?)\s*$", re.M)


@lru_cache(maxsize=None)
def manual_sections(doc: str) -> dict[str, tuple[str, str]]:
    """{section number: (title, text)} for one document, split on its ## / ### headings."""
    if doc not in MANUALS:
        raise ToolInputError(f"Unknown document {doc!r}. Known documents: {', '.join(MANUALS)}.")
    text = (DATA_DIR / MANUALS[doc]).read_text(encoding="utf-8")
    heads = list(_HEADING.finditer(text))
    out: dict[str, tuple[str, str]] = {}
    for i, h in enumerate(heads):
        level = len(h.group(1))
        end = len(text)
        for nxt in heads[i + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        out[h.group(2)] = (h.group(3), text[h.end():end].strip())
    return out


def read_manual_section(doc: str, section: str) -> str:
    """One section of one document, by number ("9", "7.4") or by a fragment of its title."""
    sections = manual_sections(doc)
    key = str(section).strip().rstrip(".")
    if key not in sections:
        key = next((k for k, (title, _) in sections.items() if str(section).lower() in title.lower()), "")
    if key not in sections:
        listing = ", ".join(f"{k} {t}" for k, (t, _) in sections.items())
        raise ToolInputError(f"No section {section!r} in {doc}. Sections: {listing}.")
    title, body = sections[key]
    return f"{doc} §{key} {title}\n\n{body}"


# =============================================================================================== maintenance data
METRICS = {"vibration_mm_s": "mm/s", "bearing_temp_c": "°C", "discharge_pressure_bar": "bar", "flow_m3h": "m³/h",
           "motor_current_a": "A"}
SHORT = {"vibration_mm_s": "vib", "bearing_temp_c": "temp", "discharge_pressure_bar": "press", "flow_m3h": "flow",
         "motor_current_a": "cur"}


class ToolInputError(ValueError):
    """Bad tool arguments; the message tells the model how to fix the call."""


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
def work_orders() -> list[dict]:
    with (DATA_DIR / "maintenance" / "work_orders.csv").open(encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else float("nan")


def query_telemetry(asset_id: str, metric: str, agg: str = "summary", days: int = 30) -> dict:
    """Aggregate one metric of one monitored asset in code (summary or a daily series)."""
    if asset_id not in assets():
        raise ToolInputError(f"Unknown or unmonitored asset {asset_id!r}. Monitored assets: {', '.join(assets())}.")
    if metric not in METRICS:
        raise ToolInputError(f"Unknown metric {metric!r}. Use one of: {', '.join(METRICS)}.")
    if agg not in ("summary", "daily"):
        raise ToolInputError("agg must be 'summary' or 'daily'.")
    start = (dt.date.fromisoformat(DATA_END) - dt.timedelta(days=days - 1)).isoformat()
    rows = [r for r in telemetry() if r["asset_id"] == asset_id and start <= r["timestamp"][:10] <= DATA_END]
    running = [r for r in rows if r["running"]]
    zeros = [r for r in running if metric == "vibration_mm_s" and r[metric] == 0.0]
    spikes = [r for r in running if metric == "vibration_mm_s" and r[metric] > 50]
    bad = {id(r) for r in zeros + spikes}
    clean = [r for r in running if id(r) not in bad]
    out: dict[str, Any] = {"asset_id": asset_id, "metric": metric, "unit": METRICS[metric],
                           "window": {"start": start, "end": DATA_END}, "running_hours": len(running)}
    if zeros:
        out["zero_readings_while_running"] = {"count": len(zeros), "first": zeros[0]["timestamp"],
                                              "last": zeros[-1]["timestamp"],
                                              "note": "exact 0.0 while running = sensor fault (CM-GUIDE-01 §4), excluded"}
    if spikes:
        out["spikes_excluded"] = len(spikes)
    if not clean:
        out["note"] = "no valid running data in this window"
        return out
    values = [r[metric] for r in clean]
    if agg == "summary":
        first_days = {(dt.date.fromisoformat(start) + dt.timedelta(days=i)).isoformat() for i in range(7)}
        last_days = {(dt.date.fromisoformat(DATA_END) - dt.timedelta(days=i)).isoformat() for i in range(7)}
        first = [r[metric] for r in clean if r["timestamp"][:10] in first_days]
        last = [r[metric] for r in clean if r["timestamp"][:10] in last_days]
        out.update({"median": _median(values), "p90": round(sorted(values)[int(0.9 * (len(values) - 1))], 2),
                    "max": max(values), "first_7d_median": _median(first), "last_7d_median": _median(last)})
        if first and last and statistics.median(first):
            out["change_pct"] = round((statistics.median(last) - statistics.median(first)) / statistics.median(first) * 100, 1)
        last24 = [r[metric] for r in clean if r["timestamp"][:10] == DATA_END]
        if last24:
            out["last_24h_median"] = _median(last24)
    else:
        by_day: dict[str, list[float]] = {}
        for r in clean:
            by_day.setdefault(r["timestamp"][:10], []).append(r[metric])
        out["daily_median"] = {d: _median(v) for d, v in sorted(by_day.items())}
    return out


# =============================================================================================== site briefs / logs
def site_brief(site_id: str) -> dict:
    """What the office knows about a site: units, service, open work orders or tickets, hazards. No people."""
    if site_id not in SITE_BY_ID:
        raise ToolInputError(f"Unknown site {site_id!r}. Sites: {', '.join(SITE_BY_ID)}.")
    s = SITE_BY_ID[site_id]
    brief: dict[str, Any] = {"site_id": s.site_id, "customer_id": s.customer_id, "customer": s.customer,
                             "plant": s.plant, "units": [{"unit_id": u.unit_id, "model": u.model, "service": u.service,
                                                          **({"notes": u.notes} if u.notes else {})} for u in s.units]}
    if s.monitored:
        ids = {u.unit_id for u in s.units}
        brief["work_orders_last_12_months"] = [
            f"{w['work_order_id']} {w['date']} {w['type']} {w['asset_id']}: {w['description']}"
            for w in work_orders() if w["asset_id"] in ids and w["date"] >= "2025-09-15"]
    else:
        brief["open_tickets"] = list(s.tickets)
    if s.hazard:
        brief["hazardous_area"] = s.hazard
    if s.critical:
        brief["criticality"] = s.critical
    brief["contact"] = "site contact details are in the CRM (Policy PRV-004); preferences may be in memory"
    return brief


LOG_NOTES = {
    "gbwd": ["2026-09-09 10:30 OPER  GB-KP400-02 borescope inspection: pitting observed on the impeller eye (WO-24517)",
             "2026-09-13 22:40 OPER  GB-KP400-02 intermittent knocking noise while the reservoir was at low level",
             "2026-09-14 06:05 OPER  GB-KP250-03 vibration on the booster still reads higher than before the coupling job",
             "OPEN: WO-24490 alignment check on GB-KP250-03 skipped (laser tool out for calibration)",
             "PARTS: none on site"],
    "harbor": ["2026-09-12 09:10 STORE BRG-6309 x2 and MS-250 x1 received into the site store for WO-24502",
               "2026-09-13 15:20 OPER  HF-KP250-03 audible hum continues; bearing housing warm to the touch",
               "SAFETY: hot-work permit from the shift supervisor before any grinding or welding",
               "OPEN: WO-24502 bearing replacement HF-KP250-03 (today)",
               "PARTS: BRG-6309 x2, MS-250 x1 on site"],
    "cobalt": ["2026-09-14 08:15 OPER  KC-1 on CC-KP250X-02 tripped F07 (ground fault); reset once by the operator",
               "2026-09-13 11:00 OPER  CC-KP600-01 motor current reads above the rated 198 A at the new duty point",
               "SAFETY: Zone 1 (ATEX) around CC-KP250X-02/-03; gas-test certificate required, valid 4 hours",
               "OPEN: WO-24466 boiler feed demand increase - pump running right of BEP",
               "PARTS: VS-10 spare sensor x1 on site"],
}


@lru_cache(maxsize=None)
def site_log(site_id: str) -> str:
    """The export the technician asks for on arrival: a historian export (monitored sites, seven days of hourly
    readings for every unit) or a KC-1 diagnostic-log export (the other sites). Big on purpose - it is the
    bulk that makes a day-long context grow."""
    if site_id not in SITE_BY_ID:
        raise ToolInputError(f"Unknown site {site_id!r}. Sites: {', '.join(SITE_BY_ID)}.")
    s = SITE_BY_ID[site_id]
    if s.monitored:
        ids = [u.unit_id for u in s.units]
        start = (dt.date.fromisoformat(DATA_END) - dt.timedelta(days=LOG_DAYS - 1)).isoformat()
        lines = [f"# historian export | {s.plant} | {start} .. {DATA_END} | hourly averages | {', '.join(ids)}",
                 "NOTES:"] + [f"  {n}" for n in LOG_NOTES[site_id]] + ["READINGS:"]
        for r in telemetry():
            if r["asset_id"] in ids and start <= r["timestamp"][:10] <= DATA_END:
                lines.append(f"{r['timestamp']} {r['asset_id']} run={int(r['running'])} vib={r['vibration_mm_s']:.2f} "
                             f"temp={r['bearing_temp_c']:.1f} press={r['discharge_pressure_bar']:.2f} "
                             f"flow={r['flow_m3h']:.0f} cur={r['motor_current_a']:.1f}")
        return "\n".join(lines) + "\n"
    return _controller_log(s)


def _controller_log(s: Site) -> str:
    """A deterministic KC-1 diagnostic-log export (UM-KC1 §6: last 32 faults with current, DC bus, heatsink
    temperature and output frequency) plus operator notes - seeded per site, identical on every run."""
    rng = random.Random(SITE_ORDER.index(s.site_id) * 1000 + 7)
    controller = next(u for u in s.units if u.model == "KC-1")
    benign = [("F04", "Undervoltage"), ("F13", "Communication timeout"), ("F11", "Sensor signal loss"),
              ("F08", "Input phase loss")]
    entries: list[tuple[str, str, str, float, int, int, float]] = []
    day0 = dt.date.fromisoformat("2026-08-20")
    for i in range(26):
        stamp = dt.datetime.combine(day0 + dt.timedelta(days=i), dt.time(rng.randrange(0, 24), rng.randrange(0, 60)))
        code, name = rng.choice(benign)
        entries.append((stamp.isoformat(sep=" ", timespec="minutes"), code, name, round(rng.uniform(31, 40), 1),
                        rng.randrange(535, 560), rng.randrange(48, 66), round(rng.uniform(38, 50), 1)))
    if s.site_id == "riverbend":
        for day, hhmm, hs in (("09-08", "14:41", 91), ("09-10", "15:03", 93), ("09-12", "14:55", 92),
                              ("09-13", "15:30", 95), ("09-14", "15:12", 94)):
            entries.append((f"2026-{day} {hhmm}", "F05", "Drive overtemperature", 37.9, 551, hs, 48.0))
        entries.append(("2026-09-11 22:05", "F10", "Dry-run protection", 9.8, 549, 52, 46.5))
        notes = ["2026-09-14 15:40 OPER  brewhouse 41 °C at the enclosure; fan module hour counter 34,120 h",
                 "2026-09-11 22:10 OPER  bright-beer tank ran empty during transfer; pump ran dry until F10 tripped",
                 "OPEN: T-31088 recurring F05; T-31091 F10 dry-run - seal not yet inspected",
                 "PARTS: KC-1-FAN x1 on site"]
    elif s.site_id == "cedar":
        entries.append(("2026-09-12 03:10", "F10", "Dry-run protection", 4.1, 552, 49, 47.0))
        entries.append(("2026-09-12 03:14", "F10", "Dry-run protection", 4.0, 553, 49, 47.0))
        notes = ["2026-09-12 06:00 OPER  washdown pump KP100-2604-0004 dripping at the seal after the night shift",
                 "2026-09-12 03:20 OPER  CIP tank level low; washdown supply valve found closed",
                 "OPEN: T-31102 seal leak - pump isolated, spare seal MS-100 requested",
                 "PARTS: MS-100 x1 delivered 2026-09-14"]
    else:
        entries.append(("2026-09-14 09:00", "F14", "Safe torque off (STO) active", 0.0, 548, 41, 0.0))
        notes = ["2026-09-14 09:05 OPER  STO circuit opened for commissioning wiring on pump A controller",
                 "2026-09-10 11:30 STORE 2 x KC-1 and LUB-EP2 x2 delivered to plant room B2",
                 "OPEN: commissioning of both KC-1 controllers (today); no faults on the circulation pumps",
                 "PARTS: LUB-EP2 x2 on site"]
    entries.sort()
    entries = entries[-32:]
    lines = [f"# KC-1 diagnostic log export (UM-KC1 §6) | {s.plant} | controller {controller.unit_id} | last "
             f"{len(entries)} faults", "NOTES:"] + [f"  {n}" for n in notes] + ["FAULTS:"]
    for stamp, code, name, cur, bus, hs, freq in entries:
        lines.append(f"{stamp}  {code}  {name:<28} I={cur:>5.1f} A  DCbus={bus} V  heatsink={hs} °C  f={freq:.1f} Hz")
    return "\n".join(lines) + "\n"


# =============================================================================================== tools
FIELD_TOOLS: list[dict] = [
    {"name": "get_site_brief", "description": "What the office knows about a site: customer, plant, units and their "
     "service, open work orders or tickets, hazardous-area rules. Call it on arrival at a site.",
     "input_schema": {"type": "object", "properties": {"site_id": {"type": "string", "description": "gbwd | harbor | "
                      "riverbend | cedar | cobalt | westfield"}}, "required": ["site_id"]}},
    {"name": "get_site_log", "description": "The site's log export: a seven-day historian export for monitored sites "
     "(hourly readings per pump plus operator notes) or the KC-1 diagnostic-log export for the others. Large.",
     "input_schema": {"type": "object", "properties": {"site_id": {"type": "string"}}, "required": ["site_id"]}},
    {"name": "read_manual_section", "description": "One section of a Kestrel document. Documents: IOM-KP250 (KP-250 "
     "pump manual; §3 installation and alignment, §5 operating limits, §6 maintenance schedule, §7.x troubleshooting, "
     "§8 spare parts, §9 tightening torques), IOM-KP400 (§3 installation, §4 limits, §5 maintenance, §6 "
     "troubleshooting), UM-KC1 (controller; §3 key parameters, §4 fault codes, §5 resetting faults, §7 F05 "
     "application note), SEAL-FA-02 (seal failure analysis; §2 reading a failed seal), CM-GUIDE-01 (vibration "
     "guide), SOP-SUP-007 (safety and escalation).",
     "input_schema": {"type": "object", "properties": {"doc": {"type": "string"}, "section": {"type": "string",
                      "description": "section number such as '9' or '7.4', or a fragment of its title"}},
                      "required": ["doc", "section"]}},
    {"name": "query_telemetry", "description": "Aggregate 30 days of condition-monitoring data for one monitored "
     "pump: a summary (median, p90, first vs last week, last 24 h) or a daily series. Sensor faults and spikes are "
     "filtered and reported.",
     "input_schema": {"type": "object", "properties": {"asset_id": {"type": "string"}, "metric": {"type": "string",
                      "enum": list(METRICS)}, "agg": {"type": "string", "enum": ["summary", "daily"]}},
                      "required": ["asset_id", "metric"]}},
    {"name": "log_finding", "description": "Record a finding in the day's findings log (the system of record for "
     "the end-of-day report): unit, what was found, the action taken or needed, parts used or to order.",
     "input_schema": {"type": "object", "properties": {"site_id": {"type": "string"}, "unit_id": {"type": "string"},
                      "finding": {"type": "string"}, "action": {"type": "string"},
                      "parts": {"type": "array", "items": {"type": "string"}},
                      "work_order": {"type": "string"}}, "required": ["site_id", "unit_id", "finding", "action"]}},
]


class FindingsLog:
    """The day's findings - the harness's system of record, independent of what survives in the context."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, entry: dict) -> dict:
        record = {"finding_id": f"FND-{len(self.entries) + 1}", **entry}
        self.entries.append(record)
        return {"recorded": record, "findings_today": len(self.entries)}


def make_executor(findings: FindingsLog | None = None) -> Callable[[str, dict], tuple[str, bool]]:
    """execute(name, input) -> (content, is_error) for the field tools."""
    findings = findings or FindingsLog()

    def execute(name: str, args: dict) -> tuple[str, bool]:
        try:
            if name == "get_site_brief":
                return json.dumps(site_brief(args["site_id"]), ensure_ascii=False), False
            if name == "get_site_log":
                return site_log(args["site_id"]), False
            if name == "read_manual_section":
                return read_manual_section(args["doc"], args["section"]), False
            if name == "query_telemetry":
                return json.dumps(query_telemetry(args["asset_id"], args["metric"], args.get("agg", "summary")),
                                  ensure_ascii=False), False
            if name == "log_finding":
                return json.dumps(findings.add(args), ensure_ascii=False), False
            raise ToolInputError(f"Unknown tool {name!r}.")
        except (ToolInputError, KeyError) as exc:
            return f"Error: {exc}", True
    execute.findings = findings      # type: ignore[attr-defined]
    return execute


# =============================================================================================== accounting
def usage_parts(usage: Any) -> dict[str, int]:
    """Billed token counts for one response; compaction responses report the summarisation pass in
    `usage.iterations` and the top-level fields exclude it, so sum the iterations when present."""
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
    """Tokens the model read on this request: uncached + written to cache + read from cache."""
    return int(_get(usage, "input_tokens")) + int(_get(usage, "cache_creation_input_tokens")) + \
        int(_get(usage, "cache_read_input_tokens"))


def price(model: str, kind: str = "input") -> float:
    spec = get_spec(model)
    return (spec.input_price if kind == "input" else spec.output_price) / 1_000_000


def money(x: float) -> str:
    return f"${x:,.4f}" if abs(x) < 1 else f"${x:,.2f}"


def table(rows: list[list[Any]], headers: list[str], *, indent: str = "  ") -> None:
    """Print an aligned text table (numbers right-aligned)."""
    cells = [[str(h) for h in headers]] + [[f"{c:,}" if isinstance(c, int) else str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def fmt(row: list[str], is_header: bool = False) -> str:
        out = []
        for i, c in enumerate(row):
            probe = c.replace(",", "").replace(".", "").replace("$", "").replace("%", "").replace("-", "").strip()
            numeric = not is_header and probe.replace("x", "").isdigit() and probe != ""
            out.append(c.rjust(widths[i]) if numeric else c.ljust(widths[i]))
        return indent + "  ".join(out).rstrip()

    print(fmt(cells[0], True))
    print(indent + "  ".join("-" * w for w in widths))
    for row in cells[1:]:
        print(fmt(row))


# =============================================================================================== the tool loop
@dataclass
class TurnStats:
    number: int
    site: str
    requests: int = 0
    tool_calls: list[str] = field(default_factory=list)
    prompt: int = 0            # largest prompt the model read in this turn (the context size)
    cache_read: int = 0
    cache_write: int = 0
    uncached: int = 0
    output: int = 0
    cost: float = 0.0
    reply: str = ""
    stop_reason: str = ""
    effort: str = ""
    notes: list[str] = field(default_factory=list)
    responses: list[Any] = field(default_factory=list)


def text_of(response: Any) -> str:
    return "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()


def run_turn(create: Callable[..., Any], *, params: dict, messages: list[dict], text: str,
             execute: Callable[[str, dict], tuple[str, bool]], number: int = 0, site: str = "",
             after_user: list[dict] | None = None, before_request: Callable[[list[dict]], None] | None = None,
             after_results: Callable[[list[dict]], None] | None = None,
             on_response: Callable[[Any], None] | None = None, max_rounds: int = 6) -> TurnStats:
    """One technician message, answered - a minimal, correct tool loop.

    * appends the FULL `response.content` (thinking, progress and compaction blocks must survive verbatim);
    * answers every tool_use of a round in ONE user message, tool_result blocks first;
    * `after_user` messages (per-turn reminders) go right after the technician's message, and
      `after_results(messages)` may append more after each tool_result message (a reminder that must stay in
      view for the whole turn is re-sent after every round - lab 04);
    * `before_request(messages)` may rewrite history in place (truncation, summarisation - the labs measure what
      that costs); never runs a tool call from a response that stopped on max_tokens.
    """
    stats = TurnStats(number=number, site=site)
    messages.append({"role": "user", "content": text})
    for extra in after_user or []:
        messages.append(extra)
    for _ in range(max_rounds):
        if before_request:
            before_request(messages)
        response = create(messages=messages, **params)
        stats.requests += 1
        stats.responses.append(response)
        parts = usage_parts(response.usage)
        stats.prompt = max(stats.prompt, prompt_size(response.usage))
        stats.cache_read += parts["cache_read"]
        stats.cache_write += parts["cache_write"]
        stats.uncached += parts["input"]
        stats.output += parts["output"]
        stats.cost += response_cost(response, params.get("model"))
        stats.stop_reason = response.stop_reason or ""
        if on_response:
            on_response(response)
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            stats.reply = text_of(response)
            return stats
        results = []
        for block in tool_uses:
            stats.tool_calls.append(block.name)
            content, is_error = execute(block.name, dict(block.input))
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content,
                            **({"is_error": True} if is_error else {})})
        messages.append({"role": "user", "content": results})
        if after_results:
            after_results(messages)
    stats.notes.append("max_rounds reached")
    return stats


def turn_row(t: TurnStats) -> list:
    return [t.number, t.site, t.requests, t.prompt, t.cache_read, t.cache_write, t.output, money(t.cost)]


TURN_HEADERS = ["turn", "site", "requests", "context", "cache read", "cache write", "output", "cost"]


# =============================================================================================== prefix comparison
def _canon(value: Any) -> str:
    """Bytes the way the binding check sees them: cache_control stripped, key order irrelevant."""
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "cache_control"}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def message_bytes(message: dict) -> str:
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
    return _canon({**{k: v for k, v in message.items() if k != "content"}, "content": blocks})


def prefix_changes(previous: dict, current: dict) -> list[str]:
    """How `current` differs from `previous` in the parts a thinking block is bound to: the top-level system
    prompt, the set of loaded tools, and the messages they share - in the vocabulary of the API's diagnostics.
    Empty means `current` extends `previous` append-only (or restarts after a compaction block)."""
    changes: list[str] = []
    if _canon(previous.get("system") or "") != _canon(current.get("system") or ""):
        changes.append("system_rerendered")
    prev_tools = {t["name"]: _canon(t) for t in previous.get("tools") or [] if not t.get("defer_loading")}
    cur_tools = {t["name"]: _canon(t) for t in current.get("tools") or [] if not t.get("defer_loading")}
    if set(prev_tools) != set(cur_tools):
        changes.append("tool_set_changed")
    elif prev_tools != cur_tools:
        changes.append("tool_schema_changed")
    prev_msgs = previous.get("messages") or []
    cur_msgs = current.get("messages") or []
    if any(isinstance(m.get("content"), list) and any(isinstance(b, dict) and b.get("type") == "compaction"
                                                     for b in m["content"]) for m in cur_msgs):
        return changes                      # the checked prefix restarts at the compaction block
    if len(cur_msgs) < len(prev_msgs):
        changes.append("blocks_removed")
        return changes
    for i, m in enumerate(prev_msgs):
        if message_bytes(m) != message_bytes(cur_msgs[i]):
            prev_blocks = m.get("content") if isinstance(m.get("content"), list) else None
            cur_blocks = cur_msgs[i].get("content") if isinstance(cur_msgs[i].get("content"), list) else None
            if prev_blocks is not None and cur_blocks is not None and len(cur_blocks) < len(prev_blocks):
                changes.append(f"messages[{i}] blocks_removed")
            elif prev_blocks is not None and cur_blocks is not None and len(cur_blocks) > len(prev_blocks):
                changes.append(f"messages[{i}] blocks_inserted")
            else:
                changes.append(f"messages[{i}] blocks_modified")
            break
    return changes


def as_body(messages: list[dict], **params: Any) -> dict:
    """A request body the way the SDK will send it (content blocks as dicts), for prefix comparison."""
    def plain(block: Any) -> Any:
        if hasattr(block, "model_dump"):
            return block.model_dump(mode="json", exclude_none=True)
        return block
    body = {k: v for k, v in params.items() if k in ("system", "tools")}
    body["messages"] = [{**m, "content": [plain(b) for b in m["content"]] if isinstance(m.get("content"), list)
                         else m.get("content")} for m in messages]
    return body


def approx_tokens(text: str) -> int:
    """A planning estimate (~3.8 characters per token, the mock's own rate); count_tokens for anything that matters."""
    return max(1, round(len(text) / 3.8))


# =============================================================================================== the day as a loop
@dataclass
class DayRun:
    """Everything one run of the day produced: per-turn accounting, the final history, the findings log, and the
    harness's own side calls (summaries, extractions) which are billed too."""
    turns: list[TurnStats]
    messages: list[dict]
    findings: FindingsLog
    side_calls: list[tuple[str, Any]] = field(default_factory=list)     # (label, response)

    @property
    def side_cost(self) -> float:
        return sum(response_cost(r) for _, r in self.side_calls)

    @property
    def cost(self) -> float:
        return sum(t.cost for t in self.turns) + self.side_cost

    @property
    def peak_context(self) -> int:
        return max((t.prompt for t in self.turns), default=0)

    @property
    def output_tokens(self) -> int:
        return sum(t.output for t in self.turns) + sum(usage_parts(r.usage)["output"] for _, r in self.side_calls)

    @property
    def truncated(self) -> list[TurnStats]:
        return [t for t in self.turns if t.stop_reason == "max_tokens"]

    def turn(self, number: int) -> TurnStats:
        return next(t for t in self.turns if t.number == number)


def run_day(create: Callable[..., Any], params: dict, *, steps: list[Step] | None = None,
            messages: list[dict] | None = None, execute: Callable[[str, dict], tuple[str, bool]] | None = None,
            before_turn: Callable[[Step, list[dict]], None] | None = None,
            after_user: Callable[[Step], list[dict]] | None = None,
            before_request: Callable[[list[dict]], None] | None = None,
            after_results: Callable[[list[dict]], None] | None = None,
            after_turn: Callable[[Step, TurnStats, list[dict]], None] | None = None,
            on_turn: Callable[[Step, TurnStats], None] | None = None) -> DayRun:
    """Replay the technician's day (or a slice of it) through `run_turn`, with the hooks a harness would use:
    `before_turn` (effort changes, history resets at site boundaries), `after_user` (reminders), `before_request`
    (client-side truncation), `after_results` (per-round reminders), `after_turn` (summaries, state extraction)."""
    messages = [] if messages is None else messages
    execute = execute or make_executor()
    turns: list[TurnStats] = []
    for s in steps or SCRIPT:
        if before_turn:
            before_turn(s, messages)
        stats = run_turn(create, params=params, messages=messages, text=s.text, execute=execute, number=s.number,
                         site=s.site, after_user=after_user(s) if after_user else None,
                         before_request=before_request, after_results=after_results)
        turns.append(stats)
        if on_turn:
            on_turn(s, stats)
        if after_turn:
            after_turn(s, stats, messages)
    return DayRun(turns=turns, messages=messages, findings=execute.findings)       # type: ignore[attr-defined]


def ask_probes(create: Callable[..., Any], params: dict, messages: list[dict], *, probes: list[Probe] | None = None,
               prepare: Callable[[list[dict]], list[dict]] | None = None) -> list[tuple[Probe, str, bool]]:
    """Ask each probe question in a fork of the conversation (the day's history is not changed), and grade the
    answer against the fact that was said during the day. `prepare(fork)` applies the strategy's own view of the
    history (e.g. the truncation window) before the probe is sent."""
    out = []
    for probe in probes or PROBES:
        fork = list(messages)
        if prepare:
            fork = prepare(fork)
        stats = run_turn(create, params=params, messages=fork, text=probe.question, execute=make_executor())
        out.append((probe, stats.reply, probe_passed(probe, stats.reply)))
    return out


def tool_result_tokens(messages: list[dict]) -> int:
    """Estimated tokens of every tool result in a history (what a task budget counts besides the output)."""
    total = 0
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                content = b.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                total += approx_tokens(text)
    return total
