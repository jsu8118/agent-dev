"""Mock policies for Day 3 - context engineering: caching, retrieval and memory.

Every scenario matches on a Day 3-specific marker in the system prompt (and, for agents, on Day 3 tool names),
and every answer is derived ONLY from what is in the request: manual text in the system prompt, document /
search_result blocks, tool results, memory-file contents, or a compaction summary.  Like a real model, the
policies can only report what survived in context - which is exactly what the context-management labs measure.

  day3.knowledge        <day3_manual_library> | <day3_full_corpus> | <day3_budget_probe>   labs 01, 02 (+ ex11)
  day3.rag_citations    <day3_rag> + document / search_result blocks                        lab 03
  day3.oneshot_dx       <day3_oneshot_diagnosis> + structured output                        lab 04
  day3.diagnostic_agent <day3_diagnostic_agent> + query_telemetry / read_section tools      lab 04
  day3.fleet_review     <day3_fleet_review> + get_telemetry_rows tool                       lab 06 (+ ex09)
  day3.field_memory     <day3_field_memory> + memory tool                                   lab 07
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from typing import Callable

from ..registry import scenario
from ..reply import Reply, cite, cited_text, json_reply, say, tool, use_tools
from ..request import MockRequest
from ..schema_tools import synthesize

ASSET_RE = re.compile(r"\b[A-Z]{2}-KP\d{3}X?-\d{2}\b")


def _md(text: str) -> str:
    """Strip markdown emphasis for prose answers (citations keep the exact source text)."""
    return text.replace("**", "").replace("`", "")


def _cells(row: str) -> list[str]:
    return [_md(c).strip() for c in row.strip().strip("|").split("|")]


# ============================================================================================ knowledge engine
@dataclass
class Unit:
    text: str            # exact substring of the source text
    source: int          # index into the list of sources
    offset: int          # character offset inside that source


@dataclass
class Source:
    text: str
    kind: str = "text"             # "text" (system prompt) | "document" | "search_result"
    doc: dict | None = None        # entry from MockRequest.documents
    block: int = 0                 # content-block index inside a search result


_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z*(\"'0-9])")
_STOP = set("a an the of to in on for and or is are be by with at as it this that from not no do does what which "
            "when how can should i we you our your my me us it's its has have had was were will would could may must "
            "there their them they then than so if into out about any all".split())
_WORD = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?")


def units_of(text: str, source: int) -> list[Unit]:
    """Citable units: table rows and sentences - each an exact substring of the source."""
    out: list[Unit] = []
    pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        base = pos + len(line) - len(line.lstrip())
        pos += len(line) + 1
        if (not stripped or stripped.startswith("#") or stripped.startswith("<")
                or re.fullmatch(r"[|\-: ]+", stripped)):
            continue
        if stripped.startswith("|"):
            out.append(Unit(stripped, source, base))
            continue
        cursor = 0
        for part in _SPLIT.split(stripped):
            idx = stripped.find(part, cursor)
            cursor = idx + len(part)
            if len(part.strip()) >= 12:
                out.append(Unit(part, source, base + idx))
    return out


def _terms(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in _STOP and len(t) > 1}


class Evidence:
    """Everything the 'model' can quote from: units over one or more sources."""

    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources
        self.units = [u for i, s in enumerate(sources) for u in units_of(s.text, i)]

    def find(self, pattern: str) -> Unit | None:
        rx = re.compile(pattern, re.I)
        return next((u for u in self.units if rx.search(u.text)), None)

    def label(self, u: Unit) -> str:
        """Where a unit comes from: a document title, or the [CODE §n] reference inside a system-prompt library."""
        src = self.sources[u.source]
        return ((src.doc or {}).get("title") or "") + _ref(src.text, u.offset)

    def best(self, question: str, n: int = 2) -> list[Unit]:
        """Generic extractive fallback: units that share the most informative terms with the question, preferring
        units from the product the question names (a KP-250 question should not be answered from the KP-400 IOM)."""
        q = _terms(question)
        if not q or not self.units:
            return []
        products = {re.sub(r"[^A-Z0-9]", "", p.upper()) for p in re.findall(r"\b(?:KP|KC)-?\d{1,3}\b", question, re.I)}
        df: dict[str, int] = {}
        unit_terms = [_terms(u.text) for u in self.units]
        for ts in unit_terms:
            for t in ts:
                df[t] = df.get(t, 0) + 1
        scored = []
        for u, ts in zip(self.units, unit_terms):
            common = q & ts
            if len(common) >= 2:
                score = sum(1.0 / df[t] for t in common) * len(common)
                scored.append((score, u))
        if products:
            own = [(sc, u) for sc, u in scored
                   if any(p in re.sub(r"[^A-Z0-9]", "", self.label(u).upper()) for p in products)]
            scored = own or scored
        scored.sort(key=lambda x: -x[0])
        picked: list[Unit] = []
        for sc, u in scored:
            if picked and sc < 0.5 * scored[0][0]:
                break                                   # only keep passages nearly as relevant as the best one
            if all(u.text != p.text for p in picked):
                picked.append(u)
            if len(picked) == n:
                break
        return picked


Piece = tuple[str, "Unit | None"]


def _num(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, _md(text))
    return m.group(1) if m else default


def t_regrease(q: str, ev: Evidence) -> list[Piece] | None:
    if not re.search(r"regreas|grease|lubric", q, re.I):
        return None
    if re.search(r"KP-?400|split", q, re.I):
        u = ev.find(r"3,000 operating hours")
        if not u:
            return None
        grams = _num(r"(\d+) g of LUB-EP2", u.text, "25")
        return [(f"Regrease each KP-400 bearing every 3,000 operating hours with {grams} g of LUB-EP2.", u)]
    u = ev.find(r"2,000 operating hours")
    if not u:
        return None
    grams = _num(r"(\d+) g of LUB-EP2", u.text, "15")
    pieces: list[Piece] = [(f"Regrease both KP-250 bearings every 2,000 operating hours with {grams} g of LUB-EP2 "
                            "each.", u)]
    if "over-grease" in u.text:
        pieces.append((" Don't over-grease: excess grease raises the bearing temperature.", u))
    hot = ev.find(r"Over-greasing\*\* \(most common")
    if hot:
        pieces.append((" If a bearing runs hot right after maintenance, over-greasing is the most common cause - "
                       "remove the excess grease.", hot))
    return pieces


def t_kp400_limits(q: str, ev: Evidence) -> list[Piece] | None:
    if not (re.search(r"KP-?400|split-case|steel|flexible", q, re.I) and re.search(r"vibrat|mm/s|shut|limit", q, re.I)):
        return None
    row = ev.find(r"Bearing housing vibration \(RMS, 10")
    if not row:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", " ".join(_cells(row.text)[1:]))
    if len(nums) < 2:
        return None
    normal, alarm = float(nums[0]), float(nums[-1])
    value = _num(r"(\d+(?:\.\d+)?)\s*mm/s", q)
    if value:
        v = float(value)
        zone = "normal" if v <= normal else "alarm/shutdown" if v > alarm else "alert"
        verdict = "so no shutdown is needed yet" if zone != "alarm/shutdown" else "so plan a controlled shutdown"
        first = (f"{value} mm/s is in the {zone} band for a KP-400 on a flexible foundation (normal up to {normal:g}, "
                 f"alert {normal:g}-{alarm:g}, alarm/shutdown above {alarm:g} mm/s), {verdict}.")
    else:
        first = (f"For a KP-400 on a flexible foundation the vibration limits are: normal up to {normal:g} mm/s, "
                 f"alert {normal:g}-{alarm:g} mm/s, alarm/shutdown above {alarm:g} mm/s.")
    pieces: list[Piece] = [(first, row)]
    act = ev.find(r"Zone B/C crossed")
    if act and value and normal < float(value) <= alarm:
        pieces.append((" Treat it as an alert: open a corrective work order, increase monitoring to daily and check "
                       "lubrication and alignment within a week.", act))
    return pieces


def t_f05(q: str, ev: Evidence) -> list[Piece] | None:
    if not re.search(r"\bF05\b|overtemp|hot afternoon", q, re.I):
        return None
    row = ev.find(r"\|\s*F05\s*\|")
    note = ev.find(r"68%")
    if not row and not note:
        return None
    pieces: list[Piece] = []
    if row:
        c = _cells(row.text)
        pieces.append((f"F05 is {c[1].lower()}; typical causes are {c[2][0].lower() + c[2][1:]}.", row))
    if note:
        pct = _num(r"(\d+)% of F05 events", note.text, "68")
        pieces.append((f" Start with the cooling fan and the heatsink: in Kestrel's field study {pct}% of F05 events "
                       "were fixed by cleaning the heatsink and replacing a worn KC-1-FAN;", note))
        pieces.append((" the rest needed better room ventilation or moving the enclosure out of direct sun.", note))
    elif row:
        pieces.append((f" Remedy per the fault table: {_cells(row.text)[3]}.", row))
    return pieces


def t_torque(q: str, ev: Evidence) -> list[Piece] | None:
    if not re.search(r"torque|bolt", q, re.I):
        return None
    row = ev.find(r"foundation bolts M20")
    if not row:
        return None
    c = _cells(row.text)
    return [(f"Tighten the {c[0][0].lower() + c[0][1:]} to {c[1]}.", row)]


def t_cavitation(q: str, ev: Evidence) -> list[Piece] | None:
    if not re.search(r"gravel|cavitat|crackl", q, re.I) or re.search(r"warrant|covered", q, re.I):
        return None
    sym = ev.find(r"Symptoms:\*\* crackling")
    if not sym:
        return None
    lead = ("Those are the classic cavitation symptoms:" if re.search(r"gravel|sound|nois|jump", q, re.I)
            else "Cavitation shows up as")
    pieces: list[Piece] = [(f"{lead} crackling noise 'like gravel', fluctuating discharge pressure and reduced "
                            "flow.", sym)]
    for pat, text in ((r"\|\s*Clogged suction strainer\s*\|", " Clean the suction strainer (FLT-SUC-100) and check "
                       "the differential pressure across it."),
                      (r"\|\s*Suction valve partly closed", " Open the suction valve fully - never throttle the "
                       "suction side."),
                      (r"Low level in the suction tank", " Check the suction tank level and raise the minimum level "
                       "if needed.")):
        u = ev.find(pat)
        if u:
            pieces.append((text, u))
    return pieces


def t_bearing_trend(q: str, ev: Evidence) -> list[Piece] | None:
    if not re.search(r"bearing wear|(vibration.*temperature|temperature.*vibration).*(rise|rising|climb|together)",
                     q, re.I | re.S):
        return None
    u = ev.find(r"classic signature of \*\*bearing wear")
    if not u:
        return None
    pieces: list[Piece] = [("Vibration and bearing temperature rising together over days or weeks is the classic "
                            "signature of bearing wear; plan the bearing replacement before the alarm limit is "
                            "reached.", u)]
    acc = ev.find(r"accelerates near the end of life")
    if acc:
        pieces.append((" Expect the rate to accelerate near the end of life.", acc))
    return pieces


def t_warranty_cavitation(q: str, ev: Evidence) -> list[Piece] | None:
    if not (re.search(r"warrant|covered", q, re.I) and re.search(r"cavitat|pitting", q, re.I)):
        return None
    u = ev.find(r"\*\*Cavitation damage\*\* caused by insufficient NPSH")
    if not u:
        return None
    return [("No - cavitation damage caused by insufficient NPSH available at the customer site is excluded from the "
             "warranty.", u)]


def t_memory_pii(q: str, ev: Evidence) -> list[Piece] | None:
    if not re.search(r"memory", q, re.I) or not re.search(r"phone|personal|contact|store|keep", q, re.I):
        return None
    u = ev.find(r"must not store customer personal data")
    if not u:
        return None
    return [("No. AI assistants must not store customer personal data in long-term memory; memory may hold only "
             "preferences and working notes keyed by customer ID.", u)]


def t_sensor_zero(q: str, ev: Evidence) -> list[Piece] | None:
    if not re.search(r"0\.0\b|exactly zero|flat-?line", q, re.I):
        return None
    u = ev.find(r"Flat-line at exactly 0\.0")
    if not u:
        return None
    return [("A flat line at exactly 0.0 mm/s while the motor draws current is a sensor or cable fault, not a "
             "machine fault: repair the VS-10 sensor and check the process data meanwhile.", u)]


TOPICS: list[Callable[[str, Evidence], list[Piece] | None]] = [
    t_warranty_cavitation, t_memory_pii, t_sensor_zero, t_regrease, t_kp400_limits, t_f05, t_torque,
    t_cavitation, t_bearing_trend,
]


def answer_pieces(question: str, ev: Evidence) -> list[Piece]:
    for topic in TOPICS:
        pieces = topic(question, ev)
        if pieces:
            return pieces
    units = ev.best(question)
    if not units:
        return [("I couldn't find this in the material I was given, so I won't guess.", None)]
    pieces: list[Piece] = [("Here is what the provided material says: ", None)]
    for i, u in enumerate(units):
        text = "; ".join(c for c in _cells(u.text) if c) if u.text.startswith("|") else _md(u.text)
        text = re.sub(r"^(?:[*\-]|\d+\.)\s+", "", text.strip())          # list markers
        pieces.append(((" " if i else "") + text.rstrip(".") + ".", u))
    return pieces


def _ref(text: str, offset: int) -> str:
    """'[IOM-KP250 §6]' for a unit inside a <manual id=...> library rendered into the system prompt."""
    head = text[:offset]
    tags = list(re.finditer(r'<(?:manual|policy) id="([^"]+)"', head))
    if not tags:
        return ""
    code = tags[-1].group(1)
    section = ""
    for h in re.finditer(r"^#{2,3} (\d+(?:\.\d+)?)\.?\s", head[tags[-1].end():], re.M):
        section = h.group(1)
    return f" [{code} §{section}]" if section else f" [{code}]"


# ============================================================================================ knowledge scenarios
def _latest_system_instruction(req: MockRequest) -> str:
    """A mid-conversation {"role": "system"} message after the last user turn (Opus 5 supports these)."""
    last_user = max((i for i, m in enumerate(req.messages) if m.get("role") == "user"), default=-1)
    for m in req.messages[last_user + 1:]:
        if m.get("role") == "system":
            c = m.get("content")
            return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
    return ""


def _question_with_context(req: MockRequest, ev: Evidence) -> str:
    """Resolve short follow-ups ('And the KP-400?') against the previous user turn."""
    texts = req.texts("user")
    q = texts[-1] if texts else ""
    for topic in TOPICS:
        if topic(q, ev):
            return q
    return (texts[-2] + " " + q) if len(texts) >= 2 else q


def _is_knowledge(req: MockRequest) -> bool:
    s = req.system_text
    return any(m in s for m in ("<day3_manual_library>", "<day3_full_corpus>", "<day3_budget_probe>"))


@scenario("day3.knowledge", match=_is_knowledge, priority=5)
def knowledge(req: MockRequest) -> Reply:
    system = req.system_text
    ev = Evidence([Source(system)])
    question = _question_with_context(req, ev)
    pieces = answer_pieces(question, ev)
    instruction = _latest_system_instruction(req)
    if re.search(r"bullet", instruction, re.I):
        cap = 3 if re.search(r"three|3", instruction) else 5
        lines = [f"- {p.strip()}{_ref(system, u.offset) if u else ''}" for p, u in pieces if p.strip()][:cap]
        return say("\n".join(lines), complexity=0.3)
    text = "".join(p + (_ref(system, u.offset) if u else "") for p, u in pieces)
    return say(text.strip(), complexity=0.4)


def _search_result_blocks(req: MockRequest) -> list[dict]:
    """Raw search_result blocks in API order (top-level in user turns, or inside tool results)."""
    found: list[dict] = []
    for m in req.messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "search_result":
                found.append(b)
            elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                found.extend(s for s in b["content"] if isinstance(s, dict) and s.get("type") == "search_result")
    return found


def _citation(sources: list[Source], u: Unit) -> dict:
    src = sources[u.source]
    if src.kind == "search_result":
        doc = src.doc or {}
        return {"type": "search_result_location", "cited_text": src.text, "search_result_index": doc.get("index", 0),
                "source": doc.get("source") or "", "title": doc.get("title"), "start_block_index": src.block,
                "end_block_index": src.block + 1}
    return cite(src.doc or {"text": src.text, "kind": "document", "index": 0}, u.text)


@scenario("day3.rag_citations", match=lambda r: "<day3_rag>" in r.system_text and bool(r.documents), priority=5)
def rag_citations(req: MockRequest) -> Reply:
    docs = req.documents
    sources: list[Source] = []
    raw_results = _search_result_blocks(req)
    sr_seen = 0
    for d in docs:
        if d["kind"] == "search_result":
            blocks = raw_results[sr_seen].get("content", []) if sr_seen < len(raw_results) else []
            sr_seen += 1
            for bi, b in enumerate(blocks):
                if isinstance(b, dict) and b.get("text"):
                    sources.append(Source(b["text"], "search_result", d, bi))
        else:
            sources.append(Source(d["text"], "document", d))
    ev = Evidence(sources)
    question = req.last_user_text or req.first_user_text
    pieces = answer_pieces(question, ev)
    cited = any(d.get("citations") for d in docs)
    content = []
    for text, u in pieces:
        if u is not None and cited:
            content.append(cited_text(text, [_citation(sources, u)]))
        else:
            content.append({"type": "text", "text": text})
    return Reply(content=content, complexity=0.5)


# ============================================================================================ telemetry reasoning
_ROW = re.compile(r"^(\d{4}-\d\d-\d\dT(\d\d):00:00Z),(?:[A-Z]{2}-KP\d{3}X?-\d{2},)?([01]),([\d.]+),([\d.]+),"
                  r"([\d.]+),([\d.]+),([\d.]+)\s*$", re.M)


def parse_rows(text: str) -> list[dict]:
    return [{"ts": m.group(1), "hour": int(m.group(2)), "running": m.group(3) == "1", "vib": float(m.group(4)),
             "temp": float(m.group(5)), "press": float(m.group(6)), "flow": float(m.group(7)),
             "cur": float(m.group(8))} for m in _ROW.finditer(text)]


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def features(rows: list[dict]) -> dict:
    """What a careful analyst computes from hourly rows before judging (spikes and 0.0 sensor dropouts removed)."""
    run = [r for r in rows if r["running"]]
    f: dict = {"hours": len(rows), "running_hours": len(run)}
    if len(run) < 24:
        return f
    spikes = [r for r in run if r["vib"] > 50]
    zeros = [r for r in run if r["vib"] == 0.0]
    clean = [r for r in run if 0.0 < r["vib"] <= 50]
    f["spikes"] = [(r["ts"], r["vib"]) for r in spikes]
    f["zeros"] = len(zeros)
    f["zero_span"] = (zeros[0]["ts"], zeros[-1]["ts"]) if zeros else None
    if not clean:
        return f
    days = sorted({r["ts"][:10] for r in clean})
    span = max(1, min(7, len(days) // 3))
    first_days, last_days = set(days[:span]), set(days[-span:])
    med = statistics.median

    def m(key: str, sel: Callable[[dict], bool]) -> float:
        vals = [r[key] for r in clean if sel(r)]
        return med(vals) if vals else float("nan")

    f.update({
        "span_days": span, "start": days[0], "end": days[-1],
        "vib_p50": med([r["vib"] for r in clean]), "vib_p90": _pct([r["vib"] for r in clean], .9),
        "vib_max": max(r["vib"] for r in clean),
        "vib_first": m("vib", lambda r: r["ts"][:10] in first_days), "vib_last": m("vib", lambda r: r["ts"][:10] in last_days),
        "vib_24h": m("vib", lambda r: r["ts"][:10] == days[-1]),
        "temp_p50": med([r["temp"] for r in clean]),
        "temp_first": m("temp", lambda r: r["ts"][:10] in first_days), "temp_last": m("temp", lambda r: r["ts"][:10] in last_days),
        "cur_p50": med([r["cur"] for r in clean]),
        "cur_first": m("cur", lambda r: r["ts"][:10] in first_days), "cur_last": m("cur", lambda r: r["ts"][:10] in last_days),
        "flow_first": m("flow", lambda r: r["ts"][:10] in first_days), "flow_last": m("flow", lambda r: r["ts"][:10] in last_days),
        "vib_night": m("vib", lambda r: 1 <= r["hour"] <= 4), "vib_day": m("vib", lambda r: 8 <= r["hour"] <= 20),
        "flow_night": m("flow", lambda r: 1 <= r["hour"] <= 4), "flow_day": m("flow", lambda r: 8 <= r["hour"] <= 20),
    })
    night_p = [r["press"] for r in clean if 1 <= r["hour"] <= 4]
    day_p = [r["press"] for r in clean if 8 <= r["hour"] <= 20]
    for label, vals in (("press_fluct_night", night_p), ("press_fluct_day", day_p)):
        if vals:
            mm = med(vals)
            f[label] = max(_pct(vals, .9) - mm, mm - _pct(vals, .1)) / mm * 100
    daily = {}
    for r in clean:
        daily.setdefault(r["ts"][:10], []).append(r["vib"])
    series = [(d, med(v)) for d, v in sorted(daily.items())]
    f["daily_vib"] = series
    jumps = [(series[i][0], series[i][1] - series[i - 1][1]) for i in range(1, len(series))]
    if jumps:
        day, jump = max(jumps, key=lambda x: x[1])
        f["step_day"], f["step_mm_s"] = day, jump
    return f


def classify(f: dict) -> tuple[str, str]:
    """(issue, confidence) - the same rules a reliability engineer applies with the vibration guide open."""
    if f.get("running_hours", 0) < 24:
        return "undetermined", "low"
    if f.get("zeros", 0) >= 6:
        return "sensor_fault", "high"
    if "vib_p50" not in f:
        return "undetermined", "low"
    vib_change = (f["vib_last"] - f["vib_first"]) / f["vib_first"] * 100
    temp_rise = f["temp_last"] - f["temp_first"]
    if vib_change >= 40 and temp_rise >= 8:
        return "bearing_wear", "high"
    if f["vib_night"] >= 1.5 * f["vib_day"] and f.get("press_fluct_night", 0) > 8:
        return "cavitation", "high"
    if (f["cur_last"] - f["cur_first"]) / f["cur_first"] * 100 >= 15:
        return "overload_right_of_bep", "medium"
    if f.get("step_mm_s", 0) >= 1.0 and temp_rise < 5 and vib_change >= 40:
        return "misalignment", "medium"
    return "no_fault", "medium"


def _dx_evidence(issue: str, f: dict) -> list[str]:
    ev = []
    if issue == "bearing_wear":
        ev.append(f"Vibration median rose from {f['vib_first']:.2f} to {f['vib_last']:.2f} mm/s between the first and last "
                  f"{f['span_days']} days (last 24 h: {f['vib_24h']:.2f} mm/s).")
        ev.append(f"Bearing temperature rose with it: {f['temp_first']:.1f} -> {f['temp_last']:.1f} °C.")
    elif issue == "cavitation":
        ev.append(f"Vibration is erratic at night: median {f['vib_night']:.2f} mm/s at 01:00-04:00 vs {f['vib_day']:.2f} "
                  "mm/s during the day.")
        ev.append(f"Discharge-pressure fluctuation about ±{f['press_fluct_night']:.0f}% at night (±"
                  f"{f.get('press_fluct_day', 0):.0f}% by day); flow drops to ~{f['flow_night']:.0f} m³/h at night.")
    elif issue == "sensor_fault":
        a, b = f["zero_span"]
        ev.append(f"{f['zeros']} hourly readings of exactly 0.0 mm/s while running ({a} to {b}).")
    elif issue == "overload_right_of_bep":
        ev.append(f"Motor current median rose from {f['cur_first']:.0f} to {f['cur_last']:.0f} A while flow rose from "
                  f"{f['flow_first']:.0f} to {f['flow_last']:.0f} m³/h.")
    elif issue == "misalignment":
        ev.append(f"Step change of +{f['step_mm_s']:.2f} mm/s in daily vibration on {f['step_day']}, flat afterwards; "
                  "bearing temperature roughly unchanged.")
    else:
        ev.append(f"Vibration median {f['vib_p50']:.2f} mm/s, no trend, no night-time pattern.")
    if f.get("spikes"):
        ts, v = f["spikes"][0]
        ev.append(f"Ignored {len(f['spikes'])} single-sample spike(s) (e.g. {v:g} mm/s at {ts}) as electrical noise.")
    return ev


ACTIONS = {
    "bearing_wear": ["Replace both bearings (BRG-6309 on a KP-250) at the next possible stop",
                     "Check lubrication (interval and quantity) and avoid over-greasing"],
    "cavitation": ["Raise the minimum suction-reservoir level / check NPSH available at night",
                   "Clean the suction strainer and keep flow above the minimum for the pump"],
    "sensor_fault": ["Repair or replace the VS-10 vibration sensor", "Use process data to watch the pump meanwhile"],
    "overload_right_of_bep": ["Throttle back to the duty point (or resize)", "Check motor current against rating"],
    "misalignment": ["Laser-align pump and motor (SVC-ALIGN)", "Review the last coupling work"],
    "no_fault": ["Continue routine monitoring"],
    "undetermined": ["Retrieve the asset's telemetry and maintenance history before deciding"],
}


@scenario("day3.oneshot_dx", match=lambda r: "<day3_oneshot_diagnosis>" in r.system_text, priority=5)
def oneshot_diagnosis(req: MockRequest) -> Reply:
    text = req.conversation_text
    asset_m = ASSET_RE.search(req.last_user_text or text)
    asset = asset_m.group(0) if asset_m else "unknown"
    rows = parse_rows(text)
    if rows:
        f = features(rows)
        issue, confidence = classify(f)
        evidence = _dx_evidence(issue, f)
    else:
        issue, confidence = "undetermined", "low"
        evidence = [f"No measurements for {asset} were provided - only general manual excerpts, which list several "
                    "possible causes of vibration (bearings, misalignment, cavitation, imbalance, looseness)."]
    result = {"asset_id": asset, "issue": issue, "confidence": confidence, "evidence": evidence,
              "recommended_actions": ACTIONS[issue]}
    schema = req.output_schema
    if schema is not None:
        props = schema.get("properties") or {}
        filler = synthesize(schema, text=text)
        result = {k: (result[k] if k in result else filler.get(k)) for k in props}
        return json_reply(result, complexity=0.5)
    return say(json.dumps(result, ensure_ascii=False), complexity=0.5)


# ============================================================================================ diagnostic agent
def _json(call) -> dict:
    data = call.result_json() if call is not None else None
    return data if isinstance(data, dict) else {}


def _summary(req: MockRequest, metric: str, agg: str = "summary") -> dict | None:
    for c in reversed(req.calls("query_telemetry")):
        if c.input.get("metric") == metric and c.input.get("agg", "summary") == agg and c.result and not c.is_error:
            return _json(c)
    return None


def _asset_info(req: MockRequest) -> dict:
    return _json(next(iter(req.calls("list_work_orders")), None)).get("asset", {})


def _family_doc(req: MockRequest, asset: dict) -> str:
    """Doc id of the asset's own manual, as seen in search results (e.g. model KP-400-S -> kp400_pump_iom)."""
    token = re.sub(r"[^a-z0-9]", "", asset.get("model", "").lower())[:5]          # "kp400"
    for c in req.calls("search_manuals"):
        for h in _json(c).get("results", []):
            if token and h.get("doc", "").startswith(token):
                return h["doc"]
    return ""


