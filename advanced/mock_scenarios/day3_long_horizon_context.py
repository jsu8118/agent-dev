"""Mock policies for the advanced Day 3 - long-horizon context: the field-service agent and its helpers.

Every policy matches on a marker in the system prompt and derives everything it "says" from the request: the
tool results in context, the technician's own words, a summary / scratchpad / memory note the harness put in a
user turn, or the documents it was given. Like a real model, the policies can only report what survived in
context - which is exactly what the labs measure (probe questions, the end-of-day report, the recall of notes).

  adv.day3.field_agent   <adv_day3_field_agent>   labs 01-04, ex05, ex11   the technician's day (40 turns); it also
                                                                           answers the harness's summary and
                                                                           scratchpad requests (a fork that reuses
                                                                           the cached prefix, lab 02)
  adv.day3.site_reader   <adv_day3_site_reader>   lab 06, ex12             one site log -> the summary contract, or
                                                                           one drill-down answer with its line number
  adv.day3.planner       <adv_day3_planner>       lab 06                   plans the day from raw logs or summaries
  adv.day3.memory_agent  <adv_day3_memory_agent>  lab 05                   reads memory before acting and before
                                                                           writing; memory is data, not instructions
  adv.day3.consolidator  <adv_day3_consolidate>   lab 05                   episodic notes -> semantic facts
  adv.day3.cache_lab     <adv_day3_cache_lab>     lab 07, ex10             answers from the cached reference card;
                                                                           reads sections one at a time or in parallel

The mock does not pace, summarise or remember by itself. Where a policy stands in for a behaviour the docs
describe (task-budget pacing, a summary that follows its instructions, a model that re-reads a source), the lab
prints a [mock] note saying so.
"""

from __future__ import annotations

import json
import re

from labkit.mock import MockRequest, Reply, json_reply, say, scenario, tool, use_tools
from labkit.mock.tokens import block_tokens

FIELD = "<adv_day3_field_agent>"
PROBE_PREFIX = "Quick check from today's session:"
WRAPPERS = ("<conversation_summary>", "<scratchpad>", "<memory", "<system-reminder>", "<site_summary")
SUMMARY_SOURCES = ("conversation summary", "scratchpad", "memory")

SITE_NAMES = {"gbwd": ("granite bay", "gbwd"), "harbor": ("harbor foods", "harbor"), "riverbend": ("riverbend",),
              "cedar": ("cedar creek", "cedar"), "cobalt": ("cobalt",), "westfield": ("westfield",)}
NEXT_STOP = {"gbwd": "Harbor Foods", "harbor": "Riverbend Brewing", "riverbend": "Cedar Creek Dairy",
             "cedar": "Cobalt Chemical", "cobalt": "Westfield Hospital", "westfield": None}
ASSET_RE = re.compile(r"\b(?:[A-Z]{2}-KP\d{3}X?-\d{2}|KP\d{3}-\d{4}-\d{4}|KC1-[A-Z0-9-]+)\b")
CITATION = re.compile(r"\[([A-Z0-9-]+ §[\d.]+)\]")
_STOP = set("a an the and or of to in on for at is are be it its this that what which when how do does did i we "
            "you my me our your from with as by into if any all so than then there here about after before while "
            "please today session quick check todays what's whats was were will would can could should must "
            "use used using".split())


# ============================================================================================ small helpers
def _site_of(text: str) -> str | None:
    m = re.search(r"\(site ([A-Za-z]+)\)", text)
    if m and m.group(1).lower() in SITE_NAMES:
        return m.group(1).lower()
    low = text.lower()
    for site_id, names in SITE_NAMES.items():
        if any(n in low for n in names):
            return site_id
    return None


