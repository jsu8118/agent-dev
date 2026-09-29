"""Mock policies for the advanced Day 3 - long-horizon context: the field-service agent and its helpers.

Every policy matches on a marker in the system prompt and derives everything it "says" from the request: the
tool results in context, the technician's own words, a conversation summary or scratchpad the harness put in a
user turn, or the memory notes a tool returned. Like a real model, the policies can only report what survived in
context - which is exactly what the labs measure (probe questions, end-of-day report, memory recall).

  adv.day3.field_agent   <adv_day3_field_agent>  labs 01-04, 06, ex08/ex09   the technician's day, 40 turns
  adv.day3.summariser    <adv_day3_summariser>   lab 02 (own summarisation)  keeps what the instructions ask for
  adv.day3.scratchpad    <adv_day3_scratchpad>   lab 02 (structured state)   one site's state as JSON
  adv.day3.site_reader   <adv_day3_site_reader>  lab 06 (subagents)          one site log -> summary contract
  adv.day3.memory_agent  <adv_day3_memory_agent> lab 05                      reads memory before acting, writes after
  adv.day3.consolidator  <adv_day3_consolidate>  lab 05                      episodic notes -> semantic facts
  adv.day3.cache_lab     <adv_day3_cache_lab>    lab 07, ex10                answers from the cached reference card

The mock does not pace, summarise or remember by itself; where a policy stands in for a behaviour the docs describe
(task-budget pacing, a summary that follows instructions), the lab prints a [mock] note saying so.
"""

from __future__ import annotations

import json
import re

from labkit.mock import MockRequest, Reply, json_reply, say, scenario, tool, use_tools
from labkit.mock.tokens import block_tokens, text_tokens

FIELD = "<adv_day3_field_agent>"
PROBE_PREFIX = "Quick check from today's session:"

SITE_NAMES = {"gbwd": ("granite bay", "gbwd"), "harbor": ("harbor foods", "harbor"), "riverbend": ("riverbend",),
              "cedar": ("cedar creek", "cedar"), "cobalt": ("cobalt",), "westfield": ("westfield",)}
NEXT_STOP = {"gbwd": "Harbor Foods", "harbor": "Riverbend Brewing", "riverbend": "Cedar Creek Dairy",
             "cedar": "Cobalt Chemical", "cobalt": "Westfield Hospital", "westfield": None}
ASSET_RE = re.compile(r"\b(?:[A-Z]{2}-KP\d{3}X?-\d{2}|KP\d{3}-\d{4}-\d{4}|KC1-[A-Z0-9-]+)\b")
_STOP = set("a an the and or of to in on for at is are be it its this that what which when how do does did i we "
            "you my me our your from with as by into if any all so than then there here about after before while "
            "please today session quick check todays what's whats was were will would can could should must".split())


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


