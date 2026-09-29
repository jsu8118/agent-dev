"""Mock policies for Day 2 - Tool engineering at scale.

Rule-based stand-ins for Claude, each matched by a marker in the lab's system prompt:

  adv.day2.tool_search  <adv_day2_tool_search>  lab 02: writes one tool-search query from the request and reports what
                                                the search returned (it does not call the tool)
  adv.day2.wide_agent   <adv_day2_wide_agent>   labs 03 and 07: plans one tool per clause of the request from the tools
                                                it can see; searches the deferred catalog when none of them covers a
                                                clause; escalates what nothing covers
  adv.day2.phases       <adv_day2_phases>       lab 04: the same planner without escalation (the application, not the
                                                model, decides which tools exist in each phase)
  adv.day2.selection    <adv_day2_selection>    labs 01 and 07: picks exactly one visible tool for the whole request
  adv.day2.ptc          <adv_day2_ptc>          lab 05: recall triage as direct tool calls (parallel, or one per turn
                                                when parallel calls are disabled) or as a code cell calling the tools
  adv.day2.stream       <adv_day2_stream>       lab 06: drafts a long bulletin into a client tool's input

How the stand-in "chooses" - every decision is lexical and derived from the request, never from a lookup table:
  * the request is split into clauses; each clause's content words are stemmed; each tool's name, description,
    argument names, argument descriptions and enums form its document;
  * a visible tool COVERS a clause when it shares at least a third of the clause's content words; the covering tool
    with the best BM25 score wins (questions down-weight write tools; descriptions that start with DEPRECATED are
    down-weighted). The stand-in only ranks tools the model can see (non-deferred, discovered, added) - it never
    peeks at deferred definitions; when nothing visible covers a clause and a tool search tool is declared, it
    searches with the clause's words (rarest words as a regex alternation, all words as a BM25 query);
  * arguments come from identifiers in the request (their formats are read from the argument descriptions, e.g.
    "e.g. SO-10248"), from earlier tool results and from the arguments' own descriptions; optional arguments are only
    filled when the request states them.
Live Claude reads the same descriptions and reasons about them; the stand-in only measures their vocabulary, which is
exactly what makes the description A/B of lab 07 move. Everything is deterministic.

One search per response, with the terms of every clause nothing visible covers (and a larger `limit` when there are
several): after a search the stand-in acts on what it found (tool calls or an answer) and searches again on a later
turn if a clause is still uncovered. It knows it is continuing a response from `req.partial_response`. That view holds
the blocks since the policy was last asked, so a second search in the same response would lose sight of the first
one's discoveries; live Claude may search more than once per response.
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
COVER_MIN = 0.34            # share of a clause's content words a tool's document must contain to cover it

STOP = frozenset("""a an the and or of to for in on at by with from into about as is are was were be been being it its this that
these those i you we they he she them our your their my me us do does did done have has had having can could should would will
shall may might must please need needs needed want wants wanted help tell give show find look check see let know make sure ask
asks asked say says said customer customers someone anyone here there now today tomorrow yesterday week month year next last
since until before after also just still again right up out off over under one two three four five first second new old any all
some no not nothing hasn haven hadn isn aren wasn doesn didn don won cannot what which who whom whose when where why how much
many more most very really thanks thank hi hello ok okay yes got go going come came back friday monday tuesday wednesday thursday
saturday sunday per each every via like onto put then if so ours mine yours whether currently get gets pull pulled because
still keep keeps kept""".split())
WRITE_VERBS = frozenset("create book open apply cancel send post update issue record reserve transfer schedule waive register close "
                        "reopen release hold assign acknowledge set link notify escalate request generate log add file reroute draft".split())
ACTION_WORDS = frozenset("create book open apply cancel send post update issue record reserve transfer schedule waive register close "
                         "reopen release hold assign acknowledge set link notify escalate request generate log add file reroute move "
                         "put change place drop knock refund reduce credit pay block draft decode list find pull".split())
SAFETY_WORDS = ("leak", "fire", "smell", "injur", "spill", "safety", "unattended", "emergency", "smoke", "wet seal", "solvent")

SERIAL = re.compile(r"\b[A-Z]{2,3}\d{1,4}-\d{4}-\d{4}\b")
TRACKING = re.compile(r"\b[A-Z]{3}\d{10}\b")
LOT = re.compile(r"\b[A-Z]{2}-\d{4}-[A-Z]\b")
FAULT = re.compile(r"\b[FE]\d{2}\b")
SKU = re.compile(r"\b(?!FSE-)[A-Z]{2,3}-\d{1,3}(?:-[A-Z0-9]{1,3})?\b(?!-)")
FAMILY = re.compile(r"\b(?:KC-[12]|KP-\d{3})\b(?!-)")
WAREHOUSE = re.compile(r"\bWH-(?:EAST|WEST|EU)\b")
REGION = re.compile(r"\b(?:US-EAST|US-WEST|EU|APAC)\b")
DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
AMOUNT = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
EG_PREFIX = re.compile(r"e\.g\.\s*(?:seal lot |board lot )?([A-Z]{1,5}-)(?=[0-9A-Z])")
ANY_ID = re.compile(r"\b[A-Z]{1,5}-[0-9A-Z][0-9A-Z-]*\b|\b[A-Z]{3}\d{10}\b|\b[A-Z]{2,3}\d{1,4}-\d{4}-\d{4}\b|\b[FE]\d{2}\b")
PROPER_NAME = re.compile(r"(?<![.?!]\s)(?<!^)\b[A-Z][a-z]+(?:\s+(?:&\s+)?[A-Z][a-z]+)+")
FORMATS = {                 # argument name -> the identifier format a model recognises for it
    "serial_number": SERIAL, "tracking_number": TRACKING, "lot": LOT, "fault_code": FAULT, "sku": SKU, "family": FAMILY,
    "warehouse": WAREHOUSE, "from_warehouse": WAREHOUSE, "to_warehouse": WAREHOUSE, "region": REGION,
    "engineer_id": re.compile(r"\bFSE-\d{2}\b"), "bulletin_id": re.compile(r"\bTSB-\d{4}-\d{2}\b"),
    "location": re.compile(r"\b(?:SITE-\d{4}-[A-Z]|WH-(?:EAST|WEST|EU))\b"), "hold_id": re.compile(r"\bQH-\d{4}-\d{3}\b"),
}
FREE_TEXT = ("summary", "justification", "action", "description", "resolution", "note", "reason", "text", "body", "title",
             "subject", "query", "address", "value", "reference", "options", "name", "topic", "section")


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
    """The words a model would search for: no stopwords, no bare numbers, no identifiers, no multi-word names
    ("Lumen Data Centers"), deduplicated, in order."""
    out: list[str] = []
    cleaned = PROPER_NAME.sub(" ", ANY_ID.sub(" ", text))
    acronyms = {a.lower() for a in re.findall(r"\b[A-Z]{2,4}\b", cleaned)}      # PO, ETA, RMA: short but meaningful
    for w in _raw_tokens(cleaned):
        if w in STOP or (len(w) < 3 and w not in acronyms) or w.isdigit() or w in out:
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
                parts.append(" ".join(map(str, schema["enum"])).replace("_", " "))
    return [_stem(w) for w in _raw_tokens(" ".join(parts))]


def id_words(text: str, tools: list[dict], *, for_search: bool = False) -> list[str]:
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
    implied_by = [(TRACKING, ["tracking", "number"]), (LOT, ["lot"]), (FAULT, ["fault", "code"])]
    if not for_search:              # generic argument words help ranking visible tools, not a catalog search
        implied_by += [(SERIAL, ["serial", "number", "unit"]), (SKU, ["sku"]), (WAREHOUSE, ["warehouse"]), (REGION, ["region"])]
    for pattern, implied in implied_by:
        if pattern.search(text):
            words += implied
    return list(dict.fromkeys(words))


# ------------------------------------------------------------------------------------------ ranking
def custom_tools(req: MockRequest) -> list[dict]:
    """Client tools the API knows about (deferred ones included) plus definitions added inline mid-conversation."""
    out = [t for t in req.tools if t.get("type") in (None, "custom") and t.get("name")]
    names = {t["name"] for t in out}
    out += [d for d in req.inline_tool_definitions.values() if d.get("name") not in names]
    return out


def visible_tools(req: MockRequest) -> list[dict]:
    """What the model can see: non-deferred tools, tools discovered by search or added, minus removed ones."""
    loaded = req.loaded_tool_names
    return [t for t in custom_tools(req) if t["name"] in loaded]


def rank(query: list[str], tools: list[dict], *, question: bool = False) -> list[tuple[float, str, float]]:
    """BM25 over the tools' documents -> [(score, name, coverage)], best first. Coverage = share of the query's
    content terms (the first `len(query)` terms that are not id-derived) the tool's document contains."""
    docs = [(t["name"], _doc(t), t.get("description", "")) for t in tools]
    n = len(docs) or 1
    avg = (sum(len(d) for _, d, _ in docs) / n) or 1.0
    df = {term: sum(1 for _, d, _ in docs if term in d) for term in set(query)}
    scored = []
    for name, doc, desc in docs:
        score = 0.0
        for term in query:
            tf = doc.count(term)
            if not tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(doc) / avg))
        if score <= 0:
            continue
        if question and name.split("_")[0] in WRITE_VERBS:
            score *= 0.5
        if re.match(r"\s*\[?deprecated", desc, re.I):
            score *= 0.2
        scored.append((score, name, 0.0))
    return sorted(scored, key=lambda x: (-x[0], x[1]))


