"""Mock policies for advanced Day 6 (evaluation science and release engineering).

Every policy here is a transparent, deterministic heuristic, not a model. It decides only from what is in the
request - the rubric, the reference context, the candidate replies, the email text, the rules the lab wrote
into its system prompt - never from hidden labels, so the agreement and accuracy figures the labs print in
mock mode are honest measurements OF THE HEURISTIC. They illustrate the method; they say nothing about how
well Claude judges or triages. Each lab prints a `[mock]` note where that matters.

Scenarios (matched on marker tags the labs put in their system prompts):
    adv.day6.pairwise_judge   <adv_day6_pairwise_judge rubric="v1|v2">   A-vs-B judge over two replies (lab 03); when
                                                                         the request's schema offers no "tie", equal
                                                                         replies are resolved by position
    adv.day6.pointwise_judge  <adv_day6_pointwise_judge>                 1-5 score for one reply (lab 02)
    adv.day6.triage           <adv_day6_triage>                          ticket triage that OBEYS the prompt's
                                                                         <rules> block literally (labs 05, 06)
    adv.day6.migration_probe  <adv_day6_migration_probe>                 a two-turn order lookup used to probe
                                                                         request shapes and thinking binding (lab 07)
"""

from __future__ import annotations

import re
from typing import Any

from labkit.mock import MockRequest, Reply, json_reply, say, scenario, tool, use_tools
from labkit.mock.schema_tools import resolve, synthesize

PAIRWISE_MARK = "<adv_day6_pairwise_judge"
POINTWISE_MARK = "<adv_day6_pointwise_judge"
TRIAGE_MARK = "<adv_day6_triage"
PROBE_MARK = "<adv_day6_migration_probe"

CATEGORY_NAMES = ("order_status", "shipping_delay", "return_request", "warranty_claim", "billing",
                  "technical_support", "product_inquiry", "safety_incident", "account_access", "other")