def _current_user_text(req: MockRequest) -> str:
    """The technician's current message (the last user message that carries text, not tool results)."""
    texts = req.texts("user")
    return texts[-1] if texts else ""


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
    ('grease' in 'regrease', 'temperat' in 'overtemperature')."""
    line_terms = _terms(line)
    out = set()
    for q in q_terms:
        if q in line_terms or (len(q) >= 5 and any((q in t or t in q) and len(t) >= 5 for t in line_terms)):
            out.add(q)
    return out


def _reminders(req: MockRequest) -> str:
    """Operator instructions in force for this turn: persistent system messages plus the newest turn-scoped one."""
    return "\n".join(req.system_messages).lower()


def _budget(req: MockRequest) -> tuple[float, int, int] | None:
    """(remaining fraction, spent, total) for a task budget - what the model reads off the countdown the API
    injects: what it generated so far plus the tool results it read, not the resent history."""
    total = req.task_budget
    if not total:
        return None
    budget = req.output_config.get("task_budget") or {}
    if isinstance(budget.get("remaining"), int):          # the client re-based the countdown (after compaction)
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
        frac, spent, total = budget
        if frac <= 0.0:
            text = text.split(". ")[0].rstrip(".") + ". (Task budget exhausted: keeping this to the essentials.)"
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
        par = _row(body, r"Parallel")
        ang = _row(body, r"Angular")
        recheck = _line(body, r"Re-check alignment after the first \d+ operating hours, then annually")
        return (f"Alignment tolerances for the KP-250: parallel (radial) offset {par[1] if par else '?'}; angular "
                f"{ang[1] if ang else '?'}. {recheck}; Kestrel offers laser alignment (SVC-ALIGN). {ref}")
    if doc == "IOM-KP250" and section == "9":
        rows = [_row(body, p) for p in (r"Impeller nut", r"Casing cover", r"Baseplate", r"Coupling hub")]
        return ("Tightening torques for the KP-250: " + "; ".join(f"{r[0]}: {r[1]}" for r in rows if r) + f". {ref}")
    if doc == "IOM-KP250" and section == "6":
        grease = _row(body, r"Regrease both bearings")
        replace = _row(body, r"Replace bearings")
        dry = _row(body, r"dry-run")
        parts = []
        if grease and re.search(r"grease|interval|hours", q):
            parts.append(f"Regrease {grease[0].lower()}: {grease[1]}")
        if replace and re.search(r"bearing|seal kit|replace", q):
            parts.append(f"{replace[0]}: {replace[1]}")
        if dry and re.search(r"dry", q):
            parts.append(f"{dry[0]}: {dry[1]}")
        if not parts and grease:
            parts.append(f"{grease[0]}: {grease[1]}")
        return "KP-250 maintenance schedule - " + "; ".join(parts) + f". {ref}"
    if doc == "IOM-KP250" and section == "7.5":
        over = _row(body, r"Over-greasing")
        return (f"Bearing running hot right after maintenance: {over[0].lower()} - {over[1]}. {ref}" if over
                else f"Bearing overheating causes listed. {ref}")
    if doc == "IOM-KP250" and section == "7.4":
        dry = _row(body, r"Dry running")
        return (f"Seal leakage after dry running: {dry[1]}. Any steady drip after the first 4 hours of run-in "
                f"indicates a problem. {ref}" if dry else f"Seal leakage causes listed. {ref}")
    if doc == "IOM-KP250" and section == "7.6":
        rows = [_row(body, p) for p in (r"beyond the end of the curve", r"density or viscosity", r"Impeller rubbing")]
        return ("Motor overload: " + "; ".join(f"{r[0]} -> {r[1]}" for r in rows if r) + f". {ref}")
    if doc == "IOM-KP400" and section == "6":
        cav = _row(body, r"Cavitation noise")
        return (f"KP-400 at partial load: {cav[1]} -> {cav[2]}. {ref}" if cav else f"Split-case troubleshooting. {ref}")
    if doc == "IOM-KP400" and section == "4":
        vib = _row(body, r"vibration")
        return (f"KP-400 vibration limits on a flexible foundation: normal {vib[1]}, alert {vib[2]}, alarm/shutdown "
                f"{vib[3]}. {ref}" if vib else f"Operating limits. {ref}")
    if doc == "IOM-KP400" and section == "5":
        grease = _row(body, r"Regrease each bearing")
        return (f"KP-400 bearings: {grease[0].lower()}, {grease[1].lower()}. {ref}" if grease
                else f"KP-400 maintenance schedule. {ref}")
    if doc == "UM-KC1" and section == "4":
        code = next((c for c in re.findall(r"\bF\d\d\b", question) if _row(body, rf"\|\s*{c}\s*\|")), None)
        if code:
            r = _row(body, rf"\|\s*{code}\s*\|")
            safety = " (SAFETY fault)" if "SAFETY" in r[1] else ""
            name = r[1].replace("- SAFETY", "").replace("— SAFETY", "").strip()
            return f"{code} = {name}{safety}. Probable causes: {r[2]}. Remedy: {r[3]}. {ref}"
        safety = [r[0] for r in (_row(body, p) for p in (r"\|\s*F07", r"\|\s*F17")) if r]
        return f"Safety faults in the fault table: {', '.join(safety)}. {ref}"
    if doc == "UM-KC1" and section == "7":
        pct = _line(body, r"\d+% of F05 events were resolved by cleaning the heatsink and replacing a worn KC-1-FAN")
        proactive = _line(body, r"Fans older than [\d,]+ hours should be replaced proactively")
        return f"Application note AN-KC1-05: {pct}; {proactive}. {ref}"
    if doc == "UM-KC1" and section == "5":
        rule = _line(body, r"SAFETY faults \(F07, F17\): only one reset is allowed before inspection[^.]*\.")
        return f"Reset rule: {rule} Otherwise remove the cause and press STOP/RESET for 2 seconds. {ref}"
    if doc == "UM-KC1" and section == "3":
        rows = [_row(body, rf"\|\s*{p}\s*\|") for p in ("P01", "P02", "P03", "P04", "P05", "P10", "P20")]
        return ("KC-1 key parameters (defaults): " + "; ".join(f"{r[0]} {r[1]} = {r[2]}" for r in rows if r)
                + f". Set P01 from the motor nameplate first. {ref}")
    if doc == "SEAL-FA-02" and section == "2":
        r = _row(body, r"heat cracks")
        return (f"Radial heat cracks with hardened elastomers = {r[1].lower()}; warranty: {r[2]}. {ref}" if r
                else f"Seal evidence table. {ref}")
    if doc == "SOP-SUP-007" and section == "1":
        atex = _line(body, r"A fault on explosion-proof \(ATEX\) equipment[^.]*\.")
        return f"P1 escalation applies: {atex} Target: on-call FSE, 1-hour response, 24/7. {ref}"
    # generic: the two lines that share most terms with the question
    q_terms = _terms(question)
    scored = sorted(((len(q_terms & _terms(ln)), i, ln.strip()) for i, ln in enumerate(body.splitlines())
                     if ln.strip() and not ln.startswith("#")), key=lambda x: (-x[0], x[1]))
    picked = [ln for score, _, ln in scored[:2] if score >= 2]
    return (" ".join(picked) if picked else body.splitlines()[0]) + f" {ref}"


def _telemetry_answer(result: dict) -> str:
    asset, metric, unit = result.get("asset_id"), result.get("metric", ""), result.get("unit", "")
    zeros = result.get("zero_readings_while_running")
    if zeros:
        return (f"{asset} {metric.replace('_', ' ')}: {zeros['count']} readings of exactly 0.0 while running "
                f"({zeros['first'][:16]} to {zeros['last'][:16]}) - a sensor or cable fault, not a machine fault "
                f"(CM-GUIDE-01 §4). The reading is not valid; check the VS-10 and its cable, and watch the process "
                f"data meanwhile.")
    if "daily_median" in result:
        series = list(result["daily_median"].items())
        jumps = [(series[i][0], round(series[i][1] - series[i - 1][1], 2)) for i in range(1, len(series))]
        day, jump = max(jumps, key=lambda x: x[1]) if jumps else ("?", 0.0)
        return (f"{asset} daily {metric.replace('_', ' ')} medians: {series[0][1]} {unit} on {series[0][0]} -> "
                f"{series[-1][1]} {unit} on {series[-1][0]}; largest day-over-day step +{jump} {unit} on {day}.")
    if "median" not in result:
        return f"{asset}: {result.get('note', 'no data')}."
    first, last, change = result.get("first_7d_median"), result.get("last_7d_median"), result.get("change_pct")
    return (f"{asset} {metric.replace('_', ' ')} (30 d): median {result['median']} {unit}, p90 {result['p90']} "
            f"{unit}; first week {first} -> last week {last} {unit} ({change:+.0f}%); last 24 h median "
            f"{result.get('last_24h_median', last)} {unit}.")


def _arrival_answer(brief: dict, log: str, escalate_hint: bool) -> str:
    units = ", ".join(f"{u['unit_id']} ({u['model']}, {u['service']})" for u in brief.get("units", []))
    open_items = brief.get("work_orders_last_12_months") or brief.get("open_tickets") or []
    notes = re.findall(r"^\s+(.+)$", log.split("READINGS:")[0].split("FAULTS:")[0].split("NOTES:")[-1], re.M)
    faults = re.findall(r"^\d{4}-\d\d-\d\d \d\d:\d\d\s+(F\d\d)", log, re.M)
    counts = {}
    for f in faults:
        counts[f] = counts.get(f, 0) + 1
    fault_line = ""
    if counts:
        top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:3]
        latest = {}
        for m in re.finditer(r"^(\d{4}-\d\d-\d\d \d\d:\d\d)\s+(F\d\d)", log, re.M):
            latest[m.group(2)] = m.group(1)
        fault_line = " Controller log: " + ", ".join(f"{c} x{n} (latest {latest[c]})" for c, n in top) + "."
    hazard = brief.get("hazardous_area", "")
    text = (f"At {brief['plant']} ({brief['customer']}). Units: {units}. Recent work orders / tickets: "
            f"{'; '.join(open_items[-3:]) or 'none'}. Shift-log notes: {'; '.join(notes[:5]) or 'none'}.{fault_line}")
    if hazard:
        text += f" Hazardous-area rules: {hazard}"
    if brief.get("criticality"):
        text += f" Criticality: {brief['criticality']}."
    if escalate_hint:
        text += " Reminder in force: safety faults here are escalated, not repaired."
    return text


# ============================================================================================ context search
def _context_lines(req: MockRequest, *, skip_last_user: bool = True) -> list[tuple[str, str, int]]:
    """(source, line, position) for everything in context that a model could quote: tool results, its own earlier
    replies, the technician's words, summaries and scratchpads the harness put in user turns, memory notes."""
    out: list[tuple[str, str, int]] = []
    messages = req.messages
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    pos = 0
    for i, m in enumerate(messages):
        if skip_last_user and i == last_user:
            continue
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                raw = b.get("content")
                text = raw if isinstance(raw, str) else "\n".join(x.get("text", "") for x in raw or [] if isinstance(x, dict))
                if "cleared by context management" in text[:80]:
                    continue
                source = "tool result"
                if text.startswith("{"):
                    try:
                        data = json.loads(text)
                        text = "\n".join(f"{k}: {v}" for k, v in data.items())
                    except ValueError:
                        pass
                for ln in text.splitlines():
                    pos += 1
                    out.append((source, ln.strip(), pos))
            elif b.get("type") == "text" and m.get("role") in ("user", "assistant"):
                text = b.get("text", "")
                if m.get("role") == "user":
                    source = ("conversation summary" if "<conversation_summary>" in text else
                              "scratchpad" if "<scratchpad>" in text else "memory" if "<memory" in text
                              else "the technician's words")
                else:
                    source = "my own earlier reply"
                for ln in re.split(r"\n|(?<=[.;])\s+(?=[A-Z0-9(])", text):
                    pos += 1
                    out.append((source, ln.strip(), pos))
    return out


