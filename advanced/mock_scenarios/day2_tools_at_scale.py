"""Mock policies for Day 2 - Tool engineering at scale.

Six rule-based stand-ins for Claude, each matched by a marker in the lab's system prompt:

  adv.day2.tool_search  <adv_day2_tool_search>  lab 02: writes a search query from the request, reports what it found
  adv.day2.wide_agent   <adv_day2_wide_agent>   lab 03: plans one tool per clause of the request, searches the deferred
                                                catalog when the best tool is not loaded, escalates what no tool covers
  adv.day2.phases       <adv_day2_phases>       lab 04: the same planner, restricted to the tools loaded right now
  adv.day2.ptc          <adv_day2_ptc>          lab 05: telemetry triage as N direct calls, or as one code cell
  adv.day2.stream       <adv_day2_stream>       lab 06: drafts a long bulletin body into a tool input
  adv.day2.selection    <adv_day2_selection>    lab 07 (and lab 01): picks exactly one loaded tool for a request

How the stand-in "chooses" - every decision is lexical and derived from the request, never from a lookup table:
  * the user's words are tokenised and stemmed; each tool's name, description, argument names and argument
    descriptions form its document; a BM25 ranking picks the best fit for each clause of the request. Questions
    down-weight write tools, descriptions that start with DEPRECATED are down-weighted, nothing else is special;
  * a search query is the same vocabulary: the rarest content words as a regex alternation for the regex variant,
    all content words for the BM25 variant;
  * arguments are filled from identifiers in the conversation (their formats are learned from the argument
    descriptions, e.g. "SO-10248"), from earlier tool results and from the argument's own description.
Live Claude reads the same descriptions and reasons about them; the stand-in only measures their vocabulary,
which is exactly what makes the description A/B of lab 07 move. Everything is deterministic.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from labkit.mock import MockRequest, Reply, ToolCall, say, scenario, search_tools, tool, use_tools
from labkit.mock.reply import run_code

MARK_SEARCH = "<adv_day2_tool_search>"
MARK_WIDE = "<adv_day2_wide_agent>"
MARK_PHASES = "<adv_day2_phases>"
MARK_PTC = "<adv_day2_ptc>"
MARK_STREAM = "<adv_day2_stream>"
MARK_SELECT = "<adv_day2_selection>"

TODAY = "2026-09-15"
NEXT_WEEK = ("2026-09-21", "2026-09-25")

STOP = frozenset("""a an the and or of to for in on at by with from into about as is are was were be been being it its this that
these those i you we they he she them our your their my me us do does did done have has had having can could should would will
shall may might must please need needs needed want wants wanted help tell give show find look check see let know make sure ask
asks asked say says said customer customers someone anyone here there now today tomorrow yesterday week month year next last
since until before after also just still again right up out off over under one two three four five first second new old any all
some no not nothing hasn haven hadn isn aren wasn doesn didn don won cannot what which who whom whose when where why how much
many more most very really thanks thank hi hello ok okay yes got go going come came back friday monday tuesday wednesday thursday
saturday sunday per each every via like onto put they them their us then that this it if so ours mine yours whether about
right currently""".split())
WRITE_VERBS = frozenset("create book open apply cancel send post update issue record reserve transfer schedule waive register close "
                        "reopen release hold assign acknowledge set link notify escalate request generate log add file reroute".split())
ACTION_WORDS = frozenset("create book open apply cancel send post update issue record reserve transfer schedule waive register close "
                         "reopen release hold assign acknowledge set link notify escalate request generate log add file reroute move "
                         "put change place drop knock refund reduce credit pay send block register acknowledge order".split())
SAFETY_WORDS = ("leak", "fire", "smell", "injur", "spill", "safety", "unattended", "emergency", "smoke", "wet seal", "solvent")

SERIAL = re.compile(r"\b[A-Z]{2,3}\d{2,4}-\d{4}-\d{4}\b")
TRACKING = re.compile(r"\b[A-Z]{3}\d{10}\b")
LOT = re.compile(r"\b[A-Z]{2}-\d{4}-[A-Z]\b")
FAULT = re.compile(r"\b[FE]\d{2}\b")
SKU = re.compile(r"\b(?:KP-\d{3}(?:-[A-Z])?|MS-\d{3}(?:-R)?|KC-\d(?:-[A-Z]{3})?)\b")
WAREHOUSE = re.compile(r"\bWH-(?:EAST|WEST|EU)\b")
REGION = re.compile(r"\b(?:US-EAST|US-WEST|EU|APAC)\b")
DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
AMOUNT = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
EG_PREFIX = re.compile(r"e\.g\.\s*([A-Z]{1,5}-)(?=[0-9A-Z])")


# ------------------------------------------------------------------------------------------ text helpers
def _stem(w: str) -> str:
    for suf in ("ations", "ation", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    if len(w) > 5 and w.endswith("y"):
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2]:
        w = w[:-1]
    return w


def _raw_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def content_words(text: str) -> list[str]:
    """The words a model would search for: no stopwords, no bare numbers, no identifiers, deduplicated, in order."""
    out: list[str] = []
    for w in _raw_tokens(re.sub(r"\b[A-Z]{1,5}-[0-9A-Z-]+\b|\b[A-Z]{3}\d{10}\b|\b[A-Z]{2,3}\d{2,4}-\d{4}-\d{4}\b", " ", text)):
        if w in STOP or len(w) < 3 or w.isdigit() or w in out:
            continue
        out.append(w)
    return out


def terms_of(text: str) -> list[str]:
    return list(dict.fromkeys(_stem(w) for w in content_words(text)))


def _doc(t: dict) -> list[str]:
    parts = [t.get("name", "").replace("_", " "), t.get("description", "")]
    for prop, schema in ((t.get("input_schema") or {}).get("properties") or {}).items():
        parts.append(prop.replace("_", " "))
        if isinstance(schema, dict):
            parts.append(str(schema.get("description", "")))
            if isinstance(schema.get("enum"), list):
                parts.append(" ".join(map(str, schema["enum"])))
    return [_stem(w) for w in _raw_tokens(" ".join(parts))]


def id_words(text: str, tools: list[dict]) -> list[str]:
    """Words a model infers from identifiers: 'SO-10248' means 'order' because an argument named order_id gives SO- as its
    example - so the mapping comes from the tool definitions in the request, not from a table in this file."""
    prefixes: dict[str, list[str]] = {}
    for t in tools:
        for prop, schema in ((t.get("input_schema") or {}).get("properties") or {}).items():
            for m in EG_PREFIX.finditer(str((schema or {}).get("description", ""))):
                prefixes.setdefault(m.group(1), [w for w in prop.replace("_id", "").split("_") if w])
    words: list[str] = []
    for prefix, base in prefixes.items():
        if re.search(rf"\b{re.escape(prefix)}[0-9A-Z]", text):
            words += base
    if SERIAL.search(text):
        words += ["serial", "number", "unit"]
    if TRACKING.search(text):
        words += ["tracking", "number"]
    if LOT.search(text):
        words.append("lot")
    if FAULT.search(text):
        words += ["fault", "code"]
    if SKU.search(text):
        words.append("sku")
    if WAREHOUSE.search(text):
        words.append("warehouse")
    if REGION.search(text):
        words.append("region")
    return list(dict.fromkeys(words))


# ------------------------------------------------------------------------------------------ ranking
def custom_tools(req: MockRequest) -> list[dict]:
    """Client tools in the request (deferred ones included), inline definitions added mid-conversation too."""
    out = [t for t in req.tools if t.get("type") in (None, "custom") and t.get("name")]
    out += [d for d in req.inline_tool_definitions.values() if d.get("name") not in {t["name"] for t in out}]
    return out


def rank(query: list[str], tools: list[dict], *, question: bool = False) -> list[tuple[float, str, list[str]]]:
    docs = [(t["name"], _doc(t), t.get("description", "")) for t in tools]
    n = len(docs) or 1
    avg = (sum(len(d) for _, d, _ in docs) / n) or 1.0
    df = {term: sum(1 for _, d, _ in docs if term in d) for term in set(query)}
    scored = []
    for name, doc, desc in docs:
        score, matched = 0.0, []
        for term in query:
            tf = doc.count(term)
            if not tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(doc) / avg))
            matched.append(term)
        if score <= 0:
            continue
        if question and name.split("_")[0] in WRITE_VERBS:
            score *= 0.5
        if re.match(r"\s*\[?deprecated", desc, re.I):
            score *= 0.2
        scored.append((score, name, matched))
    return sorted(scored, key=lambda x: (-x[0], x[1]))


def clauses_of(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!;])\s+|\s*;\s*|\s+-\s+|,\s+and\s+(?=[a-z])|\s+then\s+|\n+", text.strip())
    return [p.strip(" ,.;:") for p in parts if len(content_words(p)) >= 1]


def is_question(clause: str) -> bool:
    words = set(_raw_tokens(clause))
    return not (words & ACTION_WORDS)


def search_query(clause: str, tools: list[dict], variant: str) -> str:
    words = content_words(clause) + [w for w in id_words(clause, tools) if w not in content_words(clause)]
    if variant == "regex":
        docs = [_doc(t) for t in tools]
        n = len(docs) or 1
        # the rarest three words across the catalog make the most specific pattern (a model knows its vocabulary)
        rarity = {w: sum(1 for d in docs if _stem(w) in d) for w in words}
        chosen = sorted(words, key=lambda w: (rarity[w] == 0, rarity[w], words.index(w)))[:3]
        forms = []
        for w in chosen:
            f = w[:-3] if w.endswith("ing") and len(w) > 6 else w[:-1] if w.endswith("s") and len(w) > 4 else w
            forms.append(re.escape(f))
        return "|".join(forms)
    return " ".join(words)


# ------------------------------------------------------------------------------------------ conversation views
def segment(req: MockRequest) -> tuple[str, int]:
    """The latest user question (a user message with text and no tool results) and its index."""
    msgs = req.messages
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") != "user":
            continue
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content or [] if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            continue
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if text:
            return text, i
    return "", 0


def since(req: MockRequest, q_idx: int) -> tuple[list[ToolCall], list[dict], list[dict]]:
    """Tool calls, searches (inputs) and search results made since the latest question."""
    calls, searches, results = [], [], []
    ids = set()
    for m in req.messages[q_idx + 1:]:
        if m.get("role") != "assistant":
            continue
        for b in m.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                ids.add(b.get("id"))
            elif b.get("type") == "server_tool_use" and str(b.get("name", "")).startswith("tool_search"):
                searches.append(b.get("input") or {})
            elif b.get("type") == "tool_search_tool_result":
                results.append(b.get("content") or {})
    calls = [c for c in req.tool_calls if c.id in ids]
    return calls, searches, results


def _walk(value: Any, key: str, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, dict):
        if key in value and value[key] not in (None, "", []):
            return value[key]
        for v in value.values():
            found = _walk(v, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for v in value[:3]:
            found = _walk(v, key, depth + 1)
            if found is not None:
                return found
    return None


def from_results(req: MockRequest, key: str) -> Any:
    for c in reversed(req.tool_calls):
        if c.is_error or c.result is None:
            continue
        data = c.result_json()
        found = _walk(data, key)
        if found is not None:
            return found
    return None


# ------------------------------------------------------------------------------------------ argument filling
def _enum_from_description(desc: str) -> list[str]:
    body = desc.split(":", 1)[1] if ":" in desc else desc
    options = re.findall(r"\b([A-Za-z][A-Za-z0-9_]{1,30})\b", body)
    return [o for o in options if o.lower() not in ("or", "and", "e", "g", "the", "a", "of", "to", "in")]


def fill_args(t: dict, req: MockRequest, text: str, clause: str) -> dict | None:
    """Fill a tool's arguments from the conversation; None when a required argument is not available (yet)."""
    schema = t.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    convo = text + "\n" + req.conversation_text
    lower = text.lower()
    args: dict[str, Any] = {}

    def prefix_regex(desc: str) -> list[re.Pattern]:
        return [re.compile(rf"\b{re.escape(p)}[0-9A-Z][0-9A-Z-]*\b") for p in EG_PREFIX.findall(desc)]

    for prop, ps in props.items():
        desc = str((ps or {}).get("description", ""))
        kind = (ps or {}).get("type", "string")
        value: Any = None
        if prop == "serial_number":
            value = (SERIAL.search(text) or SERIAL.search(convo) or [None])
            value = value.group(0) if hasattr(value, "group") else from_results(req, "serial_number")
        elif prop == "tracking_number":
            m = TRACKING.search(text)
            value = m.group(0) if m else from_results(req, "tracking_number")
        elif prop == "lot":
            m = LOT.search(text)
            value = m.group(0) if m else from_results(req, "lot") or from_results(req, "seal_lot")
        elif prop == "fault_code":
            m = FAULT.search(clause) or FAULT.search(text)
            value = m.group(0) if m else from_results(req, "code")
        elif prop in ("sku", "from_warehouse", "to_warehouse", "warehouse", "region"):
            pat = {"sku": SKU, "warehouse": WAREHOUSE, "from_warehouse": WAREHOUSE, "to_warehouse": WAREHOUSE, "region": REGION}[prop]
            found = pat.findall(text)
            if prop == "to_warehouse" and len(found) > 1:
                value = found[1]
            elif found:
                value = found[0]
            elif prop == "sku":
                value = from_results(req, "sku")
        elif prop in ("from_date", "to_date", "date", "since", "due_date"):
            dates = DATE.findall(text)
            if prop in ("from_date", "date", "since", "due_date"):
                value = dates[0] if dates else (NEXT_WEEK[0] if "next week" in lower else None)
            else:
                value = dates[1] if len(dates) > 1 else (NEXT_WEEK[1] if "next week" in lower else (dates[0] if dates else None))
        elif prop == "days":
            m = re.search(r"(\d{1,3})[- ]day|last (\d{1,3}) days", lower)
            value = int(m.group(1) or m.group(2)) if m else (7 if "week" in lower else 7)
        elif prop == "amount_usd":
            m = AMOUNT.search(clause) or AMOUNT.search(text)
            if m:
                value = float(m.group(1).replace(",", ""))
            elif PERCENT.search(clause):
                amount = from_results(req, "amount_usd")
                value = round(float(amount) * float(PERCENT.search(clause).group(1)) / 100, 2) if amount else None
        elif prop in ("qty", "pieces"):
            m = re.search(r"\b(\d{1,3})\s*(?:x\s+|units?|pieces?|kits?|of\b|\b)", clause, re.I) or re.search(r"\bx\s*(\d{1,3})\b", clause, re.I)
            value = int(m.group(1)) if m else (1 if prop == "qty" else None)
        elif prop == "percent":
            m = PERCENT.search(clause)
            value = float(m.group(1)) if m else None
        elif prop in ("priority",):
            m = re.search(r"\bP[1-4]\b", text)
            value = m.group(0) if m else ("P1" if any(w in lower for w in SAFETY_WORDS) else "P3")
        elif prop == "severity":
            value = "safety" if any(w in lower for w in SAFETY_WORDS) else "medium"
        elif prop == "queue":
            value = infer_queue(clause)
        elif prop == "approver_role":
            value = "quality_lead" if "quality" in lower or "lot" in lower else "support_manager"
        elif prop == "channel":
            m = re.search(r"\b(logistics|billing|quality|field[- ]service)\b", lower)
            value = m.group(1).replace(" ", "-") if m else "support"
        elif prop == "team":
            m = re.search(r"\b(logistics|billing|quality|field[- ]service|support)\b", lower)
            value = m.group(1).replace(" ", "-") if m else "support"
        elif prop in ("skill", "service", "metric", "status", "field", "family", "reason_code", "country", "name", "topic", "section"):
            options = _enum_from_description(desc)
            hit = next((o for o in options if re.search(rf"\b{re.escape(o.lower())}\b", lower)), None)
            if prop == "family" and hit is None:
                m = re.search(r"\b(KC-[12]|KP-\d{3})\b", text)
                hit = m.group(1) if m else None
            if prop == "country" and hit is None:
                m = re.search(r"\bto ([A-Z]{2})\b", text)
                hit = m.group(1) if m else None
            if prop == "name" and hit is None:
                m = re.search(r"runbook (?:for|named|on) ([a-z ]+)", lower) or re.search(r"(?:policy|the) ([a-z_ ]+?) policy", lower)
                hit = m.group(1).strip() if m else None
            if prop == "topic" and hit is None:
                hit = " ".join(content_words(clause)[:2])
            value = hit
        elif prop == "engineer_id":
            m = re.search(r"\bFSE-\d{2}\b", text)
            value = m.group(0) if m else from_results(req, "engineer_id")
        elif prop == "slot_start":
            value = from_results(req, "slot_start")
        elif prop == "email" or prop == "to":
            m = EMAIL.search(text)
            value = m.group(0) if m else from_results(req, "email")
        elif prop in ("summary", "justification", "action", "description", "resolution", "note", "reason", "text", "body", "title",
                      "subject", "query", "address", "location", "value", "reference", "options"):
            if prop == "query":
                value = " ".join(content_words(clause)[:6]) or clause[:80]
            elif prop == "location":
                m = re.search(r"\b(?:SITE-\d{4}-[A-Z]|WH-(?:EAST|WEST|EU))\b", text)
                value = m.group(0) if m else None
            elif prop == "reference":
                m = re.search(r"reference ([A-Z0-9-]+)", text)
                value = m.group(1) if m else (re.search(r"\b(?:SVC|SO|RMA)-\d+\b", text) or [None])
                value = value if isinstance(value, str) else (value.group(0) if value else "support request")
            elif prop in ("text", "body"):
                value = f"Update from Kestrel support ({TODAY}): {clause[:240]}"
            elif prop == "summary":
                value = f"{clause[:200]} (from: {text[:120]})" if clause != text else text[:300]
            elif prop == "title":
                value = clause[:80]
            elif prop == "subject":
                value = clause[:60]
            else:
                value = clause[:240]
        else:
            # generic: an ID whose format the argument's own description shows, a value from earlier results, else nothing
            for pat in prefix_regex(desc):
                m = pat.search(text) or pat.search(convo)
                if m:
                    value = m.group(0)
                    break
            if value is None:
                value = from_results(req, prop)
            if value is None and prop.endswith("_id"):
                value = None
        if value is None and prop in required:
            return None
        if value is not None:
            if kind == "integer" and not isinstance(value, int):
                try:
                    value = int(float(value))
                except (TypeError, ValueError):
                    return None if prop in required else args
            if kind == "number" and not isinstance(value, (int, float)):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return None if prop in required else args
            if kind == "array" and isinstance(value, str):
                value = [value]
            args[prop] = value
    return args