def _limits_from_sections(req: MockRequest, family_doc: str) -> dict:
    """Limits quoted from sections the agent has read - and only from the asset's own manual."""
    out: dict = {}
    for c in req.calls("read_section"):
        r = _json(c)
        text, doc, section = r.get("text", ""), r.get("doc", ""), r.get("section", "")
        if doc != family_doc:
            continue
        row = re.search(r"^\|\s*Bearing housing vibration[^\n]*$", text, re.M)
        if row:
            nums = re.findall(r"\d+(?:\.\d+)?", _md(row.group(0)).split("Hz")[-1])
            if len(nums) >= 2:
                out["vib"] = (float(nums[0]), float(nums[-1]), doc, section)
        p = re.search(r"Discharge pressure fluctuation[^\n]*?>\s*±\s*(\d+)\s*%", _md(text))
        if p:
            out["press_fluct"] = (float(p.group(1)), doc, section)
        mf = re.search(r"Keep flow above \*\*(\d+) m³/h\*\*", text)
        if mf:
            out["min_flow"] = (float(mf.group(1)), doc, section)
    return out


HYPOTHESIS_QUERIES = {
    "undetermined": "standby pump test run monitoring",
    "bearing_wear": "rising vibration and bearing temperature bearing wear operating limits",
    "cavitation": "cavitation night pressure fluctuation suction split-case vibration limits",
    "sensor_fault": "vibration reading exactly 0.0 while running sensor fault",
    "misalignment": "vibration step change after coupling work misalignment alignment",
    "overload_right_of_bep": "motor current high flow beyond duty point overload",
    "no_fault": "vibration severity zones limits",
}
SECTION_PREFS = {       # sections that describe the failure signature, best first
    "bearing_wear": [r"High vibration", r"Reading trends"],
    "cavitation": [r"Cavitation", r"split-case", r"Reading trends"],
    "sensor_fault": [r"Sensor faults", r"Response procedure"],
    "misalignment": [r"High vibration", r"Reading trends"],
    "overload_right_of_bep": [r"Motor overload", r"Reading trends"],
    "no_fault": [r"Severity zones"],
    "undetermined": [r"Response procedure", r"Severity zones"],
}


