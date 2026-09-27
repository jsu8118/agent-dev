"""Mock policies for Day 1 (foundations).

The triage "model" is a transparent keyword heuristic that follows data/support/triage_guidelines.md.
It never looks at the ground-truth labels, so mock-mode evaluation numbers are honest (and imperfect,
which gives you real errors to analyse).  Live Claude will make different - usually fewer - mistakes.
"""

from __future__ import annotations

import re

from ..registry import scenario
from ..reply import json_reply, say
from ..request import MockRequest

SAFETY = re.compile(r"\bacid\b|leak(?:ing)? .*(chemical|hazard)|smoke|burning smell|\bfire\b(?! protection)|injur|"
                    r"evacuat|residents|zone 1|\bF17\b|sprinkler", re.I)
SPAM = re.compile(r"\bseo\b|rankings|marketing packages|traffic 300|hiring|interested in .*roles|job", re.I)
ACCOUNT = re.compile(r"log ?in|password|kestrel connect users|reset link|locked out", re.I)
BILLING = re.compile(r"invoice|paid .*twice|duplicate payment|prepay|factura|credit card|bank transfer|"
                     r"list price|past-due|pay (?:our|by)", re.I)
WARRANTY = re.compile(r"warranty|seized|\bF20\b|pitting|reads 0\.0|leaking at the mechanical seal", re.I)
RETURN = re.compile(r"\breturn\b|send back|over-?ordered|cracked|damaged|wrong (?:size|item)|received bronze|"
                    r"we can't use|refund for the", re.I)
DELAY = re.compile(r"\blate\b|delay|retraso|haven't arrived|not delivered|exception|missed .*delivery|"
                   r"supposed to be here|stuck|no sign", re.I)
STATUS = re.compile(r"status|tracking|\bETA\b|entregado|ship together|on track|delivery address|ship on time", re.I)
TECH = re.compile(r"vibration|\bF0\d\b|grease|noisy|noise|ruido|runs? (?:hot|at)|elastomer|modbus|\bHz\b|"
                  r"what should .* check|qué debemos revisar", re.I)
INQUIRY = re.compile(r"quote|in stock|compatible|listing|do you have|pricing|recommend|lieferzeit|who should i talk", re.I)
INJECTION = re.compile(r"ignore (?:all )?(?:previous|prior) instructions|system override|administrator mode|"
                       r"note to the ai|ai system|pre-approved|skip verification", re.I)


def _body(req: MockRequest) -> str:
    text = req.last_user_text
    m = re.search(r"<ticket>(.*)</ticket>", text, re.S)
    return m.group(1) if m else text


def triage_heuristic(text: str) -> dict:
    sender = (re.search(r"From:\s*(\S+@\S+)", text) or [None, ""])[1]
    if SAFETY.search(text):
        category = "safety_incident"
    elif SPAM.search(text):
        category = "other"
    elif ACCOUNT.search(text):
        category = "account_access"
    elif WARRANTY.search(text):
        category = "warranty_claim"
    elif RETURN.search(text):
        category = "return_request"
    elif BILLING.search(text):
        category = "billing"
    elif DELAY.search(text):
        category = "shipping_delay"
    elif STATUS.search(text):
        category = "order_status"
    elif TECH.search(text):
        category = "technical_support"
    elif INQUIRY.search(text):
        category = "product_inquiry"
    else:
        category = "other"

    if category == "safety_incident":
        priority = "P1"
    elif category == "warranty_claim" or (category == "shipping_delay" and re.search(
            r"shutdown|validation batch|crew|installed before", text, re.I)) or re.search(r"keeps happening", text, re.I):
        priority = "P2"
    elif category in ("product_inquiry", "other"):
        priority = "P4"
    else:
        priority = "P3"

    if re.search(r"\bKC-\d|controller|\bF\d\d\b", text, re.I):
        product = "controller"
    elif re.search(r"seal kits?|\bMS-\d|bearing|coupling|impeller|strainer|repuestos|parts\b", text, re.I):
        product = "spare_part"
    elif re.search(r"sensor|\bVS-10\b|\bPT-40\b", text, re.I):
        product = "sensor"
    elif re.search(r"valve|\bKV-\d", text, re.I):
        product = "valve"
    elif re.search(r"pump|\bKP-\d|bomba|pumpe", text, re.I):
        product = "pump"
    else:
        product = "none"

    order = re.search(r"\bSO-\d{5}\b", text)
    if re.search(r"!!!|not acceptable|ridiculous|can't live|cracked|damaged|wrong|smoke|acid|urgent|retraso|"
                 r"late|second time|keeps happening|dead|scammer", text, re.I):
        sentiment = "negative"
    elif re.search(r"flawless|great job|very interested|thank you so much", text, re.I):
        sentiment = "positive"
    else:
        sentiment = "neutral"
    lookalike = bool(re.search(r"-helpdesk\.|invoice-center\.", sender))
    requires_human = priority == "P1" or bool(INJECTION.search(text)) or lookalike
    if re.search(r"[¿¡]|\bpedido\b|\bhola\b|\bgracias\b|\bnuestro\b|\bbomba\b", text, re.I):
        language = "es"
    elif re.search(r"\bguten\b|\bwir\b|pumpe|förderhöhe|\bund\b", text, re.I):
        language = "de"
    else:
        language = "en"
    subject = (re.search(r"Subject:\s*(.+)", text) or [None, "customer request"])[1].strip()
    summary = f"Customer writes about: {subject[:80]}"
    return {"category": category, "priority": priority, "product_line": product,
            "order_id": order.group(0) if order else None, "sentiment": sentiment, "requires_human": requires_human,
            "language": language, "summary": summary}


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], start=1)}