def _text_blocks(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def _is_wrapper(text: str) -> bool:
    return text.lstrip().startswith(WRAPPERS)


def _turn_start(req: MockRequest) -> int:
    """Index of the technician's current message: the newest user message with text that is not a wrapper."""
    for i in range(len(req.messages) - 1, -1, -1):
        m = req.messages[i]
        if m.get("role") == "user" and any(t.strip() and not _is_wrapper(t) for t in _text_blocks(m)):
            return i
    return -1


def _current_user_text(req: MockRequest) -> str:
    i = _turn_start(req)
    if i < 0:
        return ""
    texts = [t for t in _text_blocks(req.messages[i]) if t.strip() and not _is_wrapper(t)]
    return texts[-1]


def _results(req: MockRequest) -> list[tuple[str, dict, str]]:
    return [(c.name, c.input, c.result or "") for c in req.last_tool_results]


def _section_in_context(req: MockRequest, doc: str, section: str) -> str | None:
    """Text of a manual section already read in this conversation (a model answers from what it has)."""
    head = f"{doc} §{section} "
    for c in reversed(req.calls("read_manual_section")):
        if c.result and not c.is_error and c.result.startswith(head):
            return c.result
    return None


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s", "ure", "al"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> set[str]:
    return {_stem(t) for t in re.findall(r"[a-z0-9][a-z0-9·.-]*[a-z0-9]|[a-z0-9]", text.lower())
            if t not in _STOP and len(t) >= 3}


def _matched(q_terms: set[str], line: str) -> set[str]:
    """Question terms present in a line: exact stems, or one contained in the other for longer words
    ('grease' in 'regrease', 'temperat' in 'overtemperat')."""
    line_terms = _terms(line)
    out = set()
    for q in q_terms:
        if q in line_terms or (len(q) >= 5 and any((q in t or t in q) and len(t) >= 5 for t in line_terms)):
            out.add(q)
    return out


def _reminders(req: MockRequest) -> str:
    """Operator instructions in force for this turn: mid-conversation system messages the model still sees
    (persistent ones, and turn-scoped ones no later user message has cleared), plus <system-reminder> text blocks
    injected into the current turn's user messages (the pre-beta form)."""
    parts = list(req.system_messages)
    start = _turn_start(req)
    for m in req.messages[max(start, 0):]:
        if m.get("role") == "user":
            parts += [t for t in _text_blocks(m) if t.lstrip().startswith("<system-reminder>")]
    return "\n".join(parts).lower()


def _budget(req: MockRequest) -> tuple[float, int, int] | None:
    """(remaining fraction, spent, total) of a task budget - what the model reads off the countdown the API
    injects: what it generated so far (its text, tool calls and an estimate of the thinking behind them) plus the
    tool results it read. When the client rewrote history it passes `remaining` and that wins."""
    total = req.task_budget
    if not total:
        return None
    budget = req.output_config.get("task_budget") or {}
    if isinstance(budget.get("remaining"), int):
        remaining = budget["remaining"]
        return max(remaining, 0) / total, total - remaining, total
    spent = 0
    for m in req.messages:
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if m.get("role") == "assistant" and b.get("type") in ("text", "tool_use"):
                spent += block_tokens(b) + (180 if b.get("type") == "text" else 0)   # + the thinking it took
            elif m.get("role") == "user" and b.get("type") == "tool_result":
                spent += block_tokens(b)
    return max(total - spent, 0) / total, spent, total


def _reply(req: MockRequest, text: str, complexity: float = 0.5, thinking: str | None = None) -> Reply:
    """A text reply shaped by the reminders in force and paced against the task budget, if any."""
    reminders = _reminders(req)
    if "bullet" in reminders:
        cap = 3 if re.search(r"three|\b3\b", reminders) else 5
        parts = [p.strip() for p in re.split(r"(?<=[.;])\s+", text) if p.strip()][:cap]
        text = "\n".join("- " + p for p in parts)
    budget = _budget(req)
    if budget is not None:
        frac, _, _ = budget
        if frac <= 0.0:
            first = re.split(r"(?<=[.;])\s+", text)[0].rstrip(".;")
            text = first + ". (Task budget spent: essentials only.)"
            complexity = 0.1
        elif frac < 0.25:
            complexity = min(complexity, 0.2)
        elif frac < 0.5:
            complexity = min(complexity, 0.35)
    return say(text, complexity=complexity, thinking=thinking)


# ============================================================================================ answer builders
def _row(text: str, pattern: str) -> list[str]:
    """Cells of the first markdown table row whose text matches `pattern`."""
    for line in text.splitlines():
        if line.startswith("|") and re.search(pattern, line, re.I):
            return [c.strip().replace("**", "") for c in line.strip().strip("|").split("|")]
    return []


def _line(text: str, pattern: str) -> str:
    m = re.search(pattern, text.replace("**", ""), re.I)
    return m.group(0) if m else ""


def _section_answer(question: str, doc: str, section: str, text: str) -> str:
    """What a technician needs from one section, extracted from the section's own text."""
    q = question.lower()
    body = text.replace("**", "")
    ref = f"[{doc} §{section}]"
    if doc == "IOM-KP250" and section == "3":
        par, ang = _row(body, r"Parallel"), _row(body, r"Angular")
        recheck = _line(body, r"Re-check alignment after the first \d+ operating hours, then annually")
        return (f"Alignment tolerances for the KP-250: parallel (radial) offset {par[1] if par else '?'}, angular "
                f"{ang[1] if ang else '?'}. {recheck}; Kestrel offers laser alignment (SVC-ALIGN). {ref}")
    if doc == "IOM-KP250" and section == "9":
        rows = [_row(body, p) for p in (r"Impeller nut", r"Casing cover", r"Baseplate", r"Coupling hub")]
        return "Tightening torques for the KP-250: " + ", ".join(f"{r[0]} {r[1]}" for r in rows if r) + f". {ref}"
    if doc == "IOM-KP250" and section == "6":
        grease, replace, dry = _row(body, r"Regrease both bearings"), _row(body, r"Replace bearings"), _row(body, r"dry-run")
        parts = []
        if replace and re.search(r"bearings and seal kit|replace", q):
            parts.append(f"{replace[1]} ({replace[0].lower()})")
        if grease and re.search(r"grease|regreas|interval", q):
            parts.append(f"{grease[1]} ({grease[0].lower()})")
        if dry and re.search(r"dry", q):
            parts.append(f"{dry[1]} ({dry[0].lower()})")
        if not parts and grease:
            parts.append(f"{grease[1]} ({grease[0].lower()})")
        return "KP-250 maintenance: " + " ".join(p.rstrip(".") + "." for p in parts) + f" {ref}"
    if doc == "IOM-KP250" and section == "7.5":
        over = _row(body, r"Over-greasing")
        return (f"A bearing running hot right after maintenance is most often {over[0].lower()}: {over[1]} {ref}"
                if over else f"Bearing overheating causes are listed in {ref}.")
    if doc == "IOM-KP250" and section == "7.4":
        dry = _row(body, r"Dry running")
        return (f"Seal leakage after dry running: {dry[1]} Any steady drip after the first 4 hours of run-in "
                f"indicates a problem. {ref}" if dry else f"Seal leakage causes are listed in {ref}.")
    if doc == "IOM-KP250" and section == "7.6":
        rows = [_row(body, p) for p in (r"beyond the end of the curve", r"density or viscosity", r"Impeller rubbing")]
        return "Motor overload: " + "; ".join(f"{r[0]} -> {r[1]}" for r in rows if r) + f". {ref}"
    if doc == "IOM-KP400" and section == "6":
        cav = _row(body, r"Cavitation noise")
        return (f"KP-400 at partial load: {cav[1]} - {cav[2]} {ref}" if cav else f"Split-case troubleshooting: {ref}")
    if doc == "IOM-KP400" and section == "4":
        vib = _row(body, r"vibration")
        return (f"KP-400 vibration limits on a flexible foundation: normal {vib[1]}, alert {vib[2]}, alarm/shutdown "
                f"{vib[3]}. {ref}" if vib else f"Operating limits: {ref}")
    if doc == "IOM-KP400" and section == "5":
        grease = _row(body, r"Regrease each bearing")
        return (f"KP-400 bearings: {grease[1]}, {grease[0].lower()}. {ref}" if grease
                else f"KP-400 maintenance schedule: {ref}")
    if doc == "UM-KC1" and section == "4":
        code = next((c for c in re.findall(r"\bF\d\d\b", question) if _row(body, rf"\|\s*{c}\s*\|")), None)
        if code:
            r = _row(body, rf"\|\s*{code}\s*\|")
            safety = " (a SAFETY fault)" if "SAFETY" in r[1] else ""
            name = re.sub(r"\s*[-—]\s*SAFETY", "", r[1]).strip()
            return f"{code} = {name}{safety}. Probable causes: {r[2]}. Remedy: {r[3]} {ref}"
        safety = [r[0] for r in (_row(body, p) for p in (r"\|\s*F07", r"\|\s*F17")) if r]
        return f"Safety faults in the fault table: {', '.join(safety)}. {ref}"
    if doc == "UM-KC1" and section == "7":
        pct = _line(body, r"\d+% of F05 events were resolved by cleaning the heatsink and replacing a worn KC-1-FAN")
        proactive = _line(body, r"Fans older than [\d,]+ hours should be replaced proactively")
        return f"Application note AN-KC1-05: {pct}. {proactive}. {ref}"
    if doc == "UM-KC1" and section == "5":
        rule = _line(body, r"SAFETY faults \(F07, F17\): only one reset is allowed before inspection[^.]*\.")
        return f"Reset rule: {rule} Other faults: remove the cause and press STOP/RESET for 2 seconds. {ref}"
    if doc == "UM-KC1" and section == "3":
        rows = [_row(body, rf"\|\s*{p}\s*\|") for p in ("P01", "P02", "P03", "P04", "P05", "P10", "P20")]
        return ("KC-1 key parameters (defaults): " + ", ".join(f"{r[0]} {r[1]} = {r[2]}" for r in rows if r)
                + f". Set P01 from the motor nameplate first. {ref}")
    if doc == "SEAL-FA-02" and section == "2":
        r = _row(body, r"heat cracks")
        return (f"Radial heat cracks with hard, charred elastomers mean {r[1].lower()}; warranty: {r[2]}. {ref}" if r
                else f"Seal evidence table: {ref}")
    if doc == "SOP-SUP-007" and section == "1":
        atex = _line(body, r"A fault on explosion-proof \(ATEX\) equipment[^.]*\.")
        return f"P1 escalation applies: {atex} Target: on-call FSE, 1-hour response, 24/7. {ref}"
    # generic: the two lines that share most terms with the question
    q_terms = _terms(question)
    scored = sorted(((len(q_terms & _terms(ln)), i, ln.strip()) for i, ln in enumerate(body.splitlines())
                     if ln.strip() and not ln.startswith("#")), key=lambda x: (-x[0], x[1]))
    picked = [ln for score, _, ln in scored[:2] if score >= 2]
    return (" ".join(picked) if picked else body.splitlines()[0]) + f" {ref}"


METRIC_LABELS = {"vibration_mm_s": "vibration", "bearing_temp_c": "bearing temperature", "flow_m3h": "flow",
                 "discharge_pressure_bar": "discharge pressure", "motor_current_a": "motor current"}


def _telemetry_answer(result: dict) -> str:
    asset, metric, unit = result.get("asset_id"), result.get("metric", ""), result.get("unit", "")
    label = METRIC_LABELS.get(metric, metric.replace("_", " "))
    zeros = result.get("zero_readings_while_running")
    if zeros:
        return (f"{asset} {label}: {zeros['count']} readings of exactly 0.0 while running ({zeros['first'][:16]} to "
                f"{zeros['last'][:16]}) - a sensor or cable fault, not a machine fault (CM-GUIDE-01 §4). The reading "
                f"is not valid; check the VS-10 and its cable, and watch the process data meanwhile.")
    if "daily_median" in result:
        series = list(result["daily_median"].items())
        jumps = [(series[i][0], round(series[i][1] - series[i - 1][1], 2)) for i in range(1, len(series))]
        day, jump = max(jumps, key=lambda x: x[1]) if jumps else ("?", 0.0)
        return (f"{asset} daily {label} medians: {series[0][1]} {unit} on {series[0][0]} -> {series[-1][1]} {unit} "
                f"on {series[-1][0]}; largest day-over-day step +{jump} {unit} on {day}.")
    if "median" not in result:
        return f"{asset}: {result.get('note', 'no data')}."
    first, last, change = result.get("first_7d_median"), result.get("last_7d_median"), result.get("change_pct")
    return (f"{asset} {label} (30 d): median {result['median']} {unit}, p90 {result['p90']} {unit}; first week "
            f"{first} -> last week {last} {unit} ({change:+.0f}%); last 24 h median "
            f"{result.get('last_24h_median', last)} {unit}.")


def _arrival_answer(brief: dict, log: str, escalate_hint: bool) -> str:
    units = ", ".join(f"{u['unit_id']} ({u['model']}, {u['service']})" for u in brief.get("units", []))
    open_items = brief.get("work_orders_last_12_months") or brief.get("open_tickets") or []
    head = log.split("READINGS:")[0].split("FAULTS:")[0]
    notes = re.findall(r"^\s+(.+)$", head.split("NOTES:")[-1], re.M)
    counts: dict[str, int] = {}
    latest: dict[str, str] = {}
    for m in re.finditer(r"^(\d{4}-\d\d-\d\d \d\d:\d\d)\s+(F\d\d)", log, re.M):
        counts[m.group(2)] = counts.get(m.group(2), 0) + 1
        latest[m.group(2)] = m.group(1)
    fault_line = ""
    if counts:
        top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:3]
        fault_line = " Controller log: " + ", ".join(f"{c} x{n} (latest {latest[c]})" for c, n in top) + "."
    readings = len(re.findall(r"^\d{4}-\d\d-\d\dT\d\d:00:00Z ", log, re.M))
    text = (f"At {brief['plant']} ({brief['customer']}). Units: {units}. Recent work orders / tickets: "
            f"{'; '.join(open_items[-3:]) or 'none'}. Shift-log notes: {'; '.join(notes[:5]) or 'none'}.{fault_line}")
    if readings:
        text += f" Historian export: {readings} hourly readings scanned."
    if brief.get("hazardous_area"):
        text += f" Hazardous-area rules: {brief['hazardous_area']}"
    if brief.get("criticality"):
        text += f" Criticality: {brief['criticality']}."
    if escalate_hint:
        text += " Reminder in force: safety faults here are escalated, not repaired."
    return text