def _hypothesis(vib: dict, temp: dict, req: MockRequest) -> tuple[str, list[dict]]:
    """Next hypothesis from the summaries, plus the extra queries needed to confirm it (empty = confirmed)."""
    asset = vib.get("asset_id", "")
    if vib.get("zero_readings_while_running", {}).get("count", 0) >= 6:
        return "sensor_fault", []
    if "median" not in vib or vib.get("running_hours", 0) < 24:
        return "undetermined", []                   # standby unit: not enough running data to judge
    change = vib.get("change_pct", 0.0)
    temp_rise = temp.get("last_7d_median", 0) - temp.get("first_7d_median", 0) if temp else 0
    erratic = vib.get("p90", 0) >= 1.6 * vib["median"]
    need: list[dict] = []
    if change >= 40 and temp_rise >= 8:
        for metric in ("vibration_mm_s", "bearing_temp_c"):
            if _summary(req, metric, "daily") is None:
                need.append({"asset_id": asset, "metric": metric, "agg": "daily"})
        return "bearing_wear", need
    if erratic:
        for metric in ("vibration_mm_s", "discharge_pressure_bar", "flow_m3h"):
            if _summary(req, metric, "hourly_profile") is None:
                need.append({"asset_id": asset, "metric": metric, "agg": "hourly_profile"})
        if need:
            return "cavitation?", need
        prof_v = {p["hour_utc"]: p for p in _summary(req, "vibration_mm_s", "hourly_profile").get("hourly_profile", [])}
        prof_p = {p["hour_utc"]: p for p in _summary(req, "discharge_pressure_bar", "hourly_profile").get("hourly_profile", [])}
        night = [prof_v[h]["median"] for h in range(1, 5) if h in prof_v]
        day = [prof_v[h]["median"] for h in range(8, 21) if h in prof_v]
        fluct = max((prof_p[h]["fluctuation_pct"] for h in range(1, 5) if h in prof_p), default=0)
        if night and day and statistics.median(night) >= 1.5 * statistics.median(day) and fluct > 8:
            return "cavitation", []
    if change >= 40 and temp_rise < 5:
        if _summary(req, "vibration_mm_s", "daily") is None:
            return "misalignment?", [{"asset_id": asset, "metric": "vibration_mm_s", "agg": "daily"}]
        return "misalignment", []
    cur = _summary(req, "motor_current_a")
    if cur is None:
        return "overload?", [{"asset_id": asset, "metric": "motor_current_a", "agg": "summary"}]
    if cur.get("change_pct", 0) >= 15:
        return "overload_right_of_bep", []
    return "no_fault", []