def infer_queue(clause: str) -> str:
    lower = clause.lower()
    for queue, words in (("security", ("phish", "fraud", "inject", "override", "suspicious")),
                         ("logistics", ("carrier", "shipment", "track", "scan", "deliver", "customs", "pickup", "pallet", "parcel")),
                         ("billing", ("invoice", "refund", "credit", "payment", "fee", "balance", "paid", "charge")),
                         ("quality", ("lot", "hold", "incident", "test", "build")),
                         ("field_service", ("engineer", "visit", "ticket", "fault", "telemetry", "vibration", "warranty", "seal", "leak", "pump"))):
        if any(w in lower for w in words):
            return queue
    return "account_management"


# ------------------------------------------------------------------------------------------ answers
def describe_result(c: ToolCall) -> str:
    args = ", ".join(f"{k}={v}" for k, v in list(c.input.items())[:3])
    if c.is_error:
        data = c.result_json() or {}
        err = data.get("error") if isinstance(data, dict) else None
        msg = (err or {}).get("message") if isinstance(err, dict) else (c.result or "")
        return f"{c.name}({args}) failed: {msg}"
    data = c.result_json()
    if not isinstance(data, dict):
        return f"{c.name}({args}): {(c.result or '')[:160]}"
    facts = []
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) and v not in ("", None):
            facts.append(f"{k} {v}")
        elif isinstance(v, list) and v:
            first = v[0]
            head = ", ".join(f"{a}={b}" for a, b in (first.items() if isinstance(first, dict) else [])
                             if isinstance(b, (str, int, float)))[:120]
            facts.append(f"{len(v)} {k}" + (f" (first: {head})" if head else ""))
        elif isinstance(v, dict) and v:
            head = ", ".join(f"{a}={b}" for a, b in v.items() if isinstance(b, (str, int, float)))[:120]
            facts.append(f"{k}: {head}")
        if len(facts) >= 6:
            break
    return f"{c.name}({args}): " + "; ".join(facts)