def _deadline(text: str) -> str | None:
    """Pick the date whose surrounding words signal a deadline (resolved against 2026-09-15)."""
    pattern = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(january|february|march|april|may|june|july|august|september|"
                         r"october|november|december)|(january|february|march|april|may|june|july|august|september|"
                         r"october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?|by the (\d{1,2})(?:st|nd|rd|th)", re.I)
    deadline_words = re.compile(r"before|need|scheduled|booked|shutdown|install|by the|must", re.I)
    best = None
    for m in pattern.finditer(text):
        if m.group(5):
            day, month = int(m.group(5)), 9
        elif m.group(1):
            day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
        else:
            day, month = int(m.group(4)), MONTHS[m.group(3).lower()]
        context = text[max(0, m.start() - 45): m.end() + 60]
        if deadline_words.search(context) and not re.search(r"promised|were told", text[max(0, m.start() - 25): m.start()], re.I):
            best = f"2026-{month:02d}-{day:02d}"
    return best


ACTIONS = {"return_request": "return", "warranty_claim": "repair_or_replace", "product_inquiry": "quote",
           "order_status": "information", "shipping_delay": "information", "technical_support": "information",
           "billing": "information", "account_access": "other", "safety_incident": "callback", "other": "other"}


@scenario("day1.triage_structured", match=lambda r: "<triage_guidelines>" in r.system_text and r.output_schema is not None)
def triage_structured(req: MockRequest):
    from ..schema_tools import synthesize
    body = _body(req)
    result = triage_heuristic(body)
    schema = req.output_schema or {}
    props = schema.get("properties", {})
    extra = synthesize(schema, text=body) if set(props) - set(result) else {}
    out = {k: result[k] if k in result else extra.get(k) for k in props} if props else result
    if "requested_action" in props:
        action = ACTIONS.get(result["category"], "other")
        if re.search(r"refund", body, re.I):
            action = "refund"
        elif re.search(r"call me|please call", body, re.I):
            action = "callback"
        out["requested_action"] = action
    if "customer_deadline" in props:
        out["customer_deadline"] = _deadline(body)
    return json_reply(out, complexity=0.35)


@scenario("day1.triage_prompt_only", match=lambda r: "<triage_guidelines>" in r.system_text and r.output_schema is None)
def triage_prompt_only(req: MockRequest):
    # A realistic failure mode of "just ask for JSON": a friendly preamble and a fenced code block.
    import json
    data = triage_heuristic(_body(req))
    return say("Sure! Here's the triage for this ticket:\n\n```json\n" + json.dumps(data, indent=2) + "\n```\n\n"
               "Let me know if you'd like me to adjust anything.", complexity=0.3)