def _diagnosis_input(req: MockRequest, asset: str, issue: str) -> dict:
    vib = _summary(req, "vibration_mm_s") or {}
    temp = _summary(req, "bearing_temp_c") or {}
    info = _asset_info(req)
    limits = _limits_from_sections(req, _family_doc(req, info))
    wo = _json(next(iter(req.calls("list_work_orders")), None))
    evidence: list[str] = []
    refs: list[str] = []
    if issue == "bearing_wear":
        evidence.append(f"Vibration median rose from {vib['first_7d_median']} to {vib['last_7d_median']} mm/s (first vs "
                        f"last 7 days, {vib['change_pct']:+.0f}%); last 24 h median {vib.get('last_24h_median')} mm/s.")
        evidence.append(f"Bearing temperature rose together with it: {temp['first_7d_median']} -> "
                        f"{temp['last_7d_median']} °C (7-day medians).")
        daily = (_summary(req, "vibration_mm_s", "daily") or {}).get("daily", [])
        if "vib" in limits:
            alert, alarm, doc, section = limits["vib"]
            crossed = next((d["date"] for d in daily if d["median"] > alarm), None)
            if crossed:
                evidence.append(f"Daily vibration median first exceeded the {alarm:g} mm/s alarm limit on {crossed}.")
            refs.append(f"{doc} / {section}")
    elif issue == "cavitation":
        prof_v = _summary(req, "vibration_mm_s", "hourly_profile").get("hourly_profile", [])
        prof_p = _summary(req, "discharge_pressure_bar", "hourly_profile").get("hourly_profile", [])
        prof_f = (_summary(req, "flow_m3h", "hourly_profile") or {}).get("hourly_profile", [])
        night_v = [p["median"] for p in prof_v if 1 <= p["hour_utc"] <= 4]
        day_v = [p["median"] for p in prof_v if 8 <= p["hour_utc"] <= 20]
        fl = [p["fluctuation_pct"] for p in prof_p if 1 <= p["hour_utc"] <= 4]
        night_f = [p["median"] for p in prof_f if 1 <= p["hour_utc"] <= 4]
        evidence.append(f"Vibration median {min(night_v):.2f}-{max(night_v):.2f} mm/s at 01:00-04:00 vs about "
                        f"{statistics.median(day_v):.2f} mm/s the rest of the day.")
        day_fl = [p["fluctuation_pct"] for p in prof_p if 8 <= p["hour_utc"] <= 20]
        evidence.append(f"Discharge-pressure fluctuation ±{min(fl):.1f}-{max(fl):.1f}% at night vs about "
                        f"±{statistics.median(day_fl):.1f}% by day"
                        + (f" (> ±{limits['press_fluct'][0]:g}% = cavitation likely)." if "press_fluct" in limits else "."))
        if night_f:
            evidence.append(f"Flow drops to about {statistics.median(night_f):.0f} m³/h at night.")
        if "vib" in limits:
            alert, alarm, doc, section = limits["vib"]
            evidence.append(f"Night-time vibration exceeds the {alert:g} mm/s alert limit for a {info.get('model', '')} "
                            f"on a {info.get('foundation', '')} foundation (alarm above {alarm:g}).")
            refs.append(f"{doc} / {section}")
    elif issue == "sensor_fault":
        z = vib.get("zero_readings_while_running", {})
        evidence.append(f"{z.get('count')} readings of exactly 0.0 mm/s while running ({z.get('first')} to {z.get('last')}).")
    elif issue == "misalignment":
        daily = (_summary(req, "vibration_mm_s", "daily") or {}).get("daily", [])
        jumps = [(daily[i]["date"], daily[i]["median"] - daily[i - 1]["median"]) for i in range(1, len(daily))]
        if jumps:
            day, jump = max(jumps, key=lambda x: x[1])
            evidence.append(f"Step change of +{jump:.2f} mm/s in daily median vibration on {day}, flat afterwards.")
    elif issue == "overload_right_of_bep":
        cur = _summary(req, "motor_current_a") or {}
        evidence.append(f"Motor current median {cur.get('first_7d_median')} -> {cur.get('last_7d_median')} A "
                        f"({cur.get('change_pct', 0):+.0f}%).")
    elif issue == "undetermined":
        evidence.append(f"Only {vib.get('running_hours', 0)} running hours in the window - not enough data to judge.")
    else:
        evidence.append(f"Vibration median {vib.get('median')} mm/s (p90 {vib.get('p90')}), change "
                        f"{vib.get('change_pct', 0):+.0f}% first vs last week; no night-time or step pattern.")
    for w in wo.get("work_orders", []):
        if re.search(r"bearing|noise|pitting|hum|coupling|sensor", w.get("description", ""), re.I):
            evidence.append(f"{w['work_order_id']} ({w['date']}): {w['description']}")
    if vib.get("spikes_excluded"):
        s = vib["spikes_excluded"][0]
        evidence.append(f"Single-sample spike {s['value']} mm/s at {s['timestamp']} ignored as electrical noise.")
    for c in req.calls("read_section"):
        r = _json(c)
        ref = f"{r.get('doc')} / {r.get('section')}"
        if r.get("doc") and ref not in refs:
            refs.append(ref)
    actions = list(ACTIONS.get(issue, ACTIONS["undetermined"]))
    if issue == "cavitation" and "min_flow" in limits:
        actions[1] = f"Clean the suction strainer and keep flow above {limits['min_flow'][0]:g} m³/h"
    return {"asset_id": asset, "issue": issue, "confidence": "high" if issue in ("bearing_wear", "cavitation",
            "sensor_fault") else "medium", "evidence": evidence, "recommended_actions": actions,
            "manual_references": refs or ["(none read)"]}