# ============================================================================================ context search
_SPLIT_USER = re.compile(r"\n|\s+\|\s+|(?<=\));\s+|(?<=[.;?])\s+(?=[A-Z0-9(\[])")


def _context_lines(req: MockRequest, *, skip_current: bool = True) -> list[tuple[str, str, int]]:
    """(source, line, position) for everything in context that a model could quote: tool results, documents, its
    own earlier replies, the technician's words, and summaries / scratchpads / memory notes in user turns."""
    out: list[tuple[str, str, int]] = []
    current = _turn_start(req) if skip_current else -1
    pos = 0
    for i, m in enumerate(req.messages):
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "tool_result":
                raw = b.get("content")
                text = raw if isinstance(raw, str) else "\n".join(x.get("text", "") for x in raw or [] if isinstance(x, dict))
                if "cleared by context management" in text[:80]:
                    continue
                if text.startswith("{"):
                    try:
                        data = json.loads(text)
                        text = "\n".join(f"{k}: {v}" for k, v in data.items())
                    except ValueError:
                        pass
                for ln in text.splitlines():
                    pos += 1
                    out.append(("tool result", ln.strip(), pos))
            elif kind == "document":
                source = b.get("source") or {}
                for ln in str(source.get("data", "")).splitlines():
                    pos += 1
                    out.append(("document", ln.strip(), pos))
            elif kind == "text" and m.get("role") in ("user", "assistant"):
                text = b.get("text", "")
                if m.get("role") == "user":
                    if i == current and not _is_wrapper(text):
                        continue
                    source = ("conversation summary" if "<conversation_summary>" in text else
                              "scratchpad" if "<scratchpad>" in text else "memory" if "<memory" in text
                              else "site summary" if "<site_summary" in text else "the technician's words")
                else:
                    source = "my own earlier reply"
                for ln in _SPLIT_USER.split(text):
                    pos += 1
                    out.append((source, ln.strip(), pos))
    return out


UNIT_HINTS = [("torque", r"N·m"), ("temperature", r"°C"), ("grease", r"\b\d+ g\b"), ("flow", r"m³/h"),
              ("current", r"\d A\b"), ("offset", r"\bmm\b"), ("tolerance", r"\bmm\b"), ("window", r"\d\d:\d\d"),
              ("interval", r"hours"), ("code", r"\bF\d\d\b")]
NOT_KNOWN = ("I don't have that in my context any more - it was in a part of the day that has been summarised, "
             "cleared or trimmed away. I can re-read the source if you want.")