def compose(calls: list[ToolCall], unmet: list[str], escalated: bool) -> str:
    lines = [describe_result(c) for c in calls if not c.name.startswith("escalate")]
    esc = [c for c in calls if c.name == "escalate_to_human" and not c.is_error]
    if unmet and esc:
        d = esc[-1].result_json() or {}
        lines.append(f"I have no tool for: {'; '.join(unmet)}. Escalated to the {d.get('queue')} queue as {d.get('escalation_id')} "
                     f"({d.get('priority')}, {d.get('sla')}).")
    elif unmet:
        lines.append(f"I could not do the following with the tools available to me: {'; '.join(unmet)}.")
    return " ".join(lines) if lines else "I did not need any tool for that."


# ------------------------------------------------------------------------------------------ the planner
def plan(req: MockRequest, *, allow_search: bool, escalate_unmet: bool, single: bool = False) -> Reply:
    """One tool per clause of the latest question: search when the best tool is deferred and a search tool exists,
    call when it is loaded and its arguments are available, defer a clause whose arguments depend on another
    clause's result, escalate (or admit) what no tool covers. Answer when nothing is pending."""
    text, q_idx = segment(req)
    calls, searches, _ = since(req, q_idx)
    tools = custom_tools(req)
    loaded = req.loaded_tool_names
    search_tool = req.server_tools.get("tool_search")
    variant = "regex" if search_tool and "regex" in search_tool["type"] else "bm25"
    by_name = {t["name"]: t for t in tools}
    already_searched = {(s.get("pattern") or s.get("query") or "") for s in searches}
    called = {c.name for c in calls}

    # error recovery: a failed call whose message names the tool to use instead (deprecations, wrong-tool errors)
    for c in calls:
        if c.is_error:
            data = c.result_json() or {}
            msg = str(data.get("error", data)) if isinstance(data, dict) else (c.result or "")
            m = re.search(r"use ([a-z_]+_v\d|[a-z_]+)\b", msg)
            if m and m.group(1) in loaded and m.group(1) not in called and m.group(1) in by_name:
                args = fill_args(by_name[m.group(1)], req, text, text)
                if args is not None:
                    return use_tools(tool(m.group(1), **{**c.input, **args}), thinking="Switch to the tool the error names.")

    pending: list[dict] = []
    unmet: list[str] = []
    waiting = False
    cls = [text] if single else clauses_of(text)
    for clause in cls:
        query = terms_of(clause) + [_stem(w) for w in id_words(clause, tools) if _stem(w) not in terms_of(clause)]
        candidates = tools if (allow_search and search_tool) else [t for t in tools if t["name"] in loaded]
        ranked = rank(query, candidates, question=is_question(clause))
        if not ranked:
            unmet.append(clause)
            continue
        score, name, matched = ranked[0]
        if name not in loaded:
            q = search_query(clause, tools, variant)
            if allow_search and search_tool and q not in already_searched:
                return Reply(content=[search_tools(q)], thinking_summary=f"No loaded tool fits '{clause[:40]}'; search the catalog.")
            # searched already and still not loaded: fall back to the best loaded tool, if any fits at all
            ranked = rank(query, [t for t in tools if t["name"] in loaded], question=is_question(clause))
            if not ranked:
                unmet.append(clause)
                continue
            score, name, matched = ranked[0]
        if name in called:
            continue
        args = fill_args(by_name[name], req, text, clause)
        if args is None:
            waiting = True
            continue
        if not any(p["name"] == name for p in pending):
            pending.append(tool(name, **args))
        if single:
            break
    if pending:
        preface = None if calls else ("Let me check." if len(pending) == 1 else "Let me look those up.")
        return use_tools(*pending, preface=preface, thinking="Pick the most specific tool for each part of the request.")
    if waiting and calls:
        # arguments for a later clause never became available: say what is missing
        return say("I could not complete every step: a later step needs a value that the earlier results did not provide. "
                   + compose(calls, unmet, False), complexity=0.4)
    if waiting:
        return say("I need one more detail before I can do that (an ID or a date that the request does not contain).", complexity=0.2)
    if unmet and escalate_unmet and "escalate_to_human" in loaded and "escalate_to_human" not in called:
        summary = f"Needs a person: {'; '.join(unmet)[:300]}. Context: {text[:200]}"
        priority = "P1" if any(w in text.lower() for w in SAFETY_WORDS) else "P3"
        return use_tools(tool("escalate_to_human", queue=infer_queue(" ".join(unmet)), priority=priority, summary=summary),
                         thinking="No tool covers part of the request: hand it to the right queue.")
    return say(compose(calls, unmet, "escalate_to_human" in called), complexity=0.45,
               thinking="Compose the answer only from the tool results.")