UNIT_HINTS = [("torque", r"N·m"), ("temperature", r"°C"), ("grease", r"\b\d+ g\b"), ("flow", r"m³/h"),
              ("current", r"\bA\b"), ("offset", r"\bmm\b"), ("tolerance", r"\bmm\b"), ("window", r"\d\d:\d\d"),
              ("interval", r"hours"), ("code", r"\bF\d\d\b")]


def _answer_from_context(req: MockRequest, question: str) -> str:
    """Quote the line in context that best matches the question; admit it when nothing does."""
    body = question.replace(PROBE_PREFIX, "").strip()
    q_terms = _terms(body)
    products = {re.sub(r"[^a-z0-9]", "", p.lower()) for p in re.findall(r"\bK[PC]-?\d{1,3}s?\b", body, re.I)}
    prefer_recent = bool(re.search(r"recent|latest|last\b", body, re.I))
    best: tuple[float, int, str, str] | None = None
    for source, line, pos in _context_lines(req):
        if len(line) < 8 or line.startswith(PROBE_PREFIX) or line.startswith("#"):
            continue
        matched = q_terms & _terms(line)
        score = float(len(matched))
        for word, unit in UNIT_HINTS:
            if word in q_terms and re.search(unit, line):
                score += 1.5
        if products:
            norm = re.sub(r"[^a-z0-9]", "", line.lower())
            if not any(p.rstrip("s") in norm for p in products):
                score -= 0.5
        if not re.search(r"\d", line):
            score -= 1.0                                   # facts worth checking carry a number or a code
        if score < 2.5:
            continue
        key = (score, pos if prefer_recent else -pos)
        if best is None or key > (best[0], best[1]):
            best = (score, key[1], source, line)
    if best is None:
        return ("I don't have that in my context any more - it was in a part of the day that has been compacted, "
                "cleared or trimmed. I can re-read the source if you want.")
    return f"From today's session ({best[2]}): {best[3]}"