@scenario("day3.diagnostic_agent", priority=5,
          match=lambda r: "<day3_diagnostic_agent>" in r.system_text and r.has_tool("query_telemetry", "read_section"))
def diagnostic_agent(req: MockRequest) -> Reply:
    m = ASSET_RE.search(req.first_user_text)
    if not m:
        return say("Which asset should I diagnose? Please give its asset ID (for example HF-KP250-01).")
    asset = m.group(0)
    if not req.called("list_work_orders"):
        return use_tools(tool("list_work_orders", asset_id=asset),
                         tool("query_telemetry", asset_id=asset, metric="vibration_mm_s", agg="summary"),
                         tool("query_telemetry", asset_id=asset, metric="bearing_temp_c", agg="summary"),
                         preface=f"I'll start with {asset}'s maintenance history and a 30-day summary of vibration and "
                                 "bearing temperature.", thinking="Get the facts first: history plus summary stats.")
    vib = _summary(req, "vibration_mm_s") or {}
    temp = _summary(req, "bearing_temp_c") or {}
    hypothesis, need = _hypothesis(vib, temp, req)
    if need:
        return use_tools(*[tool("query_telemetry", **q) for q in need],
                         preface=f"The summary suggests {hypothesis.rstrip('?').replace('_', ' ')}; I'll check the "
                                 "pattern over time before concluding.")
    issue = hypothesis
    info = _asset_info(req)
    family = re.match(r"(KP-\d{3})", info.get("model", "")) if info else None
    if not req.called("search_manuals"):
        searches = [tool("search_manuals", query=HYPOTHESIS_QUERIES.get(issue, "vibration limits"))]
        if family:
            searches.append(tool("search_manuals", query=f"{family.group(1)} vibration operating limits "
                                                         f"{info.get('foundation', '')} foundation"))
        return use_tools(*searches, preface="Now the manuals: the documented signature of this failure mode, and "
                                            f"the limits for a {info.get('model', 'pump')} on a "
                                            f"{info.get('foundation', '?')} foundation.")
    if not req.called("read_section"):
        hits = [h for c in req.calls("search_manuals") for h in _json(c).get("results", [])]
        family_doc = _family_doc(req, info)
        limits_hit = next((h for h in hits if h.get("doc") == family_doc and "Operating limits" in h.get("section", "")),
                          None)
        reads = []
        for pref in SECTION_PREFS.get(issue, []):
            hit = next((h for h in hits if re.search(pref, h.get("section", ""), re.I)
                        and (h["doc"], h["section"]) not in reads), None)
            if hit and len(reads) < 2:
                reads.append((hit["doc"], hit["section"]))
        if family_doc:
            reads.append((family_doc, limits_hit["section"] if limits_hit else "Operating limits"))
        if not reads:
            return use_tools(tool("search_manuals", query="vibration operating limits"))
        return use_tools(*[tool("read_section", doc=doc, section=sec) for doc, sec in dict.fromkeys(reads)],
                         preface="Reading the most relevant sections in full.")
    if not req.called("submit_diagnosis"):
        return use_tools(tool("submit_diagnosis", **_diagnosis_input(req, asset, issue)),
                         thinking="The data and the manual agree; record the diagnosis.")
    dx = next(iter(req.calls("submit_diagnosis"))).input
    lines = [f"{asset}: {dx['issue'].replace('_', ' ')} ({dx['confidence']} confidence)."]
    lines += [f"- {e}" for e in dx["evidence"][:3]]
    lines += ["Next: " + "; ".join(dx["recommended_actions"]) + "."]
    return say("\n".join(lines), complexity=0.3)