# ------------------------------------------------------------------------------------------ scenarios
@scenario("adv.day2.tool_search", match=lambda r: MARK_SEARCH in r.system_text, priority=10)
def tool_search_only(req: MockRequest) -> Reply:
    """Lab 02: write one search from the request, then report what the search found (no tool is called)."""
    text, q_idx = segment(req)
    _, searches, results = since(req, q_idx)
    search_tool = req.server_tools.get("tool_search")
    variant = "regex" if search_tool and "regex" in search_tool["type"] else "bm25"
    verbatim = re.search(r"`([^`]+)`", text)
    if not searches:
        query = verbatim.group(1) if verbatim else search_query(text, custom_tools(req), variant)
        return Reply(content=[search_tools(query)], thinking_summary="Search the catalog before choosing a tool.")
    last = results[-1] if results else {}
    if last.get("type") == "tool_search_tool_result_error":
        if verbatim and len(searches) == 1:
            fixed = re.sub(r"[()\[\]{}]", "", verbatim.group(1)) or "shipment"
            return Reply(content=[search_tools(fixed)], thinking_summary="The pattern was invalid; retry without the unbalanced bracket.")
        return say(f"The search failed ({last.get('error_code')}: {last.get('error_message')}).", complexity=0.2)
    names = [r.get("tool_name") for r in last.get("tool_references") or []]
    if not names:
        return say("The search matched no tool in the catalog; I would rephrase and search again.", complexity=0.2)
    return say(f"Found {len(names)} candidate tool(s): {', '.join(names)}. I would use {names[0]} next.", complexity=0.2)