@scenario("day1.chat", match=lambda r: "<day1_chat>" in r.system_text)
def chat(req: MockRequest):
    question = req.last_user_text
    history = "\n".join(req.texts("user")[:-1])
    if re.search(r"what(?:'s| is) my name", question, re.I):
        m = re.search(r"my name is (\w+)", history, re.I)
        if m:
            return say(f"Your name is {m.group(1)} - you told me earlier in this conversation.", complexity=0.1)
        return say("I don't know your name - you haven't told me in this conversation, and I have no memory of "
                   "other conversations.", complexity=0.1)
    if re.search(r"which (?:site|plant)|where do i work", question, re.I):
        m = re.search(r"(?:work at|site is|plant is) ([\w\s-]+?)(?:[.,]|$)", history, re.I)
        if m:
            return say(f"You work at {m.group(1).strip()}.", complexity=0.1)
        return say("You haven't mentioned your site in this conversation.", complexity=0.1)
    if re.search(r"my name is", question, re.I):
        return say("Nice to meet you! How can I help with your Kestrel equipment today?", complexity=0.1)
    return say("Noted. Anything else I can help you with?", complexity=0.1)


@scenario("day1.reply_drafter", match=lambda r: "<day1_reply_drafter>" in r.system_text)
def reply_drafter(req: MockRequest):
    body = _body(req)
    order = re.search(r"\bSO-\d{5}\b", body)
    name = (re.search(r"\n\s*([A-Z][a-z]+)(?: [A-Z][a-z]+)?\s*(?:\n|,|$)", body) or [None, "there"])[1]
    ref = f" regarding order {order.group(0)}" if order else ""
    return say(
        f"Dear {name},\n\nThank you for contacting Kestrel Pumps & Controls{ref}. I'm sorry for the inconvenience "
        "and I understand how important this delivery is for your schedule. I have asked our logistics team to "
        "check the latest carrier status and the documents the carrier needs, and I will send you the updated "
        "delivery estimate as soon as we have it - no later than end of day tomorrow. If there is anything the "
        "carrier needs from your side, such as customs registration numbers, I will tell you exactly what and "
        "where to send it.\n\nKind regards,\nKestrel Customer Support", complexity=0.5)


@scenario("day1.policy_math", match=lambda r: "<day1_policy_math>" in r.system_text)
def policy_math(req: MockRequest):
    text = req.last_user_text
    qty = int((re.search(r"returns? (\d+)", text) or [0, 0])[1])
    price = float((re.search(r"\$([\d,]+\.\d{2}) each", text) or [0, "0"])[1].replace(",", ""))
    days = int((re.search(r"(\d+) days ago", text) or [0, 0])[1])
    value = round(qty * price, 2)
    fee = round(value * 0.15, 2)
    if days > 30:
        return say(f"Not eligible: the item was delivered {days} days ago, outside the 30-day window. Refund: $0.00.",
                   complexity=0.7)
    return say(f"Eligible (delivered {days} days ago, within 30 days). Line value {qty} x ${price:,.2f} = "
               f"${value:,.2f}; restocking fee 15% = ${fee:,.2f}; refund after inspection = ${value - fee:,.2f}.",
               complexity=0.7)


FIELD_FACTS = {
    "cavitation": "Cavitation is the formation and violent collapse of vapor bubbles when the pressure at the impeller "
                  "eye drops below the liquid's vapor pressure (insufficient NPSH available). It sounds like gravel in "
                  "the pump, causes vibration and pressure fluctuation, and pits the impeller over time.",
    "npsh": "NPSH (net positive suction head) is the margin of suction pressure above the liquid's vapor pressure. "
            "Keep NPSH available at least 1.0 m or 30% above the pump's NPSH required to avoid cavitation.",
    "bep": "The best efficiency point (BEP) is the flow at which a pump runs most efficiently and with the least "
           "vibration; operating far to the left or right of it shortens seal and bearing life.",
}


@scenario("day1.field_engineer", match=lambda r: "concise assistant for field engineers" in r.system_text)
def field_engineer(req: MockRequest):
    question = req.last_user_text.lower()
    if re.search(r"what did i (just )?ask|previous question|earlier", question):
        return say("I don't have any record of a previous question - each request I receive only contains the "
                   "messages sent with it.", complexity=0.1)
    for key, answer in FIELD_FACTS.items():
        if key in question:
            return say(answer, complexity=0.3)
    return say("I can help with Kestrel pump, valve and controller questions - could you give me more detail?",
               complexity=0.1)