def coverage(terms: list[str], t: dict) -> float:
    if not terms:
        return 0.0
    doc = set(_doc(t))
    return sum(1 for term in terms if term in doc) / len(terms)


def clauses_of(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!;])\s+|\s*;\s*|\s+-\s+|,\s+and\s+(?=[a-z])|,?\s+then\s+|\n+", text.strip())
    return [p.strip(" ,.;:") for p in parts if len(content_words(p)) >= 1]


def is_question(clause: str) -> bool:
    """A clause without an action verb asks for information: write tools rank lower for it."""
    return not (set(_raw_tokens(clause)) & ACTION_WORDS)


def is_statement(clause: str) -> bool:
    """Context ('The customer says the carrier has not scanned it'): may prompt a search, never an escalation."""
    words = _raw_tokens(clause)
    asks = clause.rstrip().endswith("?") or (words and words[0] in ("what", "which", "who", "where", "when", "why", "how",
                                                                     "is", "are", "does", "do", "can", "could", "tell"))
    return not asks and not (set(words) & ACTION_WORDS)


def search_query(clauses: list[str], tools: list[dict], variant: str) -> str:
    """A query in the model's words: the rarest content words (regex alternation) or all of them (BM25)."""
    words: list[str] = []
    for clause in clauses:
        for w in content_words(clause) + id_words(clause, tools, for_search=True):
            if w not in words:
                words.append(w)
    if variant == "regex":
        docs = [_doc(t) for t in tools]
        rarity = {w: sum(1 for d in docs if _stem(w) in d) for w in words}
        per_clause = max(1, 3 // max(1, len(clauses)))
        chosen: list[str] = []
        for clause in clauses:
            order = [w for w in content_words(clause) if w in rarity]
            mine = sorted(order, key=lambda w: (rarity[w] == 0, rarity[w], order.index(w)))
            chosen += [w for w in mine[:per_clause] if w not in chosen]
        return "|".join(re.escape(_stem(w)) for w in chosen[:4]) or "tool"
    return " ".join(words)


# ------------------------------------------------------------------------------------------ conversation views
def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def segment(req: MockRequest) -> tuple[str, int]:
    """The latest user question (a user message with text and no tool results) and its index."""
    msgs = req.messages
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") != "user":
            continue
        blocks = _blocks(m.get("content"))
        if any(b.get("type") == "tool_result" for b in blocks):
            continue
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if text:
            return text, i
    return "", 0


def since(req: MockRequest, q_idx: int) -> tuple[list[ToolCall], list[dict], list[dict]]:
    """Tool calls, searches (their inputs) and search results made since the latest question."""
    searches, results, ids = [], [], set()
    for m in req.messages[q_idx + 1:]:
        if m.get("role") != "assistant":
            continue
        for b in _blocks(m.get("content")):
            if b.get("type") == "tool_use":
                ids.add(b.get("id"))
            elif b.get("type") == "server_tool_use" and str(b.get("name", "")).startswith("tool_search"):
                searches.append(b.get("input") or {})
            elif b.get("type") == "tool_search_tool_result":
                results.append(b.get("content") or {})
    return [c for c in req.tool_calls if c.id in ids], searches, results


def query_of(search_input: dict) -> str:
    return str(search_input.get("query") or search_input.get("pattern") or "")


def ends_with_search(req: MockRequest) -> bool:
    """True inside a response that has just run a tool search (the API asks the model to continue)."""
    partial = req.partial_response
    return bool(partial) and partial[-1].get("type") == "tool_search_tool_result"


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


def from_results(req: MockRequest, key: str, *, calls: list[ToolCall] | None = None) -> Any:
    for c in reversed(calls if calls is not None else req.tool_calls):
        if c.is_error or c.result is None:
            continue
        found = _walk(c.result_json(), key)
        if found is not None:
            return found
    return None


# ------------------------------------------------------------------------------------------ argument filling
def _options(desc: str) -> list[str]:
    """A closed set described in prose, e.g. 'no_longer_needed, wrong_item, defective, other.' -> the options."""
    body = desc.split(":", 1)[1] if ":" in desc else desc
    body = re.sub(r"\([^)]*\)|\be\.g\.", "", body)
    opts = [o.strip(" .") for o in re.split(r",|\bor\b", body)]
    return [o for o in opts if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,30}", o or "")]


def _mentioned(option: str, text: str) -> bool:
    words = [w for w in re.split(r"[_\s-]+", option.lower()) if w]
    stems = {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}
    return bool(words) and all(_stem(w) in stems for w in words)


def _find(pattern: re.Pattern, *texts: str) -> str | None:
    for t in texts:
        m = pattern.search(t or "")
        if m:
            return m.group(0)
    return None


def infer_queue(clause: str) -> str:
    lower = clause.lower()
    for queue, words in (("security", ("phish", "fraud", "inject", "override", "suspicious")),
                         ("billing", ("invoice", "refund", "credit", "payment", "fee", "balance", "paid", "charge")),
                         ("logistics", ("carrier", "shipment", "track", "scan", "deliver", "customs", "pickup", "pallet", "parcel")),
                         ("quality", ("lot", "hold", "incident", "test", "build")),
                         ("field_service", ("engineer", "visit", "ticket", "fault", "telemetry", "vibration", "warranty", "seal",
                                            "leak", "pump"))):
        if any(re.search(rf"\b{w}", lower) for w in words):
            return queue
    return "account_management"


def _value(prop: str, ps: dict, req: MockRequest, text: str, clause: str, required: bool, *, wide: bool = False) -> Any:
    """The value a model would pass for one argument. Required arguments may come from the clause, the whole request,
    the conversation and earlier tool results; optional ones only when the clause states them (`wide` lets the whole
    request supply one identifier to a tool that would otherwise get none)."""
    desc = str(ps.get("description", ""))
    lower = text.lower()
    texts = (clause, text) if (required or wide) else (clause,)
    convo = req.conversation_text if required else ""
    if isinstance(ps.get("enum"), list):                            # a closed set: the value the request names
        hit = next((o for o in ps["enum"] if _mentioned(str(o), clause)), None) or \
            next((o for o in ps["enum"] if _mentioned(str(o), text)), None)
        return hit if hit is not None or not required else ps["enum"][0]
    if prop in FORMATS:
        found = _find(FORMATS[prop], *texts)
        if prop == "to_warehouse":
            found = (WAREHOUSE.findall(text)[1:2] or [None])[0]
        if found is None and required:
            found = _find(FORMATS[prop], convo) or from_results(req, prop) or \
                (from_results(req, "seal_lot") if prop == "lot" else None) or \
                (from_results(req, "code") if prop == "fault_code" else None)
        return found
    if prop in ("from_date", "to_date", "date", "since", "due_date", "window_start", "window_end", "start", "end"):
        dates = DATE.findall(clause) or (DATE.findall(text) if required else [])
        if prop in ("to_date", "window_end", "end"):
            return dates[1] if len(dates) > 1 else (dates[0] if dates and required else None)
        return dates[0] if dates else None
    if prop == "days":
        m = re.search(r"(?:last|past)\s+(\d{1,3})\s+days|(\d{1,3})[- ]day", lower)
        return int(m.group(1) or m.group(2)) if m else (7 if required else None)
    if prop == "slot_start":
        return from_results(req, "slot_start") if required else None
    if prop in ("amount_usd", "threshold"):
        m = AMOUNT.search(clause) or (AMOUNT.search(text) if required else None)
        if m:
            return float(m.group(1).replace(",", ""))
        pct = PERCENT.search(clause)
        base = from_results(req, "open_amount_usd") or from_results(req, "amount_usd")
        if pct and base:
            return round(float(base) * float(pct.group(1)) / 100, 2)
        return None
    if prop in ("qty", "pieces", "reorder_point", "weight_kg"):
        unit = {"weight_kg": r"kg\b", "pieces": r"pieces?\b"}.get(prop, r"(?:x\s+|units?\b|pieces?\b|kits?\b|[A-Z]{2}-)")
        m = re.search(rf"\b(\d{{1,4}})\s*{unit}", clause) or re.search(r"\b(\d{1,3})\s*x\b", clause)
        if m:
            return int(m.group(1))
        return 1 if required and prop == "qty" else None
    if prop == "percent":
        m = PERCENT.search(clause)
        return float(m.group(1)) if m else None
    if prop == "priority":
        m = re.search(r"\bP[1-4]\b", text)
        return m.group(0) if m else (("P1" if any(w in lower for w in SAFETY_WORDS) else "P3") if required else None)
    if prop == "severity":
        return ("safety" if any(w in lower for w in SAFETY_WORDS) else "medium") if required else None
    if prop == "queue":
        return infer_queue(clause) if required else None
    if prop == "approver_role":
        return next((o for o in _options(desc) if _mentioned(o, text)), "support_manager")
    if prop in ("channel", "team"):
        m = re.search(r"\b(logistics|billing|quality|field[- ]service|support)\b", lower)
        return m.group(1).replace(" ", "-") if m else ("support" if required else None)
    if prop in ("email", "to"):
        return _find(EMAIL, *texts) or (from_results(req, "email") if required else None)
    if prop == "customer_id":
        return _find(re.compile(r"\bC-\d{4}\b"), *texts) or (from_results(req, "customer_id") if required else None)
    if prop == "site_id":
        return _find(re.compile(r"\bSITE-\d{4}-[A-Z]\b"), *texts) or (from_results(req, "site_id") if required else None)
    if prop in ("skill", "service", "metric", "status", "field", "reason_code", "country"):
        options = _options(desc)                                   # explicit option names: the whole request counts
        hit = next((o for o in options if _mentioned(o, clause)), None) or next((o for o in options if _mentioned(o, text)), None)
        if prop == "reason_code" and hit is None:
            for words, code in ((("no", "longer"), "no_longer_needed"), (("wrong",), "wrong_item"), (("damag",), "damaged_in_transit"),
                                (("defect",), "defective"), (("leak",), "defective")):
                if all(w in lower for w in words):
                    hit = code
                    break
        if prop == "country" and hit is None:
            m = re.search(r"\bto ([A-Z]{2})\b", text)
            hit = m.group(1) if m else None
        return hit
    if prop in FREE_TEXT:
        if prop == "query":
            return " ".join(content_words(clause)[:6]) or clause[:80]
        if prop == "name":
            m = re.search(r"runbook (?:for|named|on) ([a-z ]+)", lower) or re.search(r"\bthe ([a-z_ ]+?) policy", lower)
            return m.group(1).strip() if m else None
        if prop == "topic":
            return " ".join(content_words(clause)[:2]) or None
        if prop == "reference":
            m = re.search(r"reference ([A-Z0-9-]+)", text) or re.search(r"\b(?:SVC|SO|RMA)-\d+\b", text)
            return (m.group(1) if m and m.lastindex else m.group(0)) if m else ("support request" if required else None)
        if not required:
            return None
        if prop in ("reason", "justification"):
            m = re.search(r"\bbecause ([^.;?!]+)", text)
            return (m.group(1).strip().capitalize() if m else clause[:200])
        if prop in ("text", "body"):
            return f"Update from Kestrel support ({TODAY}): {clause[:240]}"
        if prop == "summary":
            return clause[:200] if clause == text else f"{clause[:160]} (request: {text[:160]})"
        if prop in ("title", "subject"):
            return clause[:70]
        return clause[:240]
    # generic: an ID whose format the argument's own description shows, or a value from earlier results
    for prefix in EG_PREFIX.findall(desc):
        found = _find(re.compile(rf"\b{re.escape(prefix)}[0-9A-Z][0-9A-Z-]*\b"), *texts, convo)
        if found:
            return found
    return from_results(req, prop) if required else None


def _is_identifier(prop: str, ps: dict) -> bool:
    return prop in FORMATS or prop.endswith("_id") or bool(EG_PREFIX.search(str(ps.get("description", ""))))


def _coerce(value: Any, kind: str) -> Any:
    try:
        if kind == "integer" and not isinstance(value, int):
            return int(float(value))
        if kind == "number" and not isinstance(value, (int, float)):
            return float(value)
        if kind == "array" and not isinstance(value, list):
            return [value]
        if kind == "string" and not isinstance(value, str):
            return str(value)
    except (TypeError, ValueError):
        return None
    return value


def fill_args(t: dict, req: MockRequest, text: str, clause: str) -> dict | None:
    """Fill a tool's arguments from the request; None when a required argument is not available (yet)."""
    schema = t.get("input_schema") or {}
    props = {k: (v if isinstance(v, dict) else {}) for k, v in (schema.get("properties") or {}).items()}
    required = set(schema.get("required") or [])
    args: dict[str, Any] = {}
    for prop, ps in props.items():
        value = _value(prop, ps, req, text, clause, prop in required)
        value = _coerce(value, ps.get("type", "string")) if value is not None else None
        if value is None:
            if prop in required:
                return None
            continue
        args[prop] = value
    if not any(_is_identifier(p, props[p]) for p in args):
        for prop, ps in props.items():                   # e.g. check_warranty(serial_number) for "is the unit covered?"
            if prop not in args and _is_identifier(prop, ps):
                value = _value(prop, ps, req, text, clause, False, wide=True)
                if value is not None:
                    args[prop] = _coerce(value, ps.get("type", "string"))
                    break
    return args


# ------------------------------------------------------------------------------------------ answers
def _short(v: Any, n: int = 150) -> str:
    text = ", ".join(f"{a}={b}" for a, b in v.items() if isinstance(b, (str, int, float))) if isinstance(v, dict) else str(v)
    return text if len(text) <= n else text[: n - 3] + "..."


def _asked(key: str, wanted: set[str]) -> bool:
    """Does a result field answer something the question asked ('roles' -> role, 'emails' -> email)?"""
    return any(min(4, len(p), len(w)) >= 3 and p[:min(4, len(p), len(w))] == w[:min(4, len(p), len(w))]
               for p in map(_stem, key.split("_")) for w in wanted)


def _item(item: dict, wanted: set[str]) -> str:
    """One list item: its first identifier plus the fields the question asked about (a model reads the question)."""
    scalars = [(k, v) for k, v in item.items() if isinstance(v, (str, int, float))]
    keep = scalars[:1] + [(k, v) for k, v in scalars[1:] if _asked(k, wanted)][:2]
    return " ".join(str(v) for _, v in keep)


def describe_result(c: ToolCall, question: str = "") -> str:
    args = ", ".join(f"{k}={v}" for k, v in c.input.items() if k not in FREE_TEXT)
    data = c.result_json()
    if c.is_error:
        err = (data or {}).get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else (c.result or "")
        return f"- {c.name}({args}) failed: {msg}"
    if not isinstance(data, dict):
        return f"- {c.name}({args}): {(c.result or '')[:160]}"
    wanted = {_stem(w) for w in _raw_tokens(question) if len(w) > 3}
    asked = sorted(data, key=lambda k: not _asked(k, wanted))                           # what the question asked first
    facts = []
    for k in asked:
        v = data[k]
        if k in c.input and not isinstance(v, (list, dict)):
            continue
        if isinstance(v, (str, int, float, bool)) and v not in ("", None):
            facts.append(f"{k} {_short(v)}")
        elif isinstance(v, list) and v and isinstance(v[0], dict) and len(v) <= 12:
            facts.append(f"{len(v)} {k}: " + "; ".join(_item(item, wanted) for item in v))
        elif isinstance(v, list) and v:
            facts.append(f"{len(v)} {k} (first: {_short(v[0], 110)})" if isinstance(v[0], dict) else f"{k} {', '.join(map(str, v[:4]))}")
        elif isinstance(v, dict) and v:
            facts.append(f"{k}: {_short(v)}")
        if len(facts) >= 5:
            break
    return f"- {c.name}({args}): " + "; ".join(facts)


def compose(calls: list[ToolCall], unmet: list[str], unchecked: list[str] | None = None, question: str = "") -> str:
    lines = [describe_result(c, question) for c in calls if c.name != "escalate_to_human"]
    esc = [c for c in calls if c.name == "escalate_to_human" and not c.is_error]
    if esc:
        d = esc[-1].result_json() or {}
        lines.append(f"- No tool I have covers: {'; '.join(unmet) or 'part of the request'}. I escalated it to the "
                     f"{d.get('queue')} queue as {d.get('escalation_id')} ({d.get('priority')}, {d.get('sla')}).")
    elif unmet:
        lines.append(f"- I could not do this with the tools available to me: {'; '.join(unmet)}.")
    if unchecked:
        lines.append(f"- I had no tool to check: {'; '.join(unchecked)}.")
    return "Here is what I found:\n" + "\n".join(lines) if lines else "I did not need any tool for that."


# ------------------------------------------------------------------------------------------ the planner
def _recover(req: MockRequest, calls: list[ToolCall], text: str) -> Reply | None:
    """A failed call whose error names another visible tool ('... use get_shipment_v2') -> retry with that tool."""
    visible = {t["name"]: t for t in visible_tools(req)}
    called = {c.name for c in calls}
    for c in calls:
        if not c.is_error:
            continue
        data = c.result_json()
        msg = json.dumps(data) if isinstance(data, dict) else (c.result or "")
        m = re.search(r"\buse ([a-z][a-z0-9_]+)", msg)
        if m and m.group(1) in visible and m.group(1) not in called:
            args = fill_args(visible[m.group(1)], req, text, text) or {}
            return use_tools(tool(m.group(1), **{**c.input, **args}), thinking="The error names the tool to use instead.")
    return None


def _same_call(c: ToolCall, name: str, args: dict) -> bool:
    """Already done in this request: same tool, same identifying arguments (free-text arguments may differ)."""
    return c.name == name and all(c.input.get(k) == v for k, v in args.items() if k not in FREE_TEXT)


def plan(req: MockRequest, *, escalate_unmet: bool = True, single: bool = False) -> Reply:
    """One tool per clause of the latest request, chosen among the tools the model can see.

    Search when nothing visible covers a clause (and a search tool is declared); call covering tools whose arguments
    are available; wait for a result when a later clause needs one; escalate (or admit) what nothing covers; answer
    when nothing is pending. Statements ('the carrier has not scanned it since Friday') are context: they add a call
    only when no other chosen tool covers them, and are never escalated on their own."""
    text, q_idx = segment(req)
    calls, searches, _ = since(req, q_idx)
    everything = custom_tools(req)
    search_tool = req.server_tools.get("tool_search")
    variant = "regex" if search_tool and "regex" in search_tool.get("type", "") else "bm25"
    just_searched = ends_with_search(req)
    searched = {query_of(s) for s in searches}
    parallel_ok = not (req.tool_choice or {}).get("disable_parallel_tool_use")

    recovery = _recover(req, calls, text)
    if recovery is not None:
        return recovery

    visible = visible_tools(req)
    by_name = {t["name"]: t for t in visible}
    decisions: list[tuple[str, list[str], str | None, dict | None]] = []
    for clause in ([text] if single else clauses_of(text)):
        terms = terms_of(clause)
        query = terms + [_stem(w) for w in id_words(clause, everything) if _stem(w) not in terms]
        ranked = [r[1] for r in rank(query, visible, question=is_question(clause))
                  if single or coverage(terms, by_name[r[1]]) >= COVER_MIN]
        named = [n for n in by_name if re.search(rf"\b{re.escape(n)}\b", clause)]    # "use get_shipment ...": do as told
        ranked = named + [n for n in ranked if n not in named]
        chosen, args = None, None
        for name in ranked[:3]:                              # the best covering tool whose arguments are available
            args = fill_args(by_name[name], req, text, clause)
            if args is not None:
                chosen = name
                break
        decisions.append((clause, ranked, chosen, args))
        if single and chosen:
            break

    # statements are context: drop them when a tool chosen for another clause (or already called) covers them
    acting = {d[2] for d in decisions if d[2] and not is_statement(d[0])} | {c.name for c in calls}
    decisions = [d for d in decisions if not (is_statement(d[0]) and any(
        n in by_name and coverage(terms_of(d[0]), by_name[n]) >= COVER_MIN for n in acting if n != d[2]))]

    pending: list[dict] = []
    for clause, ranked, chosen, args in decisions:
        if chosen is None or any(p["name"] == chosen for p in pending):
            continue
        if any(_same_call(c, chosen, args) for c in calls):
            continue                                         # done already
        pending.append(tool(chosen, **args))
    to_search: list[str] = []
    unmet: list[str] = []
    unchecked: list[str] = []
    for clause, ranked, chosen, args in decisions:
        if chosen is not None:
            continue
        if ranked and pending:
            continue                                         # covered, but it needs a result that has not arrived yet
        if search_tool and not just_searched and not single:
            to_search.append(clause)
        else:
            (unchecked if is_statement(clause) else unmet).append(clause)

    if to_search:
        query = search_query(to_search, everything, variant)
        if query not in searched:
            limit = None if len(to_search) == 1 else min(8, 3 * len(to_search))
            return Reply(content=[search_tools(query, limit=limit)],
                         thinking_summary=f"No visible tool covers '{to_search[0][:50]}'; search the catalog.")
        for clause in to_search:                             # searched with these words already: nothing more to find
            (unchecked if is_statement(clause) else unmet).append(clause)
    if pending:
        if not parallel_ok:
            pending = pending[:1]
        preface = None if calls else ("Let me check." if len(pending) == 1 else "Let me look those up.")
        return use_tools(*pending, preface=preface, thinking="Use the most specific tool for each part of the request.")
    if unmet and escalate_unmet and "escalate_to_human" in by_name and not any(c.name == "escalate_to_human" for c in calls):
        summary = f"Needs a person: {'; '.join(unmet)[:300]}"
        priority = "P1" if any(w in text.lower() for w in SAFETY_WORDS) else "P3"
        queue = infer_queue(" ".join(unmet))
        if queue == "account_management":                   # the clause alone names no domain: read the whole request
            queue = infer_queue(text)
        return use_tools(tool("escalate_to_human", queue=queue, priority=priority, summary=summary),
                         thinking="No tool I can see covers part of the request: hand it to the right queue.")
    return say(compose(calls, unmet, unchecked, text), complexity=0.45, thinking="Compose the answer from the tool results only.")


# ------------------------------------------------------------------------------------------ scenarios
@scenario("adv.day2.tool_search", match=lambda r: MARK_SEARCH in r.system_text, priority=10)
def tool_search_only(req: MockRequest) -> Reply:
    """Lab 02: write one search from the request, then report what the search found (no tool is called)."""
    text, q_idx = segment(req)
    _, searches, results = since(req, q_idx)
    search_tool = req.server_tools.get("tool_search")
    variant = "regex" if search_tool and "regex" in search_tool.get("type", "") else "bm25"
    verbatim = re.search(r"`([^`]+)`", text)
    if not searches:
        query = verbatim.group(1) if verbatim else search_query([text], custom_tools(req), variant)
        return Reply(content=[search_tools(query)], thinking_summary="Search the catalog before choosing a tool.")
    last = results[-1] if results else {}
    if last.get("type") == "tool_search_tool_result_error":
        if len(searches) == 1:
            fixed = re.sub(r"[()\[\]{}]", "", query_of(searches[0])) or "shipment"
            return Reply(content=[search_tools(fixed)], thinking_summary="The pattern was invalid; retry without the bracket.")
        return say(f"The search failed ({last.get('error_code')}: {last.get('error_message')}).", complexity=0.2)
    names = [r.get("tool_name") for r in last.get("tool_references") or []]
    if not names:
        return say("The search matched no tool in the catalog; I would rephrase and search again.", complexity=0.2)
    return say(f"Found {len(names)} candidate tool(s): {', '.join(names)}. I would use {names[0]} next.", complexity=0.2)


@scenario("adv.day2.wide_agent", match=lambda r: MARK_WIDE in r.system_text, priority=10)
def wide_agent(req: MockRequest) -> Reply:
    return plan(req, escalate_unmet=True)


@scenario("adv.day2.phases", match=lambda r: MARK_PHASES in r.system_text, priority=10)
def phased_agent(req: MockRequest) -> Reply:
    return plan(req, escalate_unmet=False)


@scenario("adv.day2.selection", match=lambda r: MARK_SELECT in r.system_text, priority=10)
def selection(req: MockRequest) -> Reply:
    if req.is_tool_result_turn:
        text, q_idx = segment(req)
        calls, _, _ = since(req, q_idx)
        return say(compose(calls, [], [], text), complexity=0.3)
    return plan(req, escalate_unmet=False, single=True)


# ------------------------------------------------------------------------------------------ lab 05: recall triage
TRIAGE_CELL = '''import asyncio, json
units = {units}
pumps = [s for s, u in units.items() if u["sku"].startswith("KP")]
boards = [s for s, u in units.items() if u["sku"].startswith("KC")]
# every look-up at once: the container pauses once and hands all of them to the client
raw = await asyncio.gather(*[get_pump_telemetry({{"serial_number": s}}) for s in pumps],
                           *[get_fault_codes({{"serial_number": s, "days": 7}}) for s in boards])
tele = {{s: json.loads(r) for s, r in zip(pumps, raw[:len(pumps)])}}
faults = {{s: json.loads(r) for s, r in zip(boards, raw[len(pumps):])}}
hot = [s for s in pumps if tele[s]["seal_temp_c"] > {threshold}]
trend = {{s: json.loads(r)["assessment"] for s, r in
         zip(hot, await asyncio.gather(*[get_vibration_trend({{"serial_number": s, "days": 7}}) for s in hot]))}}
triage = []
for s, u in units.items():
    if s in tele:
        t = tele[s]
        is_hot = t["seal_temp_c"] > {threshold}
        p = "P1" if is_hot and (trend.get(s) == "rising" or u["risk"] == "safety") else "P2" if is_hot else "P3"
        triage.append({{"serial": s, "sku": u["sku"], "region": u["region"], "signal": f"seal {{t['seal_temp_c']}} C",
                       "trend": trend.get(s, "-"), "priority": p}})
    else:
        n = sum(1 for c in faults[s]["codes"] if c["code"] == "F17")
        triage.append({{"serial": s, "sku": u["sku"], "region": u["region"], "signal": f"{{n}} x F17 in 7 days",
                       "trend": "-", "priority": "P2" if n >= {faults} else "P3"}})
triage.sort(key=lambda r: (r["priority"], r["serial"]))
print(json.dumps({{"units": len(triage), "by_priority": {{p: sum(r["priority"] == p for r in triage) for p in ("P1", "P2", "P3")}},
                  "triage": [{{k: r[k] for k in ("serial", "priority", "signal", "trend")}} for r in triage]}}))
'''

KITS_CELL = '''import asyncio, json
kit_for = {kits}
warehouse_for = {warehouses}
need = {{}}
for s, u in units.items():                      # `units` is still in the container from the previous cell
    key = (warehouse_for[u["region"]], kit_for[u["sku"]])
    need[key] = need.get(key, 0) + 1
stock_raw = await asyncio.gather(*[get_stock({{"sku": k}}) for k in sorted(set(kit_for.values()))])
stock = {{}}
for r in stock_raw:
    d = json.loads(r)
    for row in d["stock"]:
        stock[(row["warehouse"], d["sku"])] = row["available"]
rows = [{{"warehouse": w, "kit": k, "units": n, "in_stock": stock.get((w, k), 0), "short": max(0, n - stock.get((w, k), 0))}}
        for (w, k), n in sorted(need.items())]
print(json.dumps(rows))
'''


def _units_from(text: str) -> dict[str, dict]:
    """The unit table in the request: one line per unit with serial, SKU, region and risk class."""
    units = {}
    for line in text.splitlines():
        s, k, r = SERIAL.search(line), SKU.search(line), REGION.search(line)
        if s and k and r:
            risk = next((w for w in ("safety", "production", "standard") if w in line.lower()), "standard")
            units[s.group(0)] = {"sku": k.group(0), "region": r.group(0), "risk": risk}
    return units


def _threshold(text: str) -> int:
    m = re.search(r"(?:above|over|>)\s*(\d{2,3})\s*(?:°|deg)?\s*C\b", text, re.I)
    return int(m.group(1)) if m else 70


def _fault_limit(text: str) -> int:
    m = re.search(r"(\d+)\s*(?:\+|or more)\s*F17", text)
    return int(m.group(1)) if m else 5


def _mapping(text: str, pattern: str) -> dict[str, str]:
    return {a: b for a, b in re.findall(pattern, text)}


def _triage_rows(units: dict, tele: dict, faults: dict, trends: dict, threshold: int, limit: int) -> list[dict]:
    rows = []
    for s, u in units.items():
        if s in tele:
            t = tele[s]
            hot = t.get("seal_temp_c", 0) > threshold
            p = "P1" if hot and (trends.get(s) == "rising" or u["risk"] == "safety") else "P2" if hot else "P3"
            rows.append({"serial": s, "priority": p, "signal": f"seal {t.get('seal_temp_c')} C", "trend": trends.get(s, "-")})
        elif s in faults:
            n = sum(1 for c in faults[s].get("codes", []) if c.get("code") == "F17")
            rows.append({"serial": s, "priority": "P2" if n >= limit else "P3", "signal": f"{n} x F17 in 7 days", "trend": "-"})
    return sorted(rows, key=lambda r: (r["priority"], r["serial"]))


def _say_triage(rows: list[dict], threshold: int) -> Reply:
    counts = {p: sum(r["priority"] == p for r in rows) for p in ("P1", "P2", "P3")}
    lines = [f"  {r['priority']}  {r['serial']:<16} {r['signal']:<20} trend {r['trend']}" for r in rows]
    return say(f"Triage of {len(rows)} units (seal-chamber limit {threshold} C): {counts['P1']} P1, {counts['P2']} P2, "
               f"{counts['P3']} P3.\n" + "\n".join(lines), complexity=0.45)


def _say_kits(rows: list[dict]) -> Reply:
    short = [r for r in rows if r["short"]]
    lines = [f"  {r['warehouse']:<8} {r['kit']:<9} need {r['units']:>2}  in stock {r['in_stock']:>2}"
             + (f"  SHORT {r['short']}" if r["short"] else "") for r in rows]
    verdict = ("Short: " + ", ".join(f"{r['short']} x {r['kit']} at {r['warehouse']}" for r in short)) if short else "No shortages."
    return say("Remedy kits needed for all units, by warehouse:\n" + "\n".join(lines) + "\n" + verdict, complexity=0.4)


@scenario("adv.day2.ptc", match=lambda r: MARK_PTC in r.system_text, priority=10)
def recall_triage(req: MockRequest) -> Reply:
    text, q_idx = segment(req)
    units = _units_from(req.first_user_text)
    threshold, limit = _threshold(req.first_user_text), _fault_limit(req.first_user_text)
    kits_turn = "kit" in text.lower() and "stock" in text.lower()
    kits = _mapping(text, r"\b((?:MS-\d{3}-R|KC-2-PSB))\s+for\s+(?:the\s+)?(KP-\d{3}-S|KC-2)\b")
    kit_for = {sku: kit for kit, sku in kits.items()}
    warehouse_for = {r: w for r, w in re.findall(r"\b(US-EAST|US-WEST|EU|APAC)\s*(?:->|=>|:)\s*(WH-[A-Z]+)", text)}
    code_path = req.server_tools.get("code_execution") is not None and bool(req.code_callable_tools)
    parallel_ok = not (req.tool_choice or {}).get("disable_parallel_tool_use")

    if code_path:
        if req.completed_code is not None:
            out = req.completed_code["content"]
            if out.get("return_code") != 0:
                return say(f"The script failed: {out.get('stderr', '')[:300]}", complexity=0.3)
            data = json.loads(out["stdout"].strip().splitlines()[-1])
            if isinstance(data, dict) and "triage" in data:
                return _say_triage(data["triage"], threshold)
            return _say_kits(data)
        calls, _, _ = since(req, q_idx)
        if any(b.get("type") == "code_execution_tool_result" for m in req.messages[q_idx + 1:] if m.get("role") == "assistant"
               for b in _blocks(m.get("content"))):
            return say("Done - see the table above.", complexity=0.1)
        if kits_turn:
            return Reply(content=[run_code(KITS_CELL.format(kits=json.dumps(kit_for), warehouses=json.dumps(warehouse_for)))],
                         thinking_summary="The unit table is still in the container; count kits there and check stock.")
        return Reply(content=[run_code(TRIAGE_CELL.format(units=json.dumps(units), threshold=threshold, faults=limit))],
                     thinking_summary="Fan the look-ups out in code; only the triage table needs to come back.")

    # direct tool use: every result comes back into the context
    calls, _, _ = since(req, q_idx)
    everything = req.tool_calls
    if kits_turn:
        stock_calls = [c for c in calls if c.name == "get_stock"]
        wanted = sorted(set(kit_for.values()))
        missing = [k for k in wanted if not any(c.input.get("sku") == k for c in stock_calls)]
        if missing:
            batch = missing if parallel_ok else missing[:1]
            return use_tools(*[tool("get_stock", sku=k) for k in batch], preface="Checking recall-kit stock.")
        stock = {}
        for c in stock_calls:
            d = c.result_json() or {}
            for row in d.get("stock", []):
                stock[(row["warehouse"], d.get("sku"))] = row["available"]
        need: dict[tuple[str, str], int] = {}
        for s, u in units.items():
            key = (warehouse_for.get(u["region"], "?"), kit_for.get(u["sku"], "?"))
            need[key] = need.get(key, 0) + 1
        rows = [{"warehouse": w, "kit": k, "units": n, "in_stock": stock.get((w, k), 0), "short": max(0, n - stock.get((w, k), 0))}
                for (w, k), n in sorted(need.items())]
        return _say_kits(rows)
    tele = {c.input.get("serial_number"): c.result_json() or {} for c in everything if c.name == "get_pump_telemetry" and not c.is_error}
    faults = {c.input.get("serial_number"): c.result_json() or {} for c in everything if c.name == "get_fault_codes" and not c.is_error}
    todo = [tool("get_pump_telemetry", serial_number=s) for s, u in units.items() if u["sku"].startswith("KP") and s not in tele]
    todo += [tool("get_fault_codes", serial_number=s, days=7) for s, u in units.items() if u["sku"].startswith("KC") and s not in faults]
    if todo:
        return use_tools(*(todo if parallel_ok else todo[:1]), preface=None if calls else "Pulling the readings unit by unit.")
    trends = {c.input.get("serial_number"): (c.result_json() or {}).get("assessment") for c in everything
              if c.name == "get_vibration_trend" and not c.is_error}
    hot = [s for s, t in tele.items() if t.get("seal_temp_c", 0) > threshold]
    todo = [tool("get_vibration_trend", serial_number=s, days=7) for s in hot if s not in trends]
    if todo:
        return use_tools(*(todo if parallel_ok else todo[:1]))
    return _say_triage(_triage_rows(units, tele, faults, trends, threshold, limit), threshold)


# ------------------------------------------------------------------------------------------ lab 06: streaming
def _facts(text: str) -> dict[str, dict]:
    """Per-lot facts from the request: 'LOT: hazard ... | remedy ... | interim ...' lines."""
    out = {}
    for line in text.splitlines():
        m = LOT.search(line)
        if not m:
            continue
        parts = {k.strip().lower(): v.strip() for k, v in re.findall(r"(hazard|remedy|interim)\s*:\s*([^|]+)", line, re.I)}
        if parts:
            out[m.group(0)] = parts
    return out


@scenario("adv.day2.stream", match=lambda r: MARK_STREAM in r.system_text, priority=10)
def bulletin_writer(req: MockRequest) -> Reply:
    text, q_idx = segment(req)
    calls, _, _ = since(req, q_idx)
    drafts = [c for c in calls if c.name == "draft_bulletin"]
    limit = None
    if drafts and not drafts[-1].is_error:
        d = drafts[-1].result_json() or {}
        return say(f"Bulletin {d.get('bulletin_id')} is saved as draft {d.get('draft_id')} ({d.get('words')} words, "
                   f"{d.get('actions')} actions) and is waiting for review by {d.get('review_by')}.", complexity=0.3)
    if drafts and len(drafts) >= 3:
        return say("The draft was rejected three times; I stopped retrying. Last error: " + (drafts[-1].result or "")[:200],
                   complexity=0.2)
    if drafts:                                    # the client rejected the input: read the error and send it again
        m = re.search(r"at most ([\d,]+) characters", drafts[-1].result or "")
        limit = int(m.group(1).replace(",", "")) if m else None
    facts = _facts(text)
    lots = list(facts) or LOT.findall(text)
    m = re.search(r"\bTSB-\d{4}-\d{2}\b", text)
    bulletin_id = m.group(0) if m else "TSB-2026-09"
    audience = next((a for a in ("installers", "distributors", "operators") if a in text.lower()), "operators")
    paragraphs = [f"Kestrel Pumps & Controls - Technical Safety Bulletin {bulletin_id}, issued {TODAY}. It applies to units built "
                  f"with {' and '.join(lots) or 'the affected lots'} and supersedes any verbal guidance given by field staff."]
    for lot, f in facts.items():
        paragraphs.append(f"Lot {lot}. Hazard: {f.get('hazard', 'see the recall notice')} Remedy: {f.get('remedy', 'a field visit')} "
                          f"Until the remedy is applied: {f.get('interim', 'follow the interim measure in the recall notice')}")
    paragraphs.append("How to tell whether a unit is affected: read the serial number on the nameplate and ask Kestrel support "
                      "for its build record; units outside these lots are not affected. The remedy, parts and labour are free "
                      "of charge under recall RC-2026-03.")
    paragraphs.append("Background: the elastomer batch and the capacitor batch were traced by lot. Kestrel has stopped shipping "
                      "remaining stock from both lots, is inspecting inventory, and will confirm each completed remedy in writing. "
                      "We apologise for the disruption.")
    body = "\n\n".join(paragraphs)
    if limit and len(body) > limit:
        body = "\n\n".join(paragraphs[:-1])[:limit - 1].rsplit(". ", 1)[0] + "."
    actions = ["Identify affected serial numbers from the nameplate and the build record.",
               "Apply the interim measure for the unit's lot today.",
               "Do not run affected pumps unattended on hazardous duty.",
               "Book the remedy visit with Kestrel field service.",
               "File this bulletin with the site's maintenance records."]
    return use_tools(tool("draft_bulletin", bulletin_id=bulletin_id, title=f"{bulletin_id}: field replacement campaign RC-2026-03",
                          audience=audience, lots=lots, body=body, actions=actions),
                     preface=None if drafts else "Drafting the bulletin from the recall facts.",
                     thinking="Write the full text into the tool input; the reviewer edits the draft, not the chat.",
                     complexity=0.6)