def _best_line(req: MockRequest, question: str) -> tuple[str, str] | None:
    """The line in context that best answers the question: shared terms (stem- and substring-aware), a unit that
    fits the question, the right product family, and a number or code in it. Questions are not answers."""
    body = question.replace(PROBE_PREFIX, "").strip()
    q_terms = _terms(body)
    products = {re.sub(r"[^a-z0-9]", "", p.lower()).rstrip("s") for p in re.findall(r"\bK[PC]-?\d{1,3}s?\b", body, re.I)}
    prefer_recent = bool(re.search(r"recent|latest|last\b", body, re.I))
    wants_record = bool(re.search(r"\blogged\b|\brecorded\b|\breading", body, re.I))
    best: tuple[float, int, str, str] | None = None
    for source, line, pos in _context_lines(req):
        if len(line) < 8 or line.startswith((PROBE_PREFIX, "#", "From today's session")) or line.rstrip().endswith("?"):
            continue
        score = float(len(_matched(q_terms, line)))
        for word, unit in UNIT_HINTS:
            if any(q.startswith(word[:6]) for q in q_terms) and re.search(unit, line):
                score += 1.5
        if products:
            norm = re.sub(r"[^a-z0-9]", "", line.lower())
            if not any(p in norm for p in products):
                score -= 0.5
        if not re.search(r"\d", line):
            score -= 1.0                                   # facts worth checking carry a number or a code
        if wants_record and re.match(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d", line):
            score += 1.0                                   # "what was logged" is answered by a log entry
        if score < 2.5:
            continue
        key = (score, pos if prefer_recent else -pos)
        if best is None or key > (best[0], best[1]):
            best = (score, key[1], source, line)
    return (best[2], best[3]) if best else None


def _answer_from_context(req: MockRequest, question: str) -> str:
    hit = _best_line(req, question)
    return f"From today's session ({hit[0]}): {hit[1]}" if hit else NOT_KNOWN


# ============================================================================================ findings / notes / report
_FINDING_LINE = re.compile(r"finding:\s*(\w+)\s*/\s*(\S+)\s*-\s*(.+?)(?:;\s*action:\s*(.+?))?(?:;\s*parts:\s*(.+))?$", re.I)
_FINDING_CALL = re.compile(r"log_finding\(site_id=(\w+), unit_id=([^,]+), finding=(.+?), action=(.+?), parts=\[(.*?)\]")


def _findings_in_context(req: MockRequest) -> list[dict]:
    """Findings the model can still see: its log_finding calls (the inputs survive tool-result clearing), and
    finding lines a summary, a scratchpad or a compaction summary carried forward."""
    found: list[dict] = []

    def add(site, unit, finding, action, parts):
        if not any(f["unit"] == unit and f["finding"] == finding for f in found):
            found.append({"site": site, "unit": unit, "finding": finding.strip(), "action": (action or "").strip(),
                          "parts": [p.strip().strip("'\"") for p in parts if p.strip().strip("'\"")]})

    for c in req.calls("log_finding"):
        if c.is_error:
            continue
        rec = (c.result_json() or {}).get("recorded") or dict(c.input)
        add(rec.get("site_id", "?"), rec.get("unit_id", "?"), rec.get("finding", ""), rec.get("action", ""),
            rec.get("parts") or [])
    for source, line, _ in _context_lines(req, skip_current=False):
        if source not in SUMMARY_SOURCES:
            continue
        m = _FINDING_LINE.match(line)
        if m:
            add(m.group(1).lower(), m.group(2), m.group(3), m.group(4), (m.group(5) or "").replace("none", "").split(","))
            continue
        for m in _FINDING_CALL.finditer(line):
            add(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5).split(","))
    return found


def _sites_in_context(req: MockRequest) -> list[str]:
    visited: list[str] = []
    for c in req.calls("get_site_brief"):
        if c.input.get("site_id") and c.input["site_id"] not in visited:
            visited.append(c.input["site_id"])
    for source, line, _ in _context_lines(req, skip_current=False):
        if source not in SUMMARY_SOURCES:
            continue
        for m in re.finditer(r"get_site_brief\(site_id=(\w+)\)|\bsite[s]?(?: visited so far)?:?\s+([a-z, ]+)", line):
            for site in re.findall(r"[a-z]+", (m.group(1) or m.group(2) or "")):
                if site in SITE_NAMES and site not in visited:
                    visited.append(site)
    order = list(SITE_NAMES)
    return sorted(visited, key=order.index)


def _remember_notes(req: MockRequest) -> list[tuple[str, str]]:
    """(site, note) for every 'remember for next time' still in context: the technician's own words, or remember
    lines a summary, scratchpad or memory note carried forward."""
    notes: list[tuple[str, str]] = []

    def add(site, note):
        note = " ".join(note.split()).rstrip(".")
        if note and all(n != note for _, n in notes):
            notes.append((site, note))

    for m in req.messages:
        if m.get("role") != "user":
            continue
        for text in _text_blocks(m):
            if _is_wrapper(text):
                for line in text.splitlines():
                    mm = re.match(r"\s*remember(?: \((\w+)\))?:\s*(.+)$", line, re.I)
                    if mm:
                        add((mm.group(1) or _site_of(mm.group(2)) or "other").lower(), mm.group(2))
                continue
            mm = re.search(r"Remember for next time:\s*(.+?)(?:\s+(?:Next|Last) stop:.*)?$", text, re.S)
            if mm:
                head = re.split(r"(?:Next|Last) stop:", text)[0]
                add(_site_of(head) or "other", mm.group(1))
    return notes


def _report(req: MockRequest) -> str:
    findings = _findings_in_context(req)
    visited = _sites_in_context(req)
    lines = [f"End-of-day report, {len(visited)} site(s) in my context: {', '.join(visited) or 'none'}."]
    parts_used: list[str] = []
    for site in visited:
        mine = [f for f in findings if f["site"] == site]
        if not mine:
            lines.append(f"- {site}: no findings left in my context (that part of the day was trimmed away).")
            continue
        done = "; ".join(f"{f['unit']}: {f['finding']}" for f in mine)
        open_items = [f"{f['unit']}: {f['action']}" for f in mine
                      if re.search(r"book|schedul|escalat|recommend|review|to be", f["action"], re.I)]
        lines.append(f"- {site}: {done}. Open: {'; '.join(open_items) or 'none'}.")
        for f in mine:
            parts_used.extend(p for p in f["parts"] if p not in parts_used)
    lines.append(f"Parts used today (restock): {', '.join(parts_used) or 'none in my context'}.")
    return "\n".join(lines)


def _recall(req: MockRequest) -> str:
    notes = _remember_notes(req)
    if not notes:
        return "I have no 'remember for next time' notes left in my context - they were in turns that are gone."
    by_site: dict[str, list[str]] = {}
    for site, note in notes:
        by_site.setdefault(site, []).append(note)
    return "Noted for next time:\n" + "\n".join(f"- {site}: {'; '.join(items)}" for site, items in by_site.items())