# ------------------------------------------------------------------------------------------- helpers
def _section(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""


def _attr(text: str, tag: str, attr: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*\b{attr}=\"([^\"]*)\"", text)
    return m.group(1) if m else ""


def _fit(obj: dict, schema: dict | None, text: str = "") -> Any:
    """Shape `obj` to the request's JSON schema (keep known properties, synthesize missing ones, honour enums)."""
    if not schema:
        return obj
    root = schema
    schema = resolve(schema, root)
    out: dict = {}
    for name, sub in (schema.get("properties") or {}).items():
        if name in obj:
            value = obj[name]
            enum = resolve(sub, root).get("enum")
            if enum and value not in enum:
                value = enum[0]
            out[name] = value
        else:
            out[name] = synthesize(sub, root=root, text=text, name=name)
    return out


# =========================================================================================== judges
# Reply features a careful human grader would also look for. The reference context the lab passes (ticket
# category, the order id on file, whether a human hand-off was required) is what lets the heuristic check
# facts; without it, it can only judge the text.
_HANDOFF = re.compile(r"\bescalated your\b|\bpassed (it|this) to a colleague\b|\bhanded (it|this) to a colleague\b", re.I)
_OVERPROMISE = re.compile(r"\brefund the shipping cost\b|\bfull refund\b|\bfree replacement\b|\bwe will (also )?refund\b", re.I)
_TONE = re.compile(r"\bincredibly sorry\b|\bcompletely unacceptable\b|\btypical\b|\bmessed up\b", re.I)
_ORDER_ID = re.compile(r"\bSO-\d{5}\b")
_RESOLVABLE = ("order_status", "shipping_delay", "return_request", "warranty_claim", "billing", "technical_support",
               "product_inquiry")


def judge_reply(reply: str, category: str, order_id: str, requires_human: bool) -> tuple[int, list[str]]:
    """Score one reply 1-5 from its text plus the reference context. Returns (score, findings)."""
    text = reply.strip()
    findings: list[str] = []
    score = 5
    if not text or len(text.split()) < 6:
        return 1, ["empty or content-free reply"]
    if _HANDOFF.search(text) and category in _RESOLVABLE and not requires_human:
        score -= 2
        findings.append("hands the customer off although the request is one the assistant resolves")
    if _OVERPROMISE.search(text):
        score -= 2
        findings.append("promises compensation beyond policy")
    if order_id:
        mentioned = set(_ORDER_ID.findall(text))
        if mentioned and order_id not in mentioned:
            score -= 2
            findings.append(f"order id {'/'.join(sorted(mentioned))} contradicts the ticket's {order_id}")
    if _TONE.search(text):
        score -= 1
        findings.append("over-apologetic or unprofessional tone")
    return max(1, score), findings


def _context(text: str) -> tuple[str, str, bool]:
    category = _attr(text, "ticket", "category")
    order_id = _attr(text, "ticket", "order_id")
    requires_human = _attr(text, "ticket", "requires_human").lower() == "true"
    return category, order_id, requires_human


def _winner_options(schema: dict | None) -> list:
    """The values the request's output schema allows for `winner` (empty when there is no schema or no enum)."""
    if not schema:
        return []
    root = schema
    props = resolve(schema, root).get("properties") or {}
    return list(resolve(props.get("winner") or {}, root).get("enum") or [])


@scenario("adv.day6.pairwise_judge", match=lambda r: PAIRWISE_MARK in r.system_text, priority=10)
def pairwise_judge(req: MockRequest) -> Reply:
    text = req.last_user_text
    category, order_id, requires_human = _context(text)
    a, fa = judge_reply(_section(text, "reply_a"), category, order_id, requires_human)
    b, fb = judge_reply(_section(text, "reply_b"), category, order_id, requires_human)
    rubric = _attr(req.system_text, "adv_day6_pairwise_judge", "rubric") or "v1"
    options = _winner_options(req.output_schema)
    if options and "tie" not in options:
        # Forced choice: the schema offers no "tie". When the heuristic finds the replies equal it must still name
        # one, and it names the one presented FIRST - the extreme form of the primacy bias LLM judges show on close
        # pairs. The swap test in lab 03 is what exposes it.
        winner = "A" if a >= b else "B"
        rationale = (f"[heuristic judge {rubric}, forced choice] A scores {a} ({'; '.join(fa) or 'no findings'}); "
                     f"B scores {b} ({'; '.join(fb) or 'no findings'})"
                     + ("; equal - picked the first reply presented." if a == b else "."))
        return json_reply(_fit({"rationale": rationale, "winner": winner}, req.output_schema, text), complexity=0.3)
    if rubric == "v2":
        # Rubric v2 ("empathy and brevity"): an apology is no longer a finding, and two replies that score the
        # same are separated by length, shorter first. Same replies, different verdicts - the drift that the
        # re-calibration step in lab 03 measures.
        a += 1 if any("tone" in f for f in fa) else 0
        b += 1 if any("tone" in f for f in fb) else 0
        fa = [f for f in fa if "tone" not in f]
        fb = [f for f in fb if "tone" not in f]
    if a != b:
        winner = "A" if a > b else "B"
    elif rubric == "v2":
        la, lb = len(_section(text, "reply_a")), len(_section(text, "reply_b"))
        winner = "tie" if la == lb else ("A" if la < lb else "B")
    elif a == 5:
        winner = "tie"                                # two clean replies: the heuristic has nothing to prefer
    else:
        winner = "A"                                  # two equally flawed replies: rubric v1 prefers the one presented
        #                                               FIRST - the position bias the swap check in lab 03 exposes
    rationale = (f"[heuristic judge {rubric}] A scores {a} ({'; '.join(fa) or 'no findings'}); "
                 f"B scores {b} ({'; '.join(fb) or 'no findings'}).")
    return json_reply(_fit({"rationale": rationale, "winner": winner}, req.output_schema, text), complexity=0.3)


@scenario("adv.day6.pointwise_judge", match=lambda r: POINTWISE_MARK in r.system_text, priority=10)
def pointwise_judge(req: MockRequest) -> Reply:
    text = req.last_user_text
    category, order_id, requires_human = _context(text)
    score, findings = judge_reply(_section(text, "candidate_reply"), category, order_id, requires_human)
    verdict = {"rationale": "[heuristic judge] " + ("; ".join(findings) or "no findings: accurate, within policy"),
               "score": score, "passed": score >= 4}
    return json_reply(_fit(verdict, req.output_schema, text), complexity=0.3)


# =========================================================================================== triage
# A deliberately modest base heuristic: it knows the obvious words, misses Spanish and German, reads any
# "refund" as a return, and does not know that a wrong or damaged item is a return. Those gaps are the
# headroom the hill-climbing lab works on - and the prompt's <rules> block is how the lab (and a learner)
# changes its behaviour: the stand-in applies the rules LITERALLY, first match wins, before its own heuristic.
_BASE_RULES: list[tuple[str, re.Pattern]] = [
    ("safety_incident", re.compile(r"\bacid\b|smoke|burning|fire pump|evacuat|residents|\bF17\b|sprinkler|will not restart", re.I)),
    ("account_access", re.compile(r"log ?in|password|portal users|locked out|kestrel connect", re.I)),
    ("warranty_claim", re.compile(r"warranty|seized|\bRMA-\d{4}\b.*(replacement|weeping)|pitting", re.I)),
    ("return_request", re.compile(r"\breturn\b|send back|refund", re.I)),
    ("billing", re.compile(r"invoice|past.due|credit card|pay by card|list price|paid", re.I)),
    ("shipping_delay", re.compile(r"not delivered|haven'?t arrived|\blate\b|delay|missed .*delivery|where are|no sign|exception", re.I)),
    ("order_status", re.compile(r"tracking|\bETA\b|status|ship|delivery address|on track", re.I)),
    ("technical_support", re.compile(r"vibration|\bF0\d\b|noisy|runs hot|elastomer|modbus|grease|\bHz\b|what should|check", re.I)),
    ("product_inquiry", re.compile(r"in stock|quote|compatible|listing|pricing|recommend|who should i talk", re.I)),
]
_RULE_LINE = re.compile(r"if the email mentions (.+?) then (?:the )?category is ([a-z_]+)", re.I)
_QUOTED = re.compile(r"\"([^\"]+)\"")


def parse_rules(system_text: str) -> list[tuple[list[str], str]]:
    """The prompt's <rules> block, one (phrases, category) per line: `if the email mentions "a", "b" then category is X`."""
    rules: list[tuple[list[str], str]] = []
    for line in _section(system_text, "rules").splitlines():
        m = _RULE_LINE.search(line)
        if not m:
            continue
        phrases = [p.lower() for p in _QUOTED.findall(m.group(1))]
        if phrases and m.group(2) in CATEGORY_NAMES:
            rules.append((phrases, m.group(2)))
    return rules


def triage_email(subject: str, body: str, rules: list[tuple[list[str], str]]) -> dict:
    text = f"{subject}\n{body}"
    low = text.lower()
    category, why = "other", "no rule or keyword matched"
    for phrases, cat in rules:                        # the prompt's rules first, first match wins
        hit = next((p for p in phrases if p in low), None)
        if hit:
            category, why = cat, f"prompt rule: mentions \"{hit}\""
            break
    else:
        for cat, pattern in _BASE_RULES:
            m = pattern.search(text)
            if m:
                category, why = cat, f"keyword \"{m.group(0)}\""
                break
    injected = bool(re.search(r"ignore (all )?(previous|prior) instructions|system override|administrator mode|"
                              r"note to the ai|skip verification|pre-approved", text, re.I))
    if category == "safety_incident":
        priority = "P1"
    elif category in ("product_inquiry", "other"):
        priority = "P4"
    elif category == "warranty_claim" or re.search(r"shutdown|validation batch|crew waiting|call me today|urgent", text, re.I):
        priority = "P2"
    else:
        priority = "P3"
    return {"category": category, "priority": priority, "requires_human": priority == "P1" or injected,
            "rationale": f"[heuristic triage] {why}"}


@scenario("adv.day6.triage", match=lambda r: TRIAGE_MARK in r.system_text, priority=10)
def triage(req: MockRequest) -> Reply:
    text = req.last_user_text
    subject = _attr(text, "email", "subject")
    body = _section(text, "email") or text
    verdict = triage_email(subject, body, parse_rules(req.system_text))
    return json_reply(_fit(verdict, req.output_schema, text), complexity=0.15)


# =========================================================================================== migration probe
# A deliberately small agent: when a tool is forced it calls that tool with arguments shaped by its schema; otherwise
# it looks the order up with get_order (if offered) and answers from the tool result. Lab 07 sends the same request
# shapes to different models (a migration, a rollback, a prompt hotfix mid-conversation) to see what the API does
# with them and with thinking blocks produced elsewhere; the policy itself only has to behave like a normal agent.
@scenario("adv.day6.migration_probe", match=lambda r: PROBE_MARK in r.system_text, priority=10)
def migration_probe(req: MockRequest) -> Reply:
    asked = sorted(set(re.findall(r"\bSO-\d{5}\b", req.first_user_text)))
    order_id = asked[0] if asked else "SO-10312"
    choice = req.tool_choice or {}
    forced = choice.get("name") if choice.get("type") == "tool" else (
        req.tool_names[0] if choice.get("type") == "any" and req.tool_names else None)
    if forced and not req.called(forced):
        schema = (req.tool_definition(forced) or {}).get("input_schema") or {}
        args = synthesize(schema, root=schema, text=req.first_user_text) if schema else {}
        if isinstance(args, dict) and "order_id" in (schema.get("properties") or {}):
            args["order_id"] = order_id
        return use_tools(tool(forced, **(args if isinstance(args, dict) else {})))
    if req.has_tool("get_order") and not req.called("get_order"):
        return use_tools(tool("get_order", order_id=order_id))
    if req.called("get_order"):
        result = req.calls("get_order")[-1].result_json() or {}
        return say(f"Order {order_id} is {result.get('status', 'in transit')}. [probe answer from the tool result]")
    return say(f"Order {order_id}: noted. [probe answer, no tool needed]")