# ============================================================================================ fleet review
PLACEHOLDER_HINT = re.compile(r"cleared|trimmed by client", re.I)
_HDR = re.compile(r"^# asset (\S+) \| site (.+?) \| model (\S+) \| foundation (\w+) \| rated current (\d+(?:\.\d+)?) A",
                  re.M)
_NOTE = re.compile(r"^- ([A-Z]{2}-KP\d{3}X?-\d{2}) \[[^\]]*\]: (.+?) -> (OK|WATCH|ACTION|STANDBY)\b(.*)$", re.M)
_DIGEST = re.compile(r"\[trimmed by client\] ([A-Z]{2}-KP\d{3}X?-\d{2}) \([^)]*\): (.+)$", re.M)


def _limits_from_system(system: str) -> dict:
    out = {}
    for kind in ("rigid", "flexible"):
        m = re.search(kind + r"[^\n]*?alert (\d+(?:\.\d+)?)[^\n]*?alarm (\d+(?:\.\d+)?)", system, re.I)
        if m:
            out[kind] = (float(m.group(1)), float(m.group(2)))
    return out


def _asset_note(asset: str, meta: dict, f: dict, limits: dict) -> str:
    tag = f"{meta.get('model', '?')}, {meta.get('foundation', '?')}"
    if f.get("running_hours", 0) < 24:
        return f"- {asset} [{tag}]: ran {f.get('running_hours', 0)} h in the window -> STANDBY (not enough running data)"
    issue, _ = classify(f)
    alert, alarm = limits.get(meta.get("foundation", ""), (None, None))
    nums = (f"vib p50 {f['vib_p50']:.2f} / p90 {f['vib_p90']:.2f} mm/s (last 24 h {f['vib_24h']:.2f}), "
            f"temp p50 {f['temp_p50']:.1f} °C ({f['temp_last'] - f['temp_first']:+.1f}), current p50 {f['cur_p50']:.0f} A")
    reasons, status = [], "OK"
    if issue == "sensor_fault":
        a, b = f["zero_span"]
        status, reasons = "WATCH", [f"sensor fault: {f['zeros']} h of 0.0 mm/s while running ({a[5:16]} to {b[5:16]})"]
    elif issue == "bearing_wear":
        status, reasons = "ACTION", ["vibration and bearing temperature rising together (bearing wear suspected)"]
    elif issue == "cavitation":
        status, reasons = "ACTION", [f"night-time vibration {f['vib_night']:.1f} vs {f['vib_day']:.1f} mm/s by day, "
                                     f"pressure fluctuation ±{f['press_fluct_night']:.0f}% (cavitation suspected)"]
    if alarm is not None and f["vib_24h"] > alarm:
        status = "ACTION"
        reasons.insert(0, f"last-24 h vibration above the {alarm:g} mm/s alarm limit")
    elif alert is not None and (f["vib_p50"] > alert) and status == "OK":
        status, reasons = "WATCH", [f"vibration in the alert band (> {alert:g} mm/s)"]
    rated = float(meta.get("rated", 0) or 0)
    if rated and f["cur_p50"] > rated:
        if status == "OK":
            status = "WATCH"
        reasons.append(f"motor current {f['cur_p50'] / rated * 100:.0f}% of rated {rated:g} A")
    if f.get("spikes"):
        ts, v = f["spikes"][0]
        reasons.append(f"ignored single-sample spike {v:g} mm/s at {ts[5:16]} as electrical noise")
    return f"- {asset} [{tag}]: {nums} -> {status}" + (f": {'; '.join(reasons)}" if reasons else "")