# ============================================================================================ harness requests
def _wants(instructions: str, kind: str) -> bool:
    """What a summary keeps: findings, notes and cited facts by default; open items and fault events only when the
    instructions ask for them. A real model follows the whole instruction text; the stand-in reads keywords."""
    low = instructions.lower()
    if kind in ("findings", "remember", "facts"):
        return True
    if kind == "log_notes":
        return bool(re.search(r"open item|log note|parts on site|safety note", low))
    if kind == "fault_events":
        return bool(re.search(r"fault|event|reading|measure", low))
    return False


def _cited_replies(req: MockRequest, *, window_only: bool = False) -> list[tuple[str, str]]:
    """(site, reply) for the model's own replies that quote a cited source with a number - the facts worth keeping."""
    out: list[tuple[str, str]] = []
    site = None
    for m in req.messages:
        if m.get("role") == "user":
            for t in _text_blocks(m):
                if not _is_wrapper(t) and _site_of(t.split("Next stop:")[0].split("Last stop:")[0]):
                    site = _site_of(t.split("Next stop:")[0].split("Last stop:")[0])
        if m.get("role") != "assistant":
            continue
        for t in _text_blocks(m):
            if CITATION.search(t) and re.search(r"\d", CITATION.sub("", t)):
                out.append((site or "?", " ".join(t.split())))
    for source, line, _ in _context_lines(req, skip_current=False):
        if source in SUMMARY_SOURCES and not window_only:
            mm = re.match(r"fact(?: \((\w+)\))?:\s*(.+)$", line, re.I)
            if mm:
                out.append(((mm.group(1) or "?").lower(), mm.group(2)))
    seen, unique = set(), []
    for s, f in out:
        if f not in seen:
            seen.add(f)
            unique.append((s, f))
    return unique


def _log_notes(req: MockRequest) -> list[tuple[str, str]]:
    notes: list[tuple[str, str]] = []
    for c in req.calls("get_site_log"):
        if not c.result or c.result.startswith("["):
            continue
        for ln in c.result.split("READINGS:")[0].split("FAULTS:")[0].splitlines():
            if re.match(r"\s+(OPEN|SAFETY|PARTS):", ln):
                notes.append((c.input.get("site_id", "?"), ln.strip()))
    return notes


def _fault_events(req: MockRequest) -> list[tuple[str, str]]:
    """The latest controller-log entry per fault code and site (what 'keep fault events' preserves)."""
    events: list[tuple[str, str]] = []
    for c in req.calls("get_site_log"):
        if not c.result or "FAULTS:" not in c.result:
            continue
        latest: dict[str, str] = {}
        count: dict[str, int] = {}
        for ln in c.result.split("FAULTS:")[1].splitlines():
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d)\s+(F\d\d)\s+(.+)$", ln.strip())
            if m:
                latest[m.group(2)] = " ".join(ln.split())
                count[m.group(2)] = count.get(m.group(2), 0) + 1
        for code in sorted(latest, key=lambda k: -count[k])[:2]:
            events.append((c.input.get("site_id", "?"), f"{code} x{count[code]}, latest {latest[code]}"))
    return events


def _summary(req: MockRequest, instructions: str) -> str:
    """A conversation summary for simple compaction: findings, notes and cited facts (plus log notes and fault
    events when the instructions ask for them) - everything else, including raw readings, is dropped."""
    sites = _sites_in_context(req)
    lines = [f"Sites visited so far: {', '.join(sites) or 'none'} (current: {sites[-1] if sites else '?'})."]
    lines += [f"finding: {f['site']} / {f['unit']} - {f['finding']}; action: {f['action'] or 'none'}; parts: "
              f"{', '.join(f['parts']) or 'none'}" for f in _findings_in_context(req)]
    lines += [f"remember ({site}): {note}" for site, note in _remember_notes(req)]
    lines += [f"fact ({site}): {fact}" for site, fact in _cited_replies(req)]
    if _wants(instructions, "log_notes"):
        lines += [f"log note ({site}): {note}" for site, note in _log_notes(req)]
    if _wants(instructions, "fault_events"):
        lines += [f"fault event ({site}): {ev}" for site, ev in _fault_events(req)]
    return "\n".join(lines)


def _scratchpad(req: MockRequest, site: str) -> dict:
    """One site's state, in the fields the harness's schema asks for (the schema is the contract)."""
    findings = [f for f in _findings_in_context(req) if f["site"] == site]
    entry = {
        "site_id": site,
        "findings": [{"unit": f["unit"], "finding": f["finding"], "action": f["action"] or "none",
                      "parts": f["parts"]} for f in findings],
        "facts": [{"fact": fact, "source": (CITATION.search(fact).group(1) if CITATION.search(fact) else "reply")}
                  for s, fact in _cited_replies(req, window_only=True) if s == site],
        "open_items": [f"{f['unit']}: {f['action']}" for f in findings
                       if re.search(r"book|schedul|escalat|recommend|review|to be", f["action"], re.I)],
        "remember": [note for s, note in _remember_notes(req) if s == site],
        "parts_used": sorted({p for f in findings for p in f["parts"]}),
        "fault_events": [ev for s, ev in _fault_events(req) if s == site],
    }
    allowed = set(((req.output_schema or {}).get("properties") or {}).keys()) or set(entry)
    return {k: v for k, v in entry.items() if k in allowed}


# ============================================================================================ the field agent
READS = [  # question pattern -> manual sections it needs
    (r"alignment tolerance", [("IOM-KP250", "3")]),
    (r"torque values", [("IOM-KP250", "9")]),
    (r"which bearings and seal kit", [("IOM-KP250", "6")]),
    (r"how much grease goes into each bearing", [("IOM-KP250", "6"), ("IOM-KP250", "7.5")]),
    (r"regreasing the kp-250", [("IOM-KP250", "6")]),
    (r"flow must a kp-400 stay above", [("IOM-KP400", "6"), ("IOM-KP400", "4")]),
    (r"kp-400s?: how much grease|while i'm at the kp-400s", [("IOM-KP400", "5")]),
    (r"\bf05\b.*application note", [("UM-KC1", "4"), ("UM-KC1", "7")]),
    (r"fan hour counter", [("UM-KC1", "7")]),
    (r"\bf10\b", [("UM-KC1", "4")]),
    (r"\bf07\b", [("UM-KC1", "4"), ("UM-KC1", "5")]),
    (r"heat cracks", [("SEAL-FA-02", "2")]),
    (r"seal leakage from dry running", [("IOM-KP250", "7.4")]),
    (r"beyond the end of the curve|motor overload", [("IOM-KP250", "7.6")]),
    (r"key parameters", [("UM-KC1", "3")]),
    (r"safety faults", [("UM-KC1", "4"), ("UM-KC1", "5")]),
]


def _needs_sop(req: MockRequest, low: str) -> bool:
    return bool(re.search(r"\bf07\b", low) and (re.search(r"atex|zone 1", low) or re.search(r"atex|hazardous", _reminders(req))))