# ============================================================================================ findings / recall
def _findings_in_context(req: MockRequest) -> list[dict]:
    found: list[dict] = []
    for c in req.calls("log_finding"):
        if c.result and not c.is_error:
            data = c.result_json() or {}
            rec = data.get("recorded") or dict(c.input)
            found.append({"site": rec.get("site_id", "?"), "unit": rec.get("unit_id", "?"),
                          "finding": rec.get("finding", ""), "action": rec.get("action", ""),
                          "parts": rec.get("parts") or [], "id": rec.get("finding_id", "")})
    for source, line, _ in _context_lines(req):
        if source in ("conversation summary", "scratchpad") and re.match(r"(FND-\d+|finding):", line, re.I):
            m = re.match(r"(?:FND-\d+|finding):\s*(\w+)\s*/\s*(\S+)\s*-\s*(.+?)(?:;\s*action:\s*(.+?))?(?:;\s*parts:\s*(.+))?$", line, re.I)
            if m and not any(f["unit"] == m.group(2) and f["finding"] == m.group(3) for f in found):
                found.append({"site": m.group(1), "unit": m.group(2), "finding": m.group(3),
                              "action": m.group(4) or "", "parts": [p.strip() for p in (m.group(5) or "").split(",") if p.strip()],
                              "id": ""})
    return found


def _report(req: MockRequest) -> str:
    findings = _findings_in_context(req)
    visited = []
    for c in req.calls("get_site_brief"):
        if c.input.get("site_id") not in visited:
            visited.append(c.input.get("site_id"))
    for source, line, _ in _context_lines(req):
        if source in ("conversation summary", "scratchpad"):
            for m in re.finditer(r"\b(gbwd|harbor|riverbend|cedar|cobalt|westfield)\b", line.lower()):
                if m.group(1) not in visited:
                    visited.append(m.group(1))
    lines = [f"End-of-day report, {len(visited)} site(s) in context: {', '.join(visited) or 'none'}."]
    parts_to_order: list[str] = []
    for site in visited:
        mine = [f for f in findings if f["site"] == site]
        if not mine:
            lines.append(f"- {site}: no findings in my context (the visit was compacted, cleared or trimmed away).")
            continue
        done = "; ".join(f"{f['unit']}: {f['finding']}" for f in mine)
        open_items = [f"{f['unit']}: {f['action']}" for f in mine
                      if re.search(r"book|schedul|escalat|recommend|review|to be", f["action"], re.I)]
        lines.append(f"- {site}: {done}. Open: {'; '.join(open_items) or 'none'}.")
        for f in mine:
            parts_to_order.extend(p for p in f["parts"] if p not in parts_to_order)
    lines.append(f"Parts used today (restock): {', '.join(parts_to_order) or 'none in context'}.")
    return "\n".join(lines)