def _fleet_state(req: MockRequest) -> tuple[list[str], dict, set, dict]:
    """(site order, asset -> meta, fetched asset ids, asset -> note line) from what is still in context."""
    sites: list[str] = []
    meta: dict[str, dict] = {}
    for c in req.calls("list_assets"):
        data = c.result_json()
        if isinstance(data, dict):
            data = data.get("assets")
        for a in data or []:
            meta[a["asset_id"]] = {"site": a["site"], "model": a["model"], "foundation": a["foundation"],
                                   "rated": a.get("rated_current_a")}
            if a["site"] not in sites:
                sites.append(a["site"])
    fetched = {c.input.get("asset_id") for c in req.calls("get_telemetry_rows")}
    summary = req.first_user_text if "<conversation_summary>" in req.first_user_text else ""
    fetched |= set(re.findall(r"get_telemetry_rows\(asset_id=([A-Z]{2}-KP\d{3}X?-\d{2})", summary))
    notes: dict[str, str] = {}
    for m in req.messages:
        if m.get("role") != "assistant":
            continue
        blocks = m.get("content") if isinstance(m.get("content"), list) else [{"type": "text", "text": m.get("content") or ""}]
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                for n in _NOTE.finditer(b.get("text", "")):
                    notes[n.group(1)] = n.group(0)
    for n in _NOTE.finditer(summary):
        notes.setdefault(n.group(1), n.group(0))
    return sites, meta, fetched, notes


@scenario("day3.fleet_review", priority=5,
          match=lambda r: "<day3_fleet_review>" in r.system_text and r.has_tool("get_telemetry_rows"))