def _wanted_sections(req: MockRequest, low: str) -> list[tuple[str, str]]:
    wanted: list[tuple[str, str]] = []
    for pattern, docs in READS:
        if re.search(pattern, low):
            wanted += docs
    if _needs_sop(req, low):
        wanted.append(("SOP-SUP-007", "1"))
    return list(dict.fromkeys(wanted))


def _last_site(req: MockRequest) -> str | None:
    for c in reversed(req.calls("get_site_brief")):
        if c.input.get("site_id"):
            return c.input["site_id"]
    return None


def _plan_tools(req: MockRequest, text: str) -> Reply | None:
    """Which tools a fresh technician message needs (None: answer without tools)."""
    low = text.lower()
    site = _site_of(text)
    if re.search(r"pull the site brief", low) and site:
        reply = use_tools(tool("get_site_brief", site_id=site), tool("get_site_log", site_id=site),
                          preface=f"Pulling the brief and the log export for {site} before you go in.")
        reply.progress = [f"Fetching the site brief for {site} first.", f"Now the log export for {site}."]
        return reply
    asset = ASSET_RE.search(text)
    calls: list[dict] = []
    if re.search(r"vibration trend", low) and asset:
        calls = [tool("query_telemetry", asset_id=asset.group(0), metric="vibration_mm_s", agg="summary"),
                 tool("query_telemetry", asset_id=asset.group(0), metric="vibration_mm_s", agg="daily")]
    elif re.search(r"bearing temperature trend", low) and asset:
        calls = [tool("query_telemetry", asset_id=asset.group(0), metric="bearing_temp_c", agg="summary")]
    elif re.search(r"motor current and flow", low) and asset:
        calls = [tool("query_telemetry", asset_id=asset.group(0), metric="motor_current_a", agg="summary"),
                 tool("query_telemetry", asset_id=asset.group(0), metric="flow_m3h", agg="summary")]
    elif re.search(r"vibration summary", low) and asset:
        calls = [tool("query_telemetry", asset_id=asset.group(0), metric="vibration_mm_s", agg="summary")]
    if calls:
        reply = use_tools(*calls, preface="Pulling the telemetry aggregates.")
        reply.progress = [f"Asking the historian for {c['input']['metric']} ({c['input']['agg']}) on "
                          f"{c['input']['asset_id']}." for c in calls]
        return reply
    if low.startswith("log it:"):
        for chunk in re.split(r";\s*and\s+", text[len("Log it:"):].strip()):
            chunk = chunk.strip().rstrip(".")
            unit = chunk.split(",", 1)[0].strip()
            rest = chunk.split(",", 1)[1].strip() if "," in chunk else ""
            parts_m = re.search(r",?\s*parts used\s+(.+)$", rest)
            parts = [p.strip() for p in parts_m.group(1).split(",")] if parts_m else []
            rest = rest[: parts_m.start()] if parts_m else rest
            rest = re.sub(r",?\s*no parts$", "", rest)
            action_m = re.search(r",?\s*action:\s*(.+)$", rest)
            action = action_m.group(1).strip() if action_m else "completed"
            finding = (rest[: action_m.start()] if action_m else rest).strip().rstrip(",")
            wo = re.search(r"WO-\d+", chunk)
            args = {"site_id": _site_of(text) or _last_site(req) or "?", "unit_id": unit, "finding": finding,
                    "action": action, "parts": parts}
            if wo:
                args["work_order"] = wo.group(0)
            calls.append(tool("log_finding", **args))
        reply = use_tools(*calls, preface="Recording that in the findings log.")
        reply.progress = [f"Logging the finding for {c['input']['unit_id']}." for c in calls]
        return reply
    needed = [(d, s) for d, s in _wanted_sections(req, low) if _section_in_context(req, d, s) is None]
    if needed:
        reply = use_tools(*[tool("read_manual_section", doc=d, section=s) for d, s in needed],
                          preface="Let me read the relevant section" + ("s." if len(needed) > 1 else "."))
        reply.progress = [f"Reading {d} §{s}." for d, s in needed]
        return reply
    return None


def _answer_lookup(req: MockRequest, text: str) -> str:
    """Answer a technical question from the sections in context (just read, or read earlier today)."""
    low = text.lower()
    answers = []
    for doc, section in _wanted_sections(req, low):
        body = _section_in_context(req, doc, section)
        if body:
            answers.append(_section_answer(text, doc, section, body))
    if _needs_sop(req, low):
        sop = next((a for a in answers if "SOP-SUP-007" in a), "")
        rule = next((a for a in answers if "[UM-KC1 §5]" in a), "")
        return ("F07 is a ground fault - a SAFETY fault - and on a Zone 1 ATEX unit it is a P1 escalation, not a "
                f"repair: isolate and lock out the unit, do not reset it again, and escalate to the on-call FSE now. "
                f"{rule} {sop}".strip())
    if re.search(r"fan hour counter", low) and answers:
        return "Yes, replace it. " + answers[0]
    return " ".join(answers) if answers else ("I have not read the section that answers this; ask me to look it "
                                              "up and I will read the manual.")