@scenario("adv.day2.wide_agent", match=lambda r: MARK_WIDE in r.system_text, priority=10)
def wide_agent(req: MockRequest) -> Reply:
    return plan(req, allow_search=True, escalate_unmet=True)


@scenario("adv.day2.phases", match=lambda r: MARK_PHASES in r.system_text, priority=10)
def phased_agent(req: MockRequest) -> Reply:
    return plan(req, allow_search=False, escalate_unmet=False)


@scenario("adv.day2.selection", match=lambda r: MARK_SELECT in r.system_text, priority=10)
def selection(req: MockRequest) -> Reply:
    return plan(req, allow_search=False, escalate_unmet=False, single=True)


# ------------------------------------------------------------------------------------------ lab 05: PTC
TRIAGE_CELL = '''import asyncio, json
serials = {serials}
readings = [json.loads(r) for r in await asyncio.gather(*[get_pump_telemetry({{"serial_number": s}}) for s in serials])]
hot = [r for r in readings if r.get("seal_temp_c", 0) > {threshold}]
trends = {{}}
if hot:
    raw = await asyncio.gather(*[get_vibration_trend({{"serial_number": r["serial_number"], "days": 7}}) for r in hot])
    trends = {{r["serial_number"]: json.loads(t) for r, t in zip(hot, raw)}}
triage = []
for r in readings:
    t = trends.get(r["serial_number"], {{}})
    is_hot = r.get("seal_temp_c", 0) > {threshold}
    priority = "P1" if is_hot and t.get("assessment") == "rising" else "P2" if is_hot else "P3"
    triage.append({{"serial_number": r["serial_number"], "sku": r.get("sku"), "seal_temp_c": r.get("seal_temp_c"),
                   "vibration_mm_s": r.get("vibration_mm_s"), "trend": t.get("assessment", "not checked"), "priority": priority}})
triage.sort(key=lambda x: (x["priority"], -x["seal_temp_c"]))
print(json.dumps({{"units": len(readings), "hot": len(hot), "triage": triage}}))
'''