def _recall(req: MockRequest) -> str:
    notes: list[str] = []
    for source, line, _ in _context_lines(req):
        m = re.search(r"Remember for next time:\s*(.+?)(?:\s+(?:Next|Last) stop:.*)?$", line)
        if m and m.group(1) not in notes:
            notes.append(m.group(1).rstrip("."))
        elif source in ("conversation summary", "scratchpad", "memory") and re.match(r"remember:", line, re.I):
            note = line.split(":", 1)[1].strip().rstrip(".")
            if note not in notes:
                notes.append(note)
    if not notes:
        return "I have no 'remember for next time' notes left in my context - they were in turns that are gone."
    by_site: dict[str, list[str]] = {}
    for n in notes:
        by_site.setdefault(_site_of(n) or "other", []).append(n)
    return "Noted for next time:\n" + "\n".join(f"- {site}: {'; '.join(items)}" for site, items in by_site.items())


# ============================================================================================ the field agent
def _plan_tools(req: MockRequest, text: str) -> Reply | None:
    """Which tools a fresh technician message needs (None: answer without tools)."""
    low = text.lower()
    site = _site_of(text)
    updates = req.thinking_display == "updates"
    if re.search(r"pull the site brief", low) and site:
        reply = use_tools(tool("get_site_brief", site_id=site), tool("get_site_log", site_id=site),
                          preface=f"Pulling the brief and the log export for {site} before you go in.")
        if updates:      # progress updates (thinking.display "updates"): one short note before each tool call
            reply.progress = [f"Fetching the site brief for {site} first.", f"Now the log export for {site}."]
        return reply
    asset = ASSET_RE.search(text)
    if re.search(r"vibration trend", low) and asset:
        return use_tools(tool("query_telemetry", asset_id=asset.group(0), metric="vibration_mm_s", agg="summary"),
                         tool("query_telemetry", asset_id=asset.group(0), metric="vibration_mm_s", agg="daily"),
                         preface="Pulling the 30-day summary and the daily series.")
    if re.search(r"bearing temperature trend", low) and asset:
        return use_tools(tool("query_telemetry", asset_id=asset.group(0), metric="bearing_temp_c", agg="summary"))
    if re.search(r"motor current and flow", low) and asset:
        return use_tools(tool("query_telemetry", asset_id=asset.group(0), metric="motor_current_a", agg="summary"),
                         tool("query_telemetry", asset_id=asset.group(0), metric="flow_m3h", agg="summary"))
    if re.search(r"vibration summary", low) and asset:
        return use_tools(tool("query_telemetry", asset_id=asset.group(0), metric="vibration_mm_s", agg="summary"))
    if low.startswith("log it:"):
        calls = []
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
        return use_tools(*calls, preface="Recording that in the findings log.")
    reads: list[tuple[str, str]] = []
    if re.search(r"alignment tolerance", low):
        reads.append(("IOM-KP250", "3"))
    if re.search(r"torque values", low):
        reads.append(("IOM-KP250", "9"))
    if re.search(r"which bearings and seal kit", low):
        reads.append(("IOM-KP250", "6"))
    if re.search(r"how much grease goes into each bearing", low):
        reads += [("IOM-KP250", "6"), ("IOM-KP250", "7.5")]
    if re.search(r"regreasing the kp-250", low):
        reads.append(("IOM-KP250", "6"))
    if re.search(r"flow must a kp-400 stay above", low):
        reads += [("IOM-KP400", "6"), ("IOM-KP400", "4")]
    if re.search(r"kp-400s?: how much grease|while i'm at the kp-400s", low):
        reads.append(("IOM-KP400", "5"))
    if re.search(r"\bf05\b", low) and "application note" in low:
        reads += [("UM-KC1", "4"), ("UM-KC1", "7")]
    if re.search(r"fan hour counter", low):
        reads.append(("UM-KC1", "7"))
    if re.search(r"\bf10\b", low):
        reads.append(("UM-KC1", "4"))
    if re.search(r"\bf07\b", low):
        reads += [("UM-KC1", "4"), ("UM-KC1", "5")]
        if re.search(r"atex|zone 1", low) or re.search(r"atex|hazardous", _reminders(req)):
            reads.append(("SOP-SUP-007", "1"))
    if re.search(r"heat cracks", low):
        reads.append(("SEAL-FA-02", "2"))
    if re.search(r"seal leakage from dry running", low):
        reads.append(("IOM-KP250", "7.4"))
    if re.search(r"beyond the end of the curve|motor overload", low):
        reads.append(("IOM-KP250", "7.6"))
    if re.search(r"key parameters", low):
        reads.append(("UM-KC1", "3"))
    if re.search(r"safety faults", low):
        reads.append(("UM-KC1", "5"))
    needed = [(d, s) for d, s in dict.fromkeys(reads) if _section_in_context(req, d, s) is None]
    if needed:
        return use_tools(*[tool("read_manual_section", doc=d, section=s) for d, s in needed],
                         preface="Let me read the relevant section" + ("s." if len(needed) > 1 else "."))
    return None