@scenario("adv.day3.field_agent", match=lambda r: FIELD in r.system_text, priority=10)
def field_agent(req: MockRequest) -> Reply:
    text = _current_user_text(req)
    low = text.lower()

    # ---- a tool round just returned: compose the answer from the results
    if req.is_tool_result_turn:
        results = _results(req)
        names = [n for n, _, _ in results]
        if "get_site_brief" in names:
            brief_raw = next(r for n, _, r in results if n == "get_site_brief")
            log = next((r for n, _, r in results if n == "get_site_log"), "")
            try:
                brief = json.loads(brief_raw)
            except ValueError:
                return _reply(req, f"The site brief could not be read: {brief_raw[:120]}", 0.3)
            escalate = bool(re.search(r"atex|hazardous", _reminders(req)))
            return _reply(req, _arrival_answer(brief, log, escalate), 0.5,
                          thinking="Read the brief and the log notes, then summarise what matters today.")
        if "query_telemetry" in names:
            parts = []
            for n, _, r in results:
                if n == "query_telemetry":
                    try:
                        parts.append(_telemetry_answer(json.loads(r)))
                    except ValueError:
                        parts.append(r[:160])
            verdict = ""
            if re.search(r"coupling", low) and any("step" in p for p in parts):
                verdict = (" A step change right after the coupling job with a flat bearing temperature is the "
                           "misalignment signature (CM-GUIDE-01 §3): laser-align before it wears the coupling.")
            if re.search(r"boiler feed|demand", low):
                verdict = (" Current and flow rose together: the pump is running right of its best-efficiency "
                           "point - an overload signature, not a bearing problem.")
            return _reply(req, " ".join(parts) + verdict, 0.8, thinking="Compare first and last week; look for a step.")
        if "log_finding" in names:
            lines = []
            for n, args, r in results:
                if n != "log_finding":
                    continue
                try:
                    rec = json.loads(r).get("recorded", {})
                except ValueError:
                    rec = args
                lines.append(f"Recorded {rec.get('finding_id', '')} for {rec.get('unit_id')}: {rec.get('finding')}; "
                             f"action: {rec.get('action')}; parts: {', '.join(rec.get('parts') or []) or 'none'}.")
            return _reply(req, " ".join(lines), 0.3)
        if "read_manual_section" in names:
            errors = [r for n, _, r in results if n == "read_manual_section" and r.startswith("Error")]
            if errors:
                return _reply(req, f"The manual lookup failed: {errors[0][:160]}", 0.3)
            return _reply(req, _answer_lookup(req, text), 0.5, thinking="Find the row that answers the question.")
        return _reply(req, "Done.", 0.2)

    # ---- harness requests (a fork of the conversation that reuses its cached prefix)
    if text.startswith("<summarise_request>"):
        return say(_summary(req, text), complexity=0.6, thinking="Keep what the instructions ask for; drop raw data.")
    if text.startswith("<scratchpad_request"):
        site = re.search(r'site="(\w+)"', text)
        return json_reply(_scratchpad(req, site.group(1) if site else (_last_site(req) or "?")), complexity=0.4)

    # ---- a fresh technician message
    if text.startswith(PROBE_PREFIX):
        return _reply(req, _answer_from_context(req, text), 0.4, thinking="Search what I still have in context.")
    if re.search(r"day's report|end of the day", low):
        return _reply(req, _report(req), 0.7, thinking="Collect every finding still in context, site by site.")
    if re.search(r"asked you to remember", low):
        return _reply(req, _recall(req), 0.4)
    if "remember for next time:" in low:
        note = re.search(r"Remember for next time:\s*(.+?)(?:\s+(?:Next|Last) stop:\s*(.+?))?\.?$", text, re.S)
        site = _site_of(re.split(r"(?:Next|Last) stop:", text)[0]) or _last_site(req)
        nxt = NEXT_STOP.get(site or "")
        ack = f"Noted for next time at {site}: {note.group(1).strip() if note else text}"
        if nxt:
            ack += f" Heading to {nxt}."
        return _reply(req, ack, 0.2)
    plan = _plan_tools(req, text)
    if plan is not None:
        return plan
    answer = _answer_lookup(req, text)
    if "I have not read" not in answer:
        return _reply(req, answer, 0.4, thinking="Answer from the section already in context.")
    return _reply(req, _answer_from_context(req, text), 0.4)


# ============================================================================================ subagent: site reader
SITE_READER = "<adv_day3_site_reader>"


def _log_text(req: MockRequest) -> tuple[str, str]:
    """(title, text) of the site log the subagent was given (a document block, else the user text)."""
    if req.documents:
        d = req.documents[0]
        return d.get("title") or "?", d["text"]
    return "?", req.first_user_text


@scenario("adv.day3.site_reader", match=lambda r: SITE_READER in r.system_text, priority=10)
def site_reader(req: MockRequest) -> Reply:
    title, text = _log_text(req)
    lines = text.splitlines()
    ask = req.last_user_text
    question = re.search(r"<question>(.+?)</question>", ask, re.S)
    if question:                                   # a drill-down: one answer with its line number
        q_terms = _terms(question.group(1))
        prefer_recent = bool(re.search(r"recent|latest|last\b", question.group(1), re.I))
        best = None
        for i, ln in enumerate(lines, 1):
            score = len(_matched(q_terms, ln)) + (1.5 if re.search(r"°C", ln) and "temperat" in " ".join(q_terms) else 0)
            if score >= 2 and (best is None or (score, i if prefer_recent else -i) > (best[0], best[1])):
                best = (score, i if prefer_recent else -i, i, ln.strip())
        if best is None:
            return say(f"No line in the {title} log answers that.", complexity=0.2)
        return say(f"Line {best[2]} of the {title} log: {best[3]}", complexity=0.3)
    head = text.split("READINGS:")[0].split("FAULTS:")[0]
    notes = [(i, ln.strip()) for i, ln in enumerate(lines, 1) if ln.startswith("  ") and ln.strip() and ln in head]
    site_m = re.search(r"\| ([^|]+?) \|", lines[0]) if lines else None
    counts: dict[str, int] = {}
    for code in re.findall(r"^\d{4}-\d\d-\d\d \d\d:\d\d\s+(F\d\d)", text, re.M):
        counts[code] = counts.get(code, 0) + 1
    summary = {
        "site": site_m.group(1).strip() if site_m else title,
        "open_items": [ln.split(":", 1)[1].strip() for _, ln in notes if ln.startswith("OPEN:")],
        "safety_flags": [ln.split(":", 1)[1].strip() for _, ln in notes if ln.startswith("SAFETY:")],
        "parts_on_site": [ln.split(":", 1)[1].strip() for _, ln in notes if ln.startswith("PARTS:")],
        "facts": [{"fact": ln, "source_line": i} for i, ln in notes if not re.match(r"(OPEN|SAFETY|PARTS):", ln)],
        "fault_counts": counts,
        "readings_scanned": len(re.findall(r"^\d{4}-\d\d-\d\dT\d\d:00:00Z ", text, re.M)),
    }
    if req.output_schema is not None:
        allowed = set((req.output_schema.get("properties") or {}).keys())
        summary = {k: v for k, v in summary.items() if k in allowed}
    return json_reply(summary, complexity=0.4)


# ============================================================================================ coordinator: planner
PLANNER = "<adv_day3_planner>"


def _site_views(req: MockRequest) -> list[dict]:
    """Per-site views the planner can plan from: subagent summaries (JSON in <site_summary> blocks) or raw logs."""
    views = []
    for m in req.messages:
        if m.get("role") != "user":
            continue
        for t in _text_blocks(m):
            mm = re.match(r"\s*<site_summary site=\"(\w+)\">\s*(\{.*\})\s*</site_summary>", t, re.S)
            if mm:
                try:
                    views.append({"site_id": mm.group(1), **json.loads(mm.group(2))})
                except ValueError:
                    pass
    for d in req.documents:
        head = d["text"].split("READINGS:")[0].split("FAULTS:")[0]
        notes = [ln.strip() for ln in head.splitlines() if ln.startswith("  ")]
        views.append({"site_id": d.get("title") or "?",
                      "open_items": [n.split(":", 1)[1].strip() for n in notes if n.startswith("OPEN:")],
                      "safety_flags": [n.split(":", 1)[1].strip() for n in notes if n.startswith("SAFETY:")],
                      "parts_on_site": [n.split(":", 1)[1].strip() for n in notes if n.startswith("PARTS:")]})
    return views


