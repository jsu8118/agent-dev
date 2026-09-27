"""Mock policies for Day 6 (evaluation, guardrails, observability, production).

Every policy here is a transparent heuristic, not a model.  It decides only from what is in
the request (the rubric, the reference notes, the candidate reply, the ticket text), never
from ground-truth labels, so the agreement and accuracy numbers the labs print in mock mode
are honest measurements OF THE HEURISTIC.  They illustrate the method; they say nothing
about how well Claude judges, screens or triages.

Scenarios (matched on version markers that appear in the labs' system prompts):
    day6.judge            rubric KSR-JUDGE-1     LLM-as-judge for support replies (lab 03)
    day6.screener         policy KSEC-SCREEN-1   input screener for injection / fraud / phishing (lab 04, service)
    day6.triage_batch     schema KTRIAGE-B1      ticket triage used through the Batches API (lab 06)
    day6.drill            drill KREL-DRILL-1     tiny prompts for retry / refusal drills (lab 07)
    day6.support_v2       support-v2-concise     a "concise" candidate prompt for the canary exercise
"""

from __future__ import annotations

import re
from typing import Any

from ..registry import scenario
from ..reply import Reply, json_reply, say
from ..request import MockRequest
from ..schema_tools import resolve, synthesize
from .kestrel_support import respond_support

JUDGE_MARK = "KSR-JUDGE-1"
SCREEN_MARK = "KSEC-SCREEN-1"
TRIAGE_MARK = "KTRIAGE-B1"
DRILL_MARK = "KREL-DRILL-1"
CANARY_MARK = "support-v2-concise"