def _last_site(req: MockRequest) -> str | None:
    for c in reversed(req.calls("get_site_brief")):
        if c.input.get("site_id"):
            return c.input["site_id"]
    return None


def _answer_lookup(req: MockRequest, text: str) -> str:
    """Answer a technical question from the sections in context (just read, or read earlier today)."""
    low = text.lower()
    wanted: list[tuple[str, str]] = []
    for pattern, docs in ((r"alignment tolerance", [("IOM-KP250", "3")]), (r"torque values", [("IOM-KP250", "9")]),
                          (r"which bearings and seal kit", [("IOM-KP250", "6")]),
                          (r"how much grease goes into each bearing", [("IOM-KP250", "6"), ("IOM-KP250", "7.5")]),
                          (r"regreasing the kp-250", [("IOM-KP250", "6")]),
                          (r"flow must a kp-400 stay above", [("IOM-KP400", "6"), ("IOM-KP400", "4")]),
                          (r"kp-400s?: how much grease|while i'm at the kp-400s", [("IOM-KP400", "5")]),
                          (r"\bf05\b.*application note", [("UM-KC1", "4"), ("UM-KC1", "7")]),
                          (r"fan hour counter", [("UM-KC1", "7")]), (r"\bf10\b", [("UM-KC1", "4")]),
                          (r"\bf07\b", [("UM-KC1", "4"), ("UM-KC1", "5"), ("SOP-SUP-007", "1")]),
                          (r"heat cracks", [("SEAL-FA-02", "2")]), (r"seal leakage from dry running", [("IOM-KP250", "7.4")]),
                          (r"beyond the end of the curve|motor overload", [("IOM-KP250", "7.6")]),
                          (r"key parameters", [("UM-KC1", "3")]), (r"safety faults", [("UM-KC1", "5")])):
        if re.search(pattern, low):
            wanted += docs
    answers = []
    for doc, section in dict.fromkeys(wanted):
        body = _section_in_context(req, doc, section)
        if body:
            answers.append(_section_answer(text, doc, section, body))
    if re.search(r"\bf07\b", low) and (re.search(r"atex|zone 1", low) or re.search(r"atex|hazardous|no repair", _reminders(req))):
        sop = next((a for a in answers if "SOP-SUP-007" in a), "")
        rule = next((a for a in answers if "[UM-KC1 §5]" in a), "")
        return ("F07 is a ground fault - a SAFETY fault - and on a Zone 1 ATEX unit it is a P1 escalation, not a "
                f"repair: isolate and lock out the unit, do not reset it again, and escalate to the on-call FSE now. "
                f"{rule} {sop}".strip())
    if re.search(r"fan hour counter", low) and answers:
        return "Yes, replace it: " + answers[0]
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
                parts = ", ".join(rec.get("parts") or []) or "none"
                lines.append(f"Recorded {rec.get('finding_id', '')} for {rec.get('unit_id')}: {rec.get('finding')}; "
                             f"action: {rec.get('action')}; parts: {parts}.")
            return _reply(req, " ".join(lines), 0.3)
        if "read_manual_section" in names:
            errors = [r for n, _, r in results if n == "read_manual_section" and r.startswith("Error")]
            if errors:
                return _reply(req, f"The manual lookup failed: {errors[0][:160]}", 0.3)
            return _reply(req, _answer_lookup(req, text), 0.5, thinking="Find the row that answers the question.")
        return _reply(req, "Done.", 0.2)

    # ---- a fresh technician message
    if text.startswith(PROBE_PREFIX):
        return _reply(req, _answer_from_context(req, text), 0.4, thinking="Search what I still have in context.")
    if re.search(r"day's report|end of the day", low):
        return _reply(req, _report(req), 0.7, thinking="Collect every finding still in context, site by site.")
    if re.search(r"asked you to remember", low):
        return _reply(req, _recall(req), 0.4)
    if "remember for next time:" in low:
        note = re.search(r"Remember for next time:\s*(.+?)(?:\s+(?:Next|Last) stop:\s*(.+?))?\.?$", text, re.S)
        site = _site_of(text) or _last_site(req)
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


# ============================================================================================ summariser
SUMMARISER = "<adv_day3_summariser>"