def fleet_review(req: MockRequest) -> Reply:
    limits = _limits_from_system(req.system_text)
    if not req.called("list_assets") and "<conversation_summary>" not in req.first_user_text:
        return use_tools(tool("list_assets"), preface="First, the asset register.")
    sites, meta, fetched, notes = _fleet_state(req)

    # notes for freshly returned raw data (the model writes them while the rows are still in context)
    fresh: list[str] = []
    for c in req.last_tool_results:
        if c.name != "get_telemetry_rows" or not c.result or PLACEHOLDER_HINT.search(c.result[:200]):
            continue
        h = _HDR.search(c.result)
        info = dict(meta.get(c.input.get("asset_id"), {}))
        if h:
            info.update({"site": h.group(2), "model": h.group(3), "foundation": h.group(4), "rated": h.group(5)})
        asset = c.input.get("asset_id")
        note = _asset_note(asset, info, features(parse_rows(c.result)), limits)
        notes[asset] = note
        fresh.append(note)
    fresh_text = ("Notes:\n" + "\n".join(fresh) + "\n\n") if fresh else ""

    # user follow-up after the report: answer from the latest report still in context
    if not req.is_tool_result_turn and req.assistant_turns > 0 and "<conversation_summary>" not in req.last_user_text:
        report_lines = [n for n in notes.values()]
        urgent = [re.sub(r"\s*\[[^\]]*\]", "", n) for n in report_lines if re.search(r"-> (ACTION|WATCH)", n)]
        if not report_lines:
            return say("I no longer have the per-asset findings in my context, so I can't answer that reliably - "
                       "I'd need to pull the telemetry again.", complexity=0.3)
        return say("Assets that need action or a work order this week (from the fleet report above):\n"
                   + "\n".join(urgent or ["- none"]), complexity=0.3)

    remaining_sites = [s for s in sites if any(a not in fetched for a, i in meta.items() if i["site"] == s)]
    if remaining_sites:
        site = remaining_sites[0]
        batch = [a for a, i in meta.items() if i["site"] == site and a not in fetched]
        days = int(_num(r"last (\d+) days", req.first_user_text, "10")) if "<conversation_summary>" not in \
            req.first_user_text else 10
        return use_tools(*[tool("get_telemetry_rows", asset_id=a, days=days) for a in batch],
                         preface=f"{fresh_text}Next site: {site} ({len(batch)} pumps).", complexity=0.4)

    # final fleet report from everything still in context
    everything = sorted(fetched | set(notes) | set(meta), key=lambda a: (a[:2], a))
    lines = []
    for a in everything:
        if a in notes:
            lines.append(notes[a])
        else:
            lines.append(f"- {a} [?]: details not retained in context -> UNKNOWN")
    counts = {s: sum(1 for l in lines if f"-> {s}" in l) for s in ("ACTION", "WATCH", "OK", "STANDBY", "UNKNOWN")}
    head = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    return say(f"{fresh_text}Fleet report ({len(everything)} pumps: {head})\n" + "\n".join(lines), complexity=0.6)


# ============================================================================================ memory tool
_PII_HINT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{7,}\d")
_STORE_INTENT = re.compile(r"\b(remember|note (?:this|that|for next time)|for next time|keep in mind|save)\b", re.I)


def _memory_files(req: MockRequest) -> list[str]:
    for c in reversed(req.calls("memory")):
        if c.input.get("command") == "view" and c.input.get("path", "").rstrip("/") == "/memories" and c.result:
            return re.findall(r"(/memories/\S+\.md)\b", c.result)
    return []


def _viewed(req: MockRequest) -> dict[str, str]:
    out = {}
    for c in req.calls("memory"):
        if c.input.get("command") == "view" and c.result and not c.is_error and c.input.get("path", "").endswith(".md"):
            lines = [re.sub(r"^\s*\d+\t", "", ln) for ln in c.result.splitlines()[1:]]
            out[c.input["path"]] = "\n".join(lines)
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [p.strip() for p in parts if p.strip()]


def _sanitize(sentence: str) -> str:
    """Drop the personal data from a sentence, keep the preference it expresses."""
    want = re.search(r"\b(wants|prefers|asks for|needs)\b(.*)$", sentence)
    tail = (want.group(1) + want.group(2)).rstrip(".") if want else "has a contact preference"
    return f"Site contact {tail} (contact details are kept in the CRM, not in memory)."


@scenario("day3.field_memory", priority=5,
          match=lambda r: "<day3_field_memory>" in r.system_text and r.has_tool("memory"))
def field_memory(req: MockRequest) -> Reply:
    user = req.first_user_text
    calls = req.calls("memory")
    if not calls:
        return use_tools(tool("memory", command="view", path="/memories"),
                         preface="Checking my memory for this site first.")
    files = _memory_files(req)

    if _STORE_INTENT.search(user):
        sents = _sentences(user)
        facts = [s for s in sents[1:] if not re.search(r"\bI prefer\b", s)]
        prefs = [s for s in sents if re.search(r"\bI prefer\b", s)]
        site_path = "/memories/site_notes.md"
        creates = [c for c in calls if c.input.get("command") == "create" and c.input.get("path") == site_path]
        title = "# Site notes (written by the field-service assistant)\n"
        if not creates:
            # first draft writes the notes as dictated - including the contact details (the guard must catch this)
            return use_tools(tool("memory", command="create", path=site_path,
                                  file_text=title + "".join(f"- {s}\n" for s in facts)))
        last = creates[-1]
        if last.is_error and re.search(r"personal data|PII|PRV-004", last.result or "", re.I):
            if sum(1 for c in creates if c.is_error) >= 2:
                return say("I couldn't save the site notes: the memory store keeps rejecting them as personal data. "
                           "Please record them in the CRM instead.", complexity=0.2)
            flagged = re.findall(r"'([^']+)'", last.result or "")
            clean = [(_sanitize(s) if _PII_HINT.search(s) or any(f.lower() in s.lower() for f in flagged) else s)
                     for s in facts]
            return use_tools(tool("memory", command="create", path=site_path,
                                  file_text=title + "".join(f"- {s}\n" for s in clean)),
                             preface="The memory store rejected personal data (PRV-004). Saving the notes without it.")
        pref_path = "/memories/technician_preferences.md"
        if prefs and not any(c.input.get("path") == pref_path for c in calls):
            text = "# Technician preferences\n" + "".join(
                f"- {re.sub(r'^.*?I prefer ', 'Prefers ', p)}\n" for p in prefs)
            return use_tools(tool("memory", command="create", path=pref_path, file_text=text))
        saved = [c.input.get("path") for c in calls if c.input.get("command") == "create" and not c.is_error]
        rejected = any(c.is_error for c in calls if c.input.get("command") == "create")
        msg = f"Saved for next time: {', '.join(dict.fromkeys(saved))}."
        if rejected:
            msg += (" I left out the site contact's personal details: customer personal data may not be kept in "
                    "long-term memory (PRV-004), so only the preference itself is noted.")
        return say(msg, complexity=0.2)

    # recall: read every note file once, then answer only from what the files say
    viewed = _viewed(req)
    unread = [f for f in files if f not in viewed]
    if unread:
        return use_tools(*[tool("memory", command="view", path=f) for f in unread])
    if not viewed:
        return say("I have no notes for this site yet, so I can't tell you its stop windows or preferences.")
    q_terms = set(re.findall(r"[a-z0-9-]{3,}", user.lower()))
    notes = [ln[2:] for text in viewed.values() for ln in text.splitlines() if ln.startswith("- ")]
    relevant = [n for n in notes if q_terms & set(re.findall(r"[a-z0-9-]{3,}", n.lower()))] or notes
    others = [n for n in notes if n not in relevant]
    answer = ["From my notes for this site:"] + [f"- {n}" for n in relevant]
    if others:
        answer += ["Also noted:"] + [f"- {n}" for n in others]
    return say("\n".join(answer), complexity=0.3)