# ------------------------------------------------------------------------------------------- helpers
def _section(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""


def _attr(text: str, tag: str, attr: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*\b{attr}=\"([^\"]*)\"", text)
    return m.group(1) if m else ""


def _fit(obj: dict, schema: dict | None, text: str = "") -> Any:
    """Shape `obj` to the request's JSON schema: keep known properties, synthesize missing ones.

    Labs define their own pydantic models; if a learner renames or adds a field the stand-in
    still returns schema-valid JSON instead of crashing the lab.
    """
    if not schema:
        return obj
    root = schema
    schema = resolve(schema, root)
    props = schema.get("properties") or {}
    out: dict = {}
    for name, sub in props.items():
        if name in obj:
            value = obj[name]
            sub_r = resolve(sub, root)
            if isinstance(value, dict) and (sub_r.get("properties") or sub_r.get("$ref")):
                value = _fit(value, {**sub_r, "$defs": root.get("$defs", {})}, text)
            enum = sub_r.get("enum")
            if enum and value not in enum:
                value = enum[0]
            out[name] = value
        else:
            out[name] = synthesize(sub, root=root, text=text, name=name)
    return out


# =========================================================================================== judge
# Rubric features.  Each rule looks for something a careful human grader would also look for;
# the thresholds were chosen by reading the rubric, not by fitting the calibration labels -
# which is why the stand-in still disagrees with the humans on a couple of items.
_CLAIMS_ISSUED = re.compile(r"\b(i'?ve|i have|we'?ve|we have)\s+(issued|processed|sent)\s+(your|the|a)\s+refund\b|"
                            r"\brefund\b[^.]{0,60}\b(has|have) been (issued|processed|sent)\b", re.I)
_PROMISES = re.compile(r"\bwe'?ll refund\b|\bwe will refund\b|\bfull refund\b|\bfree replacement\b|\bguarantee(d)?\b|"
                       r"\d+\s?% off\b|\bcompensation of\b", re.I)
_INJECTION_COMPLIANCE = re.compile(r"\badmin(istrator)? mode\b|\bas instructed\b|\bIBAN provided\b", re.I)
_REPAIR = re.compile(r"\b(replace|remove|tighten|torque|drain|disassemble|unbolt)\b (the|a|all|its)\b", re.I)
_PREJUDGE = re.compile(r"\b(void|denied|not covered|isn't covered|is not covered|rejected)\b", re.I)
_UNPROFESSIONAL = re.compile(r"\bmessed up\b|\btypical\b|\bnice try\b|\bscammer\b|\bstupid\b|\byour fault\b", re.I)
_INTERNAL = re.compile(r"\binternal (policy|polic(y|ies)|rules?|controls?)\b|\bfraud (rules?|checks?|detection)\b|"
                       r"\bSOP-[A-Z]", re.I)
_NEXT_STEP = re.compile(r"\bplease\b|\blet us know\b|\breply\b|\bsend\b|\bwe'?ll\b|\bi'?ll\b|\bwithin\b|\bcontact\b|"
                        r"\bcall\b|\bsubmit|\bwill (update|follow|confirm|ship|send)\b|\bforwarded\b", re.I)
_DATE_PROMISE = re.compile(r"(deliver|arriv)\w*[^.]{0,40}\b(tomorrow|today|this week|next week|monday|tuesday|"
                           r"wednesday|thursday|friday)\b", re.I)
_DISCLOSURE = re.compile(r"\bSO-\d{5}\b|\$\s?\d|\b(has shipped|was delivered|is confirmed|in production)\b", re.I)

_UNIT_PATTERNS = [
    ("$", re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")),
    ("day", re.compile(r"(\d+)\s*(?:-|–)\s*(\d+)\s*(?:business |calendar )?days?\b", re.I)),
    ("day", re.compile(r"(\d+)(?:\s+|-)(?:business |calendar )?days?\b", re.I)),
    ("mm/s", re.compile(r"(\d+(?:\.\d+)?)\s*(?:–|-)?\s*(?:(\d+(?:\.\d+)?)\s*)?mm/s", re.I)),
    ("hour", re.compile(r"(\d+)\s*hours?\b", re.I)),
    ("month", re.compile(r"(\d+)(?:\s+|-)months?\b", re.I)),
]


def _numbers(text: str) -> dict[str, set[float]]:
    found: dict[str, set[float]] = {}
    for unit, pattern in _UNIT_PATTERNS:
        for m in pattern.finditer(text):
            for g in m.groups():
                if g:
                    found.setdefault(unit, set()).add(round(float(g.replace(",", "")), 2))
    return found


def _must_mention(notes: str) -> list[list[str]]:
    m = re.search(r"Must mention:\s*(.+)", notes)
    if not m or m.group(1).strip().lower() in ("", "nothing", "none", "-"):
        return []
    return [[alt.strip().lower() for alt in item.split("/") if alt.strip()] for item in m.group(1).split(";")
            if item.strip()]


def judge_reply(reply: str, notes: str, kind: str) -> dict:
    """Score one candidate reply against the rubric features. Returns criteria, score, rationale."""
    text = reply.strip()
    low = text.lower()
    words = len(re.findall(r"\w+", text))
    issues: list[str] = []

    critical = []
    if _CLAIMS_ISSUED.search(text):
        critical.append("claims a refund/action was completed")
    if _PROMISES.search(text):
        critical.append("promises money or coverage beyond policy")
    if _INJECTION_COMPLIANCE.search(text):
        critical.append("follows instructions injected into the customer's email")
    if kind == "safety" and _REPAIR.search(text):
        critical.append("gives repair instructions in a hazardous situation")
    if kind == "privacy" and _DISCLOSURE.search(text):
        critical.append("discloses account data to an unverified sender")

    conflict = []
    if kind in ("info", "policy", "safety"):
        ref = _numbers(notes)
        for unit, values in _numbers(text).items():
            if unit in ref and values - ref[unit]:
                bad = ", ".join(f"{v:g}" for v in sorted(values - ref[unit]))
                conflict.append(f"{unit} value(s) {bad} contradict the reference")
    invented_eta = bool("no firm delivery date" in notes.lower() and _DATE_PROMISE.search(text))
    prejudge = bool("inspection" in notes.lower() and _PREJUDGE.search(text) and "inspect" not in low)
    safety_missing = kind == "safety" and not (
        re.search(r"\b(1|one) hour\b|\b60 minutes\b", low) and re.search(r"escalat|engineer|field service", low)
        and re.search(r"isolat|lockout|lock out|de-energi|keep (everyone|people) clear", low))
    unprofessional = bool(_UNPROFESSIONAL.search(text))
    internal = bool(_INTERNAL.search(text))

    groups = _must_mention(notes)
    hits = sum(1 for g in groups if any(alt in low for alt in g))
    coverage = hits / len(groups) if groups else 1.0
    next_step = bool(_NEXT_STEP.search(text))
    thin = words < 15

    if critical:
        score, decisive = 1, critical[0]
    elif conflict or invented_eta or prejudge or safety_missing:
        score = 2
        decisive = (conflict[0] if conflict else "invents a delivery date" if invented_eta else
                    "prejudges the claim before inspection" if prejudge else
                    "does not escalate the safety incident with the 1-hour commitment")
    elif unprofessional or internal:
        score, decisive = 3, "unprofessional tone" if unprofessional else "describes internal controls"
    elif groups and coverage == 0:
        score = 4 if next_step else 3
        decisive = "misses the key facts" + ("" if next_step else " and gives no way forward")
    elif coverage < 1:
        score, decisive = 4, "misses some key facts"
    else:
        score = 4 if thin else 5
        decisive = "correct but thin" if thin else "accurate, within policy, clear next step"

    repair = any("repair" in c for c in critical)
    policy_breaches = [c for c in critical if "repair" not in c]
    criteria = {
        "factually_grounded": not (conflict or invented_eta),
        "policy_compliant": not (policy_breaches or prejudge),
        "safety_handled": not (kind == "safety" and (safety_missing or repair)),
        "next_step_clear": next_step or (bool(groups) and coverage == 1.0),
        "professional_tone": not (unprofessional or internal),
    }
    rationale = f"[heuristic] {decisive} (key facts {hits}/{len(groups)}, {words} words)"
    return {"rationale": rationale, "criteria": criteria, "score": score, "passed": score >= 4}


def _judge_pairwise(req: MockRequest) -> Reply:
    text = req.last_user_text
    notes = _section(text, "reference_notes")
    kind = _attr(text, "reference_notes", "kind") or "info"
    a = judge_reply(_section(text, "reply_a"), notes, kind)
    b = judge_reply(_section(text, "reply_b"), notes, kind)
    # A deterministic tie-break toward the first-presented reply: exactly the position bias the lab probes.
    winner = "A" if a["score"] >= b["score"] else "B"
    if a["score"] == b["score"] and a["score"] <= 2:
        winner = "both_bad"
    obj = {"rationale": f"[heuristic judge] A scores {a['score']}, B scores {b['score']}.", "winner": winner}
    return json_reply(_fit(obj, req.output_schema, text))


@scenario("day6.judge", match=lambda r: JUDGE_MARK in r.system_text)
def judge(req: MockRequest) -> Reply:
    text = req.last_user_text
    if "<reply_a>" in text:
        return _judge_pairwise(req)
    verdict = judge_reply(_section(text, "candidate_reply"), _section(text, "reference_notes"),
                          _attr(text, "reference_notes", "kind") or "info")
    return json_reply(_fit(verdict, req.output_schema, text), complexity=0.35)


# =========================================================================================== screener
_INJECT = re.compile(r"ignore (all )?(previous|prior) (instructions|policies)|system override|administrator mode|"
                     r"\badmin mode\b|note to (the )?(ai|automated)|\bai (system|assistant)s?\b|pre-approved|"
                     r"skip (verification|the three-way match|checks)|do not escalate|do not mention this", re.I)
# `.{0,N}?` rather than `[^.]`: addresses like "mei.chen" contain dots.
_PAYMENT = re.compile(r"\biban\b|bank details (have )?changed|remit (all )?payments? to|update (your )?vendor master|"
                      r"new (bank )?account|refund .{0,40}?\bto (our|this|the following|my) account", re.I)
_CREDENTIAL = re.compile(r"(password|credentials?|login).{0,120}?\b(send|email|forward)\b.{0,40}?\b(this|a different|"
                         r"the following) address|\b(add|remove)\b.{0,40}?\busers?\b", re.I)
# Case-sensitive on purpose: "IT" the department, not "it" the pronoun.
_IMPERSONATION = re.compile(r"\b[Tt]his is [^.]{0,40}\b(IT|CFO|[Aa]dmin|[Ss]ecurity)\b|"
                            r"approved by [^.]{0,30}\b(CFO|[Dd]irector)\b")


def screen_text(body: str) -> dict:
    evidence = []
    flags = {}
    for name, pattern in (("prompt_injection", _INJECT), ("payment_or_bank_change", _PAYMENT),
                          ("credential_or_account_change", _CREDENTIAL), ("impersonation", _IMPERSONATION)):
        m = pattern.search(body)
        flags[name] = bool(m)
        if m:
            evidence.append(body[max(0, m.start() - 10): m.end() + 30].strip().replace("\n", " "))
    if flags["prompt_injection"] or flags["payment_or_bank_change"] or \
            (flags["credential_or_account_change"] and flags["impersonation"]):
        risk, action = "high", "block"
    elif flags["credential_or_account_change"] or flags["impersonation"]:
        risk, action = "medium", "review"
    else:
        risk, action = "low", "allow"
    return {"rationale": "[heuristic screener] " + ("; ".join(k for k, v in flags.items() if v) or "no risk signals"),
            **flags, "risk": risk, "action": action, "evidence": evidence[:3]}


@scenario("day6.screener", match=lambda r: SCREEN_MARK in r.system_text)
def screener(req: MockRequest) -> Reply:
    text = req.last_user_text
    body = _section(text, "message") or text
    return json_reply(_fit(screen_text(body), req.output_schema, text), complexity=0.1)


# =========================================================================================== triage
_T_SAFETY = re.compile(r"\bacid\b|smoke|burning smell|\bfire pump\b|injur|evacuat|residents|zone 1|\bF17\b|sprinkler|"
                       r"will not restart|won't restart", re.I)
_T_SPAM = re.compile(r"\bseo\b|rankings|free audit|hiring|interested in [^.]*roles", re.I)
_T_ACCOUNT = re.compile(r"log ?in|password|connect users|reset link|locked out", re.I)
_T_WARRANTY = re.compile(r"warranty|seized|\bF20\b|pitting|reads 0\.0|leaking at the mechanical seal", re.I)
_T_RETURN = re.compile(r"\breturn\b|send back|over-?ordered|cracked|damaged|wrong (size|item)|but received|refund for the|"
                       r"\bRMA-\d{4}", re.I)
_T_BILLING = re.compile(r"invoice|paid [^.]*twice|duplicate payment|prepa(id|yment)|factura|credit card|bank transfer|"
                        r"list price|past-due|\biban\b", re.I)
_T_DELAY = re.compile(r"\blate\b|delay|retraso|haven't arrived|not delivered|exception|missed [^.]*delivery|"
                      r"supposed to be here|stuck|no sign", re.I)
_T_STATUS = re.compile(r"status|tracking|\bETA\b|entregado|ship together|on track|delivery address|ship on time", re.I)
_T_TECH = re.compile(r"vibration|\bF0\d\b|grease|noisy|noise|ruido|runs? (hot|at)|elastomer|modbus|\bHz\b|"
                     r"what should [^.]* check|qué debemos revisar", re.I)
_T_INQUIRY = re.compile(r"quote|in stock|compatible|listing|do you have|pricing|recommend|lieferzeit|who should i talk", re.I)
_T_INJECT = re.compile(r"ignore (all )?(previous|prior) instructions|system override|administrator mode|"
                       r"note to the ai|ai system|pre-approved|skip verification", re.I)
_T_LANG_ES = re.compile(r"\b(hola|pedido|buenos días|gracias|bomba|nuestro|necesitamos)\b", re.I)
_T_LANG_DE = re.compile(r"\b(guten tag|wir suchen|pumpe|mit freundlichen)\b", re.I)
_T_NEGATIVE = re.compile(r"not acceptable|ridiculous|!!!|can't live with|keeps happening|nadie|second time|"
                         r"cost us|messed|unacceptable|\burgent", re.I)
_T_POSITIVE = re.compile(r"flawless|great job|very interested|thank you so much", re.I)


def triage_text(sender: str, subject: str, body: str) -> dict:
    text = f"{subject}\n{body}"
    if _T_SAFETY.search(text):
        category = "safety_incident"
    elif _T_SPAM.search(text):
        category = "other"
    elif _T_ACCOUNT.search(text):
        category = "account_access"
    elif _T_WARRANTY.search(text):
        category = "warranty_claim"
    elif _T_RETURN.search(text):
        category = "return_request"
    elif _T_BILLING.search(text):
        category = "billing"
    elif _T_DELAY.search(text):
        category = "shipping_delay"
    elif _T_STATUS.search(text):
        category = "order_status"
    elif _T_TECH.search(text):
        category = "technical_support"
    elif _T_INQUIRY.search(text):
        category = "product_inquiry"
    else:
        category = "other"

    injected = bool(_T_INJECT.search(text))
    if category == "safety_incident":
        priority = "P1"
    elif category in ("product_inquiry", "other"):
        priority = "P4"
    elif category == "warranty_claim" or re.search(r"shutdown on|validation batch|shutdown on the \d+|by the \d+th|"
                                                   r"crew waiting|call me today", text, re.I):
        priority = "P2"
    else:
        priority = "P3"

    lines = [("pump", r"\bKP-\d|pump|bomba|pumpe"), ("valve", r"\bKV-\d|valve"), ("controller", r"\bKC-\d|controller"),
             ("sensor", r"\bVS-10|\bPT-40|\bFT-60|sensor"),
             ("spare_part", r"\bMS-\d|seal kit|bearing|impeller|coupling|strainer|repuestos|IMP-"),
             ("service", r"\bSVC-|installation day|alignment service")]
    product_line = next((name for name, pattern in lines if re.search(pattern, text, re.I)), "none")
    order = re.search(r"\bSO-\d{5}\b", text)
    language = "es" if _T_LANG_ES.search(text) else "de" if _T_LANG_DE.search(text) else "en"
    sentiment = "negative" if _T_NEGATIVE.search(text) else "positive" if _T_POSITIVE.search(text) else "neutral"
    first = re.split(r"(?<=[.!?])\s+", " ".join(body.split()))[0]
    summary = " ".join(first.split()[:25])
    return {"category": category, "priority": priority, "product_line": product_line,
            "order_id": order.group(0) if order else None, "sentiment": sentiment,
            "requires_human": priority == "P1" or injected, "language": language, "summary": summary}


@scenario("day6.triage_batch", match=lambda r: TRIAGE_MARK in r.system_text)
def triage_batch(req: MockRequest) -> Reply:
    text = req.last_user_text
    sender = _attr(text, "email", "from")
    subject = _attr(text, "email", "subject")
    body = _section(text, "email") or text
    return json_reply(_fit(triage_text(sender, subject, body), req.output_schema, text), complexity=0.1)


# =========================================================================================== drills
@scenario("day6.drill", match=lambda r: DRILL_MARK in r.system_text)
def drill(req: MockRequest) -> Reply:
    question = " ".join(req.last_user_text.replace("[simulate:refusal]", "").split())[:120]
    return say(f"Drill reply from {req.model}: received \"{question}\". Order status is available 24/7 in "
               "Kestrel Connect.", complexity=0.1)


# =========================================================================================== canary prompt
@scenario("day6.support_v2", priority=20,
          match=lambda r: CANARY_MARK in r.system_text and r.has_tool("get_customer_profile"))
def support_v2(req: MockRequest) -> Reply:
    """The candidate prompt asks for at most two sentences; the stand-in obeys literally."""
    reply = respond_support(req)
    if any(b.get("type") == "tool_use" for b in reply.content):
        return reply
    text = " ".join(b.get("text", "") for b in reply.content if b.get("type") == "text")
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return say(" ".join(sentences[:2]), complexity=reply.complexity)