KITS_CELL = '''import json
regions = {regions}
kits = {{"KP-250-S": "MS-250-R", "KP-100-S": "MS-100-R"}}
need = {{}}
for row in triage:
    if row["priority"] in ("P1", "P2"):
        key = (regions.get(row["serial_number"], "?"), kits.get(row["sku"], "n/a"))
        need[key] = need.get(key, 0) + 1
print(json.dumps([{{"region": r, "kit": k, "units": n}} for (r, k), n in sorted(need.items())]))
'''


def _threshold(text: str) -> int:
    m = re.search(r"(?:above|over|>)\s*(\d{2,3})\s*(?:°|deg)?\s*C\b", text, re.I)
    return int(m.group(1)) if m else 70


def _regions_from_text(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        s, r = SERIAL.search(line), REGION.search(line)
        if s and r:
            out[s.group(0)] = r.group(0)
    return out


@scenario("adv.day2.ptc", match=lambda r: MARK_PTC in r.system_text, priority=10)
def ptc_triage(req: MockRequest) -> Reply:
    text, q_idx = segment(req)
    calls, _, _ = since(req, q_idx)
    serials = list(dict.fromkeys(SERIAL.findall(text)))
    threshold = _threshold(req.first_user_text)
    kits_turn = "kit" in text.lower() and "region" in text.lower()
    programmatic = req.server_tools.get("code_execution") is not None and bool(req.code_callable_tools)

    if programmatic:
        if req.completed_code is not None:
            out = req.completed_code["content"]
            if out.get("return_code") != 0:
                return say(f"The script failed: {out.get('stderr', '')[:200]}", complexity=0.3)
            try:
                data = json.loads(out["stdout"].strip().splitlines()[-1])
            except (ValueError, IndexError):
                return say("Script output: " + out.get("stdout", "")[:400], complexity=0.3)
            if isinstance(data, dict) and "triage" in data:
                rows = "; ".join(f"{r['serial_number']} {r['priority']} ({r['seal_temp_c']} C, {r['trend']})" for r in data["triage"])
                return say(f"Triage of {data['units']} units, {data['hot']} above {threshold} C: {rows}.", complexity=0.4)
            return say("Kits needed by region: " + "; ".join(f"{r['region']} {r['kit']} x{r['units']}" for r in data) + ".", complexity=0.3)
        if req.code_results:            # a cell already ran for this question and the answer was given
            return say("Done.", complexity=0.1)
        if kits_turn:
            return Reply(content=[run_code(KITS_CELL.format(regions=json.dumps(_regions_from_text(req.first_user_text))))],
                         thinking_summary="The triage list is still in the container; aggregate it there.")
        return Reply(content=[run_code(TRIAGE_CELL.format(serials=json.dumps(serials), threshold=threshold))],
                     thinking_summary="Fan out the telemetry calls in code and only return the triage table.")

    # standard tool use: every result comes back into the context
    if kits_turn:
        regions = _regions_from_text(req.first_user_text)
        kits = {"KP-250-S": "MS-250-R", "KP-100-S": "MS-100-R"}
        need: dict[tuple[str, str], int] = {}
        for c in req.tool_calls:
            if c.name == "get_pump_telemetry" and not c.is_error:
                d = c.result_json() or {}
                if d.get("seal_temp_c", 0) > threshold:
                    key = (regions.get(d["serial_number"], "?"), kits.get(d.get("sku"), "n/a"))
                    need[key] = need.get(key, 0) + 1
        return say("Kits needed by region: " + "; ".join(f"{r} {k} x{n}" for (r, k), n in sorted(need.items())) + ".", complexity=0.4)
    tele = [c for c in calls if c.name == "get_pump_telemetry"]
    if not tele:
        return use_tools(*[tool("get_pump_telemetry", serial_number=s) for s in serials], preface="Pulling the latest readings for every unit.")
    hot = [c.result_json() for c in tele if not c.is_error and (c.result_json() or {}).get("seal_temp_c", 0) > threshold]
    trends = {c.input.get("serial_number"): c.result_json() for c in calls if c.name == "get_vibration_trend" and not c.is_error}
    missing = [r["serial_number"] for r in hot if r["serial_number"] not in trends]
    if missing:
        return use_tools(*[tool("get_vibration_trend", serial_number=s, days=7) for s in missing],
                         preface=f"{len(hot)} units are above {threshold} C; checking their vibration trends.")
    rows = []
    for c in tele:
        d = c.result_json() or {}
        t = trends.get(d.get("serial_number"), {})
        is_hot = d.get("seal_temp_c", 0) > threshold
        pr = "P1" if is_hot and t.get("assessment") == "rising" else "P2" if is_hot else "P3"
        rows.append((pr, -d.get("seal_temp_c", 0), f"{d.get('serial_number')} {pr} ({d.get('seal_temp_c')} C, {t.get('assessment', 'not checked')})"))
    rows.sort()
    return say(f"Triage of {len(tele)} units, {len(hot)} above {threshold} C: " + "; ".join(r[2] for r in rows) + ".", complexity=0.5)


# ------------------------------------------------------------------------------------------ lab 06: streaming
@scenario("adv.day2.stream", match=lambda r: MARK_STREAM in r.system_text, priority=10)
def bulletin_writer(req: MockRequest) -> Reply:
    text, q_idx = segment(req)
    calls, _, _ = since(req, q_idx)
    drafts = [c for c in calls if c.name == "draft_bulletin"]
    if drafts:
        d = drafts[-1].result_json() or {}
        if drafts[-1].is_error:
            return say(f"The draft was rejected: {(d.get('error') or {}).get('message', drafts[-1].result)}. I will correct the input and try again.",
                       complexity=0.3)
        return say(f"Bulletin {d.get('bulletin_id')} is drafted ({d.get('words')} words) and awaits review by {d.get('review_by')}.", complexity=0.3)
    # facts from the request: lots, hazards, remedies, interim measures (the stand-in copies them into the body)
    lots = LOT.findall(text)
    hazard = re.findall(r"hazard[^:]*:\s*([^\n]+)", text, re.I)
    remedy = re.findall(r"remedy[^:]*:\s*([^\n]+)", text, re.I)
    interim = re.findall(r"interim[^:]*:\s*([^\n]+)", text, re.I)
    m = re.search(r"\bTSB-\d{4}-\d{2}\b", text)
    bulletin_id = m.group(0) if m else "TSB-2026-09"
    paragraphs = [f"Kestrel Pumps & Controls - Technical Safety Bulletin {bulletin_id}. Issued {TODAY}. Applies to units built with "
                  f"{' and '.join(lots) if lots else 'the affected lots'}. This bulletin supersedes any verbal guidance given by field staff."]
    for i, lot in enumerate(lots or ["the affected lot"]):
        h = hazard[i] if i < len(hazard) else "see the recall notice"
        r = remedy[i] if i < len(remedy) else "a field visit by a Kestrel engineer"
        it = interim[i] if i < len(interim) else "follow the interim measure in the recall notice"
        paragraphs.append(f"Lot {lot}. Hazard: {h.strip()} Remedy: {r.strip()} Interim measure until the remedy is applied: {it.strip()}")
    paragraphs.append("What operators must do now: (1) identify affected serial numbers from the nameplate and the build record; "
                      "(2) apply the interim measure; (3) do not run affected units unattended on hazardous duty; (4) book the remedy "
                      "visit through Kestrel field service; (5) keep this bulletin with the site's maintenance records. Contact "
                      "support@kestrel-pumps.example or your account manager with the serial numbers to schedule the visit. "
                      "No charge applies to the remedy, parts or labour under recall RC-2026-03.")
    paragraphs.append("Background: the elastomer batch and the capacitor batch were traced by lot; units outside these lots are "
                      "not affected. Kestrel has stopped shipment of remaining stock from both lots and is inspecting inventory. "
                      "We apologise for the disruption and will confirm each completed remedy in writing.")
    body = "\n\n".join(paragraphs)
    return use_tools(tool("draft_bulletin", bulletin_id=bulletin_id, title=f"{bulletin_id}: field replacement campaign RC-2026-03",
                          audience="operators", body=body), preface="Drafting the bulletin from the recall facts.",
                     thinking="Write the full body into the tool input; the reviewer edits the draft, not the chat.")