def _facts_from_transcript(req: MockRequest) -> dict:
    """What a careful summary keeps: findings, remember-notes, cited facts, log notes, sites visited."""
    facts = {"sites": [], "findings": [], "remember": [], "facts": [], "log_notes": []}
    for c in req.calls("get_site_brief"):
        if c.input.get("site_id") and c.input["site_id"] not in facts["sites"]:
            facts["sites"].append(c.input["site_id"])
    for f in _findings_in_context(req):
        facts["findings"].append(f"{f['site']} / {f['unit']} - {f['finding']}; action: {f['action']}; parts: "
                                 f"{', '.join(f['parts']) or 'none'}")
    seen = set()
    for source, line, _ in _context_lines(req, skip_last_user=False):
        if source == "the technician's words":
            m = re.search(r"Remember for next time:\s*(.+?)(?:\s+(?:Next|Last) stop:.*)?$", line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                facts["remember"].append(m.group(1).rstrip("."))
        elif source in ("conversation summary", "scratchpad"):
            if line.lower().startswith("remember:") and line not in seen:
                seen.add(line)
                facts["remember"].append(line.split(":", 1)[1].strip())
            elif line.lower().startswith("fact:") and line not in seen:
                seen.add(line)
                facts["facts"].append(line.split(":", 1)[1].strip())
        elif source == "my own earlier reply" and re.search(r"\[[A-Z0-9-]+ §[\d.]+\]", line) and re.search(r"\d", line):
            if line not in seen:
                seen.add(line)
                facts["facts"].append(line)
        elif source == "tool result" and re.match(r"(OPEN|SAFETY|PARTS):", line) and line not in seen:
            seen.add(line)
            facts["log_notes"].append(line)
    return facts


@scenario("adv.day3.summariser", match=lambda r: SUMMARISER in r.system_text, priority=10)
def summariser(req: MockRequest) -> Reply:
    facts = _facts_from_transcript(req)
    lines = [f"Sites visited so far: {', '.join(facts['sites']) or 'none'} (current: {facts['sites'][-1] if facts['sites'] else '?'})."]
    lines += [f"FND: {f}" for f in facts["findings"]]
    lines += [f"remember: {r}" for r in facts["remember"]]
    lines += [f"fact: {f}" for f in facts["facts"]]
    lines += [f"log note: {n}" for n in facts["log_notes"]]
    return say("\n".join(lines), complexity=0.6, thinking="Keep every finding, note and cited fact; drop raw data.")


SCRATCHPAD = "<adv_day3_scratchpad>"


@scenario("adv.day3.scratchpad", match=lambda r: SCRATCHPAD in r.system_text, priority=10)
def scratchpad(req: MockRequest) -> Reply:
    facts = _facts_from_transcript(req)
    site = facts["sites"][-1] if facts["sites"] else "?"
    mine = [f for f in facts["findings"] if f.startswith(f"{site} /")]
    entry = {
        "site_id": site,
        "done": [f.split(" - ", 1)[1].split("; action:")[0] for f in mine],
        "open_items": [f.split("; action: ")[1].split("; parts:")[0] for f in mine
                       if re.search(r"book|schedul|escalat|recommend|review|to be", f, re.I)]
                      + [n for n in facts["log_notes"] if n.startswith("OPEN:")],
        "facts": [{"fact": f, "source": re.search(r"\[([A-Z0-9-]+ §[\d.]+)\]", f).group(1)
                   if re.search(r"\[([A-Z0-9-]+ §[\d.]+)\]", f) else "reply"} for f in facts["facts"]],
        "parts_to_order": sorted({p.strip() for f in mine for p in f.split("; parts: ")[1].split(",")
                                  if p.strip() and p.strip() != "none"}),
        "remember": facts["remember"][-1:],
    }
    return json_reply(entry, complexity=0.5)


# ============================================================================================ subagent: site reader
SITE_READER = "<adv_day3_site_reader>"


@scenario("adv.day3.site_reader", match=lambda r: SITE_READER in r.system_text, priority=10)
def site_reader(req: MockRequest) -> Reply:
    text = req.conversation_text
    if req.documents:
        text = "\n".join(d["text"] for d in req.documents)
    head = text.split("READINGS:")[0].split("FAULTS:")[0]
    notes = [(i + 1, ln.strip()) for i, ln in enumerate(text.splitlines()) if ln.startswith("  ") and ln.strip()
             and ln.strip() in head]
    site_m = re.search(r"\| ([^|]+?) \|", text.splitlines()[0]) if text else None
    faults = re.findall(r"^\d{4}-\d\d-\d\d \d\d:\d\d\s+(F\d\d)", text, re.M)
    counts: dict[str, int] = {}
    for f in faults:
        counts[f] = counts.get(f, 0) + 1
    readings = len(re.findall(r"^\d{4}-\d\d-\d\dT\d\d:00:00Z ", text, re.M))
    summary = {
        "site": site_m.group(1).strip() if site_m else "?",
        "open_items": [ln.split(":", 1)[1].strip() for _, ln in notes if ln.startswith("OPEN:")],
        "safety_flags": [ln.split(":", 1)[1].strip() for _, ln in notes if ln.startswith("SAFETY:")],
        "parts_needed": [ln.split(":", 1)[1].strip() for _, ln in notes if ln.startswith("PARTS:")],
        "facts": [{"fact": ln, "source_line": i} for i, ln in notes if not re.match(r"(OPEN|SAFETY|PARTS):", ln)],
        "fault_counts": counts,
        "readings_scanned": readings,
    }
    if req.output_schema is not None:
        allowed = set((req.output_schema.get("properties") or {}).keys())
        summary = {k: v for k, v in summary.items() if k in allowed}
    return json_reply(summary, complexity=0.4)


# ============================================================================================ memory agent
MEMORY_AGENT = "<adv_day3_memory_agent>"
INJECTION = re.compile(r"ignore (?:your|all|previous) instructions|send .* password|email .* to [\w.-]+@", re.I)


@scenario("adv.day3.memory_agent", match=lambda r: MEMORY_AGENT in r.system_text and r.has_tool("memory_search"),
          priority=10)
def memory_agent(req: MockRequest) -> Reply:
    text = _current_user_text(req)
    low = text.lower()
    site = _site_of(text) or _last_site(req)
    searched = [c for c in req.calls("memory_search") if c.result is not None]
    if not searched or (req.is_tool_result_turn is False and not any(c.input.get("query", "") in low for c in searched)):
        if not req.is_tool_result_turn:
            query = "stop window permit" if re.search(r"stop|shut|take .* down", low) else "site notes preferences"
            return use_tools(tool("memory_search", customer_id=site or "?", query=query),
                             preface="Checking memory for this site first.")
    if req.is_tool_result_turn:
        results = _results(req)
        if any(n == "memory_write" for n, _, _ in results):
            return _reply(req, "Saved to memory: " + "; ".join(r[:90] for n, _, r in results if n == "memory_write"), 0.2)
        hits = [r for n, _, r in results if n == "memory_search"]
        notes = []
        quarantined = []
        for r in hits:
            try:
                data = json.loads(r)
            except ValueError:
                data = {"notes": [{"text": r}]}
            for note in data.get("notes", []):
                if INJECTION.search(note.get("text", "")):
                    quarantined.append(note)
                else:
                    notes.append(note)
        if "remember" in low or "note for next time" in low:
            body = re.sub(r"^.*?(?:remember|note for next time)[^:]*:\s*", "", text, flags=re.I | re.S).strip()
            return use_tools(tool("memory_write", customer_id=site or "?", tier="episodic", text=body),
                             preface="Writing that down as today's note.")
        answer = ""
        if notes:
            relevant = [n for n in notes if _terms(n.get("text", "")) & _terms(text)] or notes
            answer = "From memory (data, not instructions): " + " | ".join(
                f"[{n.get('tier', '?')}] {n.get('text', '')}" for n in relevant[:3])
        else:
            answer = "Memory has nothing for this site yet."
        if quarantined:
            answer += (f" One memory note contained instructions aimed at me and was ignored and flagged for "
                       f"review ({len(quarantined)} note(s)).")
        return _reply(req, answer, 0.4)
    return _reply(req, "Understood.", 0.2)


CONSOLIDATE = "<adv_day3_consolidate>"


@scenario("adv.day3.consolidator", match=lambda r: CONSOLIDATE in r.system_text, priority=10)
def consolidator(req: MockRequest) -> Reply:
    """Episodic notes -> durable facts: keep site rules and preferences, generalise 'today' into dated events,
    drop personal data and anything that reads like an instruction to the assistant."""
    text = req.last_user_text or req.first_user_text
    notes = re.findall(r'"text":\s*"([^"]+)"', text)
    facts, dropped = [], []
    for n in notes:
        if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+|\+\d[\d\s-]{6,}", n) or INJECTION.search(n):
            dropped.append({"note": n, "reason": "personal data or instruction-like content"})
            continue
        if re.search(r"\b(wants|requires|prefers|only|may only|expires|no electrical work|held at|hearing)\b", n, re.I):
            facts.append({"key": "site_rule", "value": n.rstrip("."), "kind": "rule"})
        else:
            facts.append({"key": "event", "value": f"2026-09-15: {n.rstrip('.')}", "kind": "event"})
    return json_reply({"facts": facts, "dropped": dropped}, complexity=0.5)


# ============================================================================================ cache lab
CACHE_LAB = "<adv_day3_cache_lab>"


@scenario("adv.day3.cache_lab", match=lambda r: CACHE_LAB in r.system_text, priority=10)
def cache_lab(req: MockRequest) -> Reply:
    question = req.last_user_text or req.first_user_text
    q_terms = _terms(question)
    best, best_score = "", 0
    for ln in req.system_text.splitlines():
        if not ln.strip() or ln.startswith("#") or ln.startswith("<"):
            continue
        score = len(q_terms & _terms(ln))
        if score > best_score:
            best, best_score = ln.strip(), score
    if best_score >= 2:
        cells = [c.strip().replace("**", "") for c in best.strip("|").split("|")] if best.startswith("|") else [best]
        return say("From the reference card: " + " - ".join(c for c in cells if c), complexity=0.2)
    return say("Acknowledged.", complexity=0.1)