@scenario("adv.day3.planner", match=lambda r: PLANNER in r.system_text, priority=10)
def planner(req: MockRequest) -> Reply:
    text = _current_user_text(req)
    if req.is_tool_result_turn:
        answers = [r for n, _, r in _results(req) if n == "ask_site_reader"]
        return say("From the source log: " + " ".join(answers), complexity=0.3)
    if re.search(r"plan (?:my|the) day", text, re.I):
        lines = []
        for v in _site_views(req):
            lines.append(f"- {v['site_id']}: open: {'; '.join(v.get('open_items') or []) or 'none'}; safety: "
                         f"{'; '.join(v.get('safety_flags') or []) or 'none'}; on site: "
                         f"{'; '.join(v.get('parts_on_site') or []) or 'nothing listed'}")
        return say("Plan for today, in visiting order:\n" + "\n".join(lines), complexity=0.6,
                   thinking="One line per site: what is open, what is dangerous, what is already there.")
    hit = _best_line(req, text)
    if hit is None and req.has_tool("ask_site_reader") and not req.called("ask_site_reader"):
        site = _site_of(text) or "?"
        return use_tools(tool("ask_site_reader", site_id=site, question=text),
                         preface="That detail is not in the summaries; asking a reader to look it up in the source.")
    return say(f"From my context ({hit[0]}): {hit[1]}" if hit else NOT_KNOWN, complexity=0.3)


# ============================================================================================ memory agent
MEMORY_AGENT = "<adv_day3_memory_agent>"
INJECTION = re.compile(r"ignore (?:your|all|previous|the) (?:\w+ )?(?:instructions|rules)|you (?:must|should) now|"
                       r"(?:send|email|forward) (?:the |all |every )?[\w\s-]{0,30}\bto\b\s+[\w.+-]+@", re.I)


def _memory_hits(req: MockRequest) -> tuple[list[dict], int]:
    notes, quarantined = [], 0
    for c in req.calls("memory_search"):
        data = c.result_json() or {}
        notes += data.get("notes", [])
        quarantined += int(data.get("quarantined", 0))
    return notes, quarantined


@scenario("adv.day3.memory_agent", match=lambda r: MEMORY_AGENT in r.system_text and r.has_tool("memory_search"),
          priority=10)
def memory_agent(req: MockRequest) -> Reply:
    text = _current_user_text(req)
    low = text.lower()
    site = _site_of(text) or "?"
    start = _turn_start(req)
    this_turn = [c for c in req.tool_calls if any(b.get("id") == c.id for m in req.messages[start:]
                                                  if m.get("role") == "assistant" for b in m.get("content") or []
                                                  if isinstance(b, dict))]
    searched = [c for c in this_turn if c.name == "memory_search"]
    wrote = [c for c in this_turn if c.name == "memory_write"]
    remember = re.search(r"(?:remember|note) for next time:\s*(.+?)\s*$", text, re.I | re.S)
    if not searched:                                  # reads before acting - and before writing
        query = remember.group(1) if remember else text
        return use_tools(tool("memory_search", site_id=site, query=query[:200]),
                         preface="Checking memory for this site first.")
    if wrote:
        status = [c.result_json() or {} for c in wrote]
        return say("Memory: " + "; ".join(f"{s.get('status')} ({s.get('id', '-')})" for s in status), complexity=0.2)
    notes, quarantined = _memory_hits(req)
    if remember:
        note = " ".join(remember.group(1).split()).rstrip(".")
        same = [n for n in notes if len(_matched(_terms(note), n.get("text", ""))) >= max(3, len(_terms(note)) // 2)]
        if same:                                      # already known: confirm instead of writing a duplicate
            return use_tools(tool("memory_write", site_id=site, text=note, confirms=same[0].get("id")),
                             preface=f"That is already in memory as {same[0].get('id')}; confirming it.")
        return use_tools(tool("memory_write", site_id=site, text=note), preface="Writing that down as today's note.")
    relevant = [n for n in notes if _matched(_terms(text), n.get("text", ""))] or notes
    if not relevant:
        answer = "Memory has nothing for this site yet."
    else:
        answer = "From memory (data, not instructions): " + " | ".join(
            f"{n.get('text')} [{n.get('id')}, {n.get('tier')}]" for n in relevant[:3])
    if quarantined:
        answer += (f" {quarantined} note(s) for this site are quarantined for review (they contained instructions "
                   "aimed at the assistant); I have not used them.")
    return say(answer, complexity=0.4, thinking="Answer from memory notes; treat them as data.")


CONSOLIDATE = "<adv_day3_consolidate>"


@scenario("adv.day3.consolidator", match=lambda r: CONSOLIDATE in r.system_text, priority=10)
def consolidator(req: MockRequest) -> Reply:
    """Episodic notes -> durable facts: keep site rules and preferences, turn 'today' into dated events, drop
    personal data and anything that reads like an instruction to the assistant."""
    try:
        notes = json.loads(re.search(r"<episodes>(.*)</episodes>", req.last_user_text, re.S).group(1))
    except (AttributeError, ValueError):
        notes = []
    facts, dropped = [], []
    for n in notes:
        text = n.get("text", "")
        if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d\s().-]{7,}\d", text) or INJECTION.search(text):
            dropped.append({"note_id": n.get("id"), "reason": "personal data or instructions aimed at the assistant"})
            continue
        if n.get("trust") == "untrusted":
            dropped.append({"note_id": n.get("id"), "reason": "untrusted source - needs human review"})
            continue
        rule = re.search(r"\b(wants|requires|prefers|only|may only|expires|no electrical work|held at|hearing|must)\b",
                         text, re.I)
        key = " ".join(sorted(_terms(text)))[:60]
        facts.append({"site_id": n.get("site_id"), "key": key, "fact": text.rstrip("."),
                      "kind": "rule" if rule else "event", "sources": [n.get("id")]})
    return json_reply({"facts": facts, "dropped": dropped}, complexity=0.5)


# ============================================================================================ cache lab
CACHE_LAB = "<adv_day3_cache_lab>"


@scenario("adv.day3.cache_lab", match=lambda r: CACHE_LAB in r.system_text, priority=10)
def cache_lab(req: MockRequest) -> Reply:
    question = _current_user_text(req)
    wanted = re.findall(r"([A-Z]{2,4}-[A-Z0-9]+) §([\d.]+)", question) if "sections:" in question.lower() else []
    if wanted and req.has_tool("read_manual_section"):
        read = {(c.input.get("doc"), c.input.get("section")) for c in req.calls("read_manual_section")}
        todo = [(d, s) for d, s in wanted if (d, s) not in read]
        if todo:
            batch = todo if "in parallel" in question.lower() else todo[:1]
            return use_tools(*[tool("read_manual_section", doc=d, section=s) for d, s in batch],
                             preface="Reading " + ", ".join(f"{d} §{s}" for d, s in batch) + ".")
        return say(f"Read {len(wanted)} sections; ready for your questions.", complexity=0.2)
    q_terms = _terms(question)
    best, best_score = "", 0
    for ln in req.system_text.splitlines():
        if not ln.strip() or ln.startswith("#") or ln.startswith("<"):
            continue
        score = len(_matched(q_terms, ln))
        if score > best_score:
            best, best_score = ln.strip(), score
    if best_score >= 2:
        cells = [c.strip().replace("**", "") for c in best.strip("|").split("|")] if best.startswith("|") else [best]
        return say("From the reference card: " + " - ".join(c for c in cells if c), complexity=0.2)
    return say("That is not on the reference card.", complexity=0.1)
