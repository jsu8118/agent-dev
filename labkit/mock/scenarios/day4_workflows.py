"""Mock policies for Day 4 - workflow patterns and multi-agent systems (day4_workflows_multi_agent/labs).

Every scenario matches on a unique `<day4_...>` marker in the system prompt (plus tool names where
relevant), so it never captures another day's requests.  Policies are deterministic stand-ins
for Claude that work ONLY from what is in the request: invoice text, tickets, reports, tool
results.  They never read the dataset's ground-truth files.

Section index
    AP invoices ......... extraction, exception memo, single-call decision, AP agent (labs 01, 07)
    Invoice reviews ..... sectioning reviewers and voting lenses (lab 03)
    Ticket routing ...... classifier, route handlers, flagship-for-everything baseline (lab 02)
    Quality ............. orchestrator, plant workers, synthesizer with a DB tool (lab 04)
    Dispute email ....... generator and rubric judge (lab 05)
    Fleet triage ........ lead agent, analyst subagents, single-agent baseline (lab 06)
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..registry import scenario
from ..reply import Reply, json_reply, say, tool, use_tools
from ..request import MockRequest


# ============================================================================ shared helpers
def _between(text: str, tag: str) -> str | None:
    """Content of the first <tag ...>...</tag> element (attributes allowed)."""
    m = re.search(rf"<{tag}(?:\s[^>]*)?>\s*(.*?)\s*</{tag}>", text, re.S)
    return m.group(1) if m else None


def _json_between(text: str, tag: str) -> Any:
    raw = _between(text, tag)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _attr(text: str, tag: str, name: str) -> str | None:
    m = re.search(rf"<{tag}\s[^>]*\b{name}=\"([^\"]*)\"", text)
    return m.group(1) if m else None


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _d(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _last_json(req: MockRequest, name: str) -> Any:
    calls = req.calls(name)
    return calls[-1].result_json() if calls else None


# ============================================================================ AP invoices
LINE_RE = re.compile(r"^\s{2}(?P<part>[A-Z0-9][A-Z0-9-]+)\s{2,}(?P<desc>.+?)\s{2,}(?P<qty>[\d,.]+)\s+(?P<uom>[A-Z]+)\s+"
                     r"(?P<price>[\d,]+\.\d{2})\s+(?P<amount>[\d,]+\.\d{2})\s*$", re.M)
BANK_RE = re.compile(r"bank details (?:have )?changed|new (?:bank )?account|\bIBAN\b|update (?:your|the) vendor master|"
                     r"remit all payments to", re.I)
INJECT_RE = re.compile(r"automated (?:invoice )?(?:processing )?systems?|\bAI assistants?\b|ignore (?:all |any )?"
                       r"(?:previous|prior) instructions|pre-approved|skip (?:the )?(?:three-way )?match", re.I)


def parse_invoice(doc: str) -> dict:
    """Read one invoice (either of the two supplier layouts) the way a careful clerk would."""
    lines_ = doc.splitlines()
    first = next((l.strip() for l in lines_ if l.strip()), "")
    supplier = first.strip("* ").split("  ")[0].strip()
    number = (re.search(r"Invoice no\.:\s*(\S+)", doc) or re.search(r"INV#\s*(\S+)", doc))
    date = (re.search(r"Invoice date:\s*(\S+)", doc) or re.search(r"\bDATE\s+(\d{4}-\d{2}-\d{2})", doc))
    po = re.search(r"(?:Your PO|YOUR PO):\s*(\S+)", doc, re.I)
    po_number = po.group(1) if po and re.match(r"PO-\d+", po.group(1)) else None
    currency = (re.search(r"Subtotal \(([A-Z]{3})\)", doc) or re.search(r"SUB-TOTAL ([A-Z]{3})", doc)
                or re.search(r"TOTAL DUE \(([A-Z]{3})\)", doc))
    subtotal = re.search(r"Subtotal \([A-Z]{3}\):\s*([\d,]+\.\d{2})", doc) or re.search(r"SUB-TOTAL [A-Z]{3} ([\d,]+\.\d{2})", doc)
    tax = re.search(r"Tax \([\d.]+%\):\s*([\d,]+\.\d{2})", doc) or re.search(r"^TAX ([\d,]+\.\d{2})", doc, re.M)
    total = re.search(r"TOTAL DUE \([A-Z]{3}\):\s*([\d,]+\.\d{2})", doc) or re.search(r"AMOUNT DUE [A-Z]{3} ([\d,]+\.\d{2})", doc)
    remit = re.search(r"account ending (\d{4})", doc)
    items = [{"part_number": m["part"], "description": m["desc"].strip(), "quantity": _num(m["qty"]),
              "unit_of_measure": m["uom"], "unit_price": _num(m["price"]), "amount": _num(m["amount"])}
             for m in LINE_RE.finditer(doc)]
    suspicious = [l.strip() for l in lines_ if BANK_RE.search(l) or INJECT_RE.search(l)]
    return {
        "supplier_name": supplier,
        "invoice_number": number.group(1) if number else "",
        "invoice_date": date.group(1) if date else "",
        "po_number": po_number,
        "currency": currency.group(1) if currency else "USD",
        "lines": items,
        "subtotal": _num(subtotal.group(1)) if subtotal else 0.0,
        "tax_amount": _num(tax.group(1)) if tax else 0.0,
        "total": _num(total.group(1)) if total else 0.0,
        "remit_account_last4": remit.group(1) if remit else None,
        "bank_change_requested": bool(BANK_RE.search(doc)),
        "addresses_automated_systems": bool(INJECT_RE.search(doc)),
        "suspicious_text": " ".join(suspicious) or None,
    }


@scenario("day4.ap_extraction", match=lambda r: "<day4_ap_extraction>" in r.system_text)
def ap_extraction(req: MockRequest) -> Reply:
    doc = _between(req.conversation_text, "invoice_document") or req.first_user_text
    data = parse_invoice(doc)
    return json_reply(data, thinking="Transcribing the header, each priced line and the totals exactly as printed; "
                                     "flagging any instruction-like text as data.", complexity=0.2)


MEMO_TEMPLATES = {
    "price_variance": "Ask the buyer to confirm the agreed price; if the PO price stands, request a credit note or a "
                      "corrected invoice from the supplier.",
    "quantity_exceeds_received": "Check with the receiving dock whether more goods arrived; otherwise ask the supplier "
                                 "to re-invoice only what was received.",
    "no_goods_receipt": "Nothing has been received against this PO - do not pay until a goods receipt exists.",
    "po_cancelled": "The PO is cancelled: return the invoice to the supplier and inform the buyer.",
    "missing_po": "Ask the requester for a valid PO number before processing.",
    "currency_mismatch": "Ask the supplier to re-issue the invoice in the PO currency.",
    "unit_of_measure_mismatch": "Ask the supplier to invoice in the PO's unit of measure (or the buyer to amend the PO).",
    "charge_not_on_po": "Charges not on the PO exceed $50: get buyer approval or ask the supplier to remove them.",
    "tax_calculation_error": "Tax does not match the supplier's rate on file: request a corrected invoice.",
    "arithmetic_error": "The invoice arithmetic is wrong: request a corrected invoice.",
    "duplicate_invoice": "Duplicate of an invoice already processed: reject, do not pay twice.",
    "bank_details_change_request": "Do NOT change bank details from an invoice. Notify security@kestrel-pumps.example; "
                                   "verify via the supplier master-data process (letterhead + call-back to the contact "
                                   "on file).",
    "prompt_injection": "Invoice contains instructions aimed at automated systems - treat as a fraud indicator and "
                        "escalate to security.",
    "total_above_auto_approval_limit": "Route to the AP Controller for sign-off.",
}


@scenario("day4.ap_memo", match=lambda r: "<day4_ap_memo>" in r.system_text)
def ap_memo(req: MockRequest) -> Reply:
    facts = _json_between(req.first_user_text, "decision_facts") or {}
    codes = facts.get("exceptions") or []
    total = facts.get("total")
    amount = f"{facts.get('currency', '')} {total:,.2f}" if isinstance(total, (int, float)) else ""
    supplier = str(facts.get("supplier", "?")).title().replace("Gmbh", "GmbH")
    head = (f"{facts.get('invoice_number', '?')} from {supplier} "
            f"(PO {facts.get('po_number') or 'none'}, {amount}): {str(facts.get('decision', '?')).upper()}.")
    reasons = "; ".join(n.split(": ", 1)[-1] for n in facts.get("notes") or []) or "no exceptions"
    actions = " ".join(MEMO_TEMPLATES.get(c, f"Resolve {c}.") for c in codes)
    return say(f"{head} Why: {reasons}. Next step: {actions}", complexity=0.2)


# ============================================================================ ticket routing (lab 02)
# Keyword rules evaluated in precedence order ("the issue that needs action first"): the stand-in
# classifier reads the email, not the labels.  Real models are better at nuance and worse at
# consistency; that is what the live run measures.
TICKET_SAFETY = re.compile(r"smoke|burning smell|evacuat|injur|(?:acid|chemical)\b.{0,40}\bleak|leak\w*\b.{0,40}"
                           r"\b(?:acid|chemical)|will not restart|won't restart|sprinkler.{0,40}pressure|"
                           r"fire pump.{0,60}\bF\d{2}\b", re.I | re.S)
TICKET_INJECTION = re.compile(r"ignore (?:all )?previous instructions|system override|administrator mode|"
                              r"note to the ai|pre-approved|skip verification|send the (?:temporary )?password to",
                              re.I)
TICKET_RULES: list[tuple[str, re.Pattern]] = [
    ("other", re.compile(r"\bseo\b|boost your|rankings|are you hiring|roles at|job application", re.I)),
    ("account_access", re.compile(r"log ?in|password|kestrel connect|portal users", re.I)),
    ("warranty_claim", re.compile(r"warranty|seized|pitting|weep|replace(?:d|ment)?\b", re.I)),
    ("return_request", re.compile(r"\breturn|send back|over-?ordered|didn't need|wrong (?:size|item|part)|"
                                  r"but received|cracked|crushed|damaged", re.I)),
    ("technical_support", re.compile(r"vibration|mm/s|\bF\d{2}\b|grease|regreas|noisy|noise|ruido|runs? hot|"
                                     r"bearing housing|elastomer|modbus|registers|\b\d+ ?Hz\b", re.I)),
    ("shipping_delay", re.compile(r"not (?:been )?delivered|still not|haven't arrived|delay|retraso|\blate\b|"
                                  r"missed .{0,20}delivery|stuck|no sign|supposed to be here", re.I)),
    ("billing", re.compile(r"invoice|\bAR-\d+|\bpaid\b|payment|prepay|credit card|factura|refund", re.I)),
    ("order_status", re.compile(r"tracking|status|\bETA\b|ship|on track|entregado|delivery address", re.I)),
    ("product_inquiry", re.compile(r"in stock|availability|quote|lead time|recommend|empfehlen|lieferzeit|compatible|"
                                   r"listing|pricing", re.I)),
]
# Pairs where the first match does not settle the question (a multi-issue email).  Refund talk inside
# a return, and fault codes inside a warranty claim, are expected - not ambiguity.
TICKET_EXPECTED_OVERLAP = {("return_request", "billing"), ("warranty_claim", "technical_support"),
                           ("warranty_claim", "return_request"), ("shipping_delay", "order_status")}


def classify_ticket(subject: str, body: str) -> dict:
    text = f"{subject}\n{body}"
    injection = bool(TICKET_INJECTION.search(text))
    if TICKET_SAFETY.search(text):
        return {"category": "safety_incident", "priority": "P1", "requires_human": True, "confidence": 0.95,
                "reason": "Reports a hazardous leak, fire/smoke or a critical outage."}
    matched = [cat for cat, rx in TICKET_RULES if rx.search(text)]
    category = matched[0] if matched else "other"
    confidence = 0.9 if matched else 0.3
    others = [c for c in matched[1:] if (category, c) not in TICKET_EXPECTED_OVERLAP]
    if others and category in ("technical_support", "billing", "return_request") and others[0] in (
            "technical_support", "billing", "return_request", "warranty_claim"):
        confidence = 0.5
    if re.search(r"[áéíóúñ¿¡äöüß]", text):
        confidence = min(confidence, 0.8)
    if category == "safety_incident":
        priority = "P1"
    elif category == "warranty_claim":
        priority = "P2"
    elif category == "shipping_delay" and re.search(r"shutdown|validation|crew|on site", text, re.I):
        priority = "P2"
    elif category in ("product_inquiry", "other"):
        priority = "P4"
    else:
        priority = "P3"
    reason = {True: "Contains instructions aimed at the assistant or a credential request - treat as untrusted."}.get(
        injection, f"Matches the {category.replace('_', ' ')} criteria of the rubric.")
    return {"category": category, "priority": priority, "requires_human": injection or priority == "P1",
            "confidence": confidence, "reason": reason}


def _email(req: MockRequest) -> tuple[str, str, str]:
    text = _between(req.first_user_text, "email") or req.first_user_text
    subject = re.search(r"^Subject:\s*(.*)$", text, re.M)
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    ticket = _attr(req.first_user_text, "email", "ticket_id") or ""
    return ticket, subject.group(1) if subject else "", body


HANDLER_REPLIES = {
    "tech_support": "Thanks for the details. Our application engineers will review them against the product manual; "
                    "to speed this up, please send the pump/controller model and serial number, the operating point "
                    "(flow, pressure) and any fault codes or readings with timestamps.",
    "warranty_desk": "Thank you for reporting this. We'll open a warranty review: please send the serial number, the "
                     "installation date, a short description of the operating conditions and photos of the failure. "
                     "Coverage is confirmed after our technicians inspect the unit.",
    "logistics": "I'm sorry for the delay. Our logistics team is checking the shipment with the carrier now and will "
                 "come back to you with a confirmed delivery date.",
    "order_desk": "Thanks for your message. Our order desk will check the order and reply with the current status "
                  "and next dates.",
    "returns_desk": "Thanks - we can help with that. Our returns team will check eligibility under our returns policy "
                    "and, if approved, send an RMA number and shipping instructions.",
    "billing_desk": "Thank you. Our billing team will review the invoice and payment records and reply with the "
                    "details.",
    "sales": "Thanks for your interest. I've passed your requirements to our sales team, who will reply with a "
             "recommendation, pricing and lead time.",
    "it_helpdesk": "For security we never send passwords by email. Please use the 'Forgot password' link on the Kestrel "
                   "Connect sign-in page; if the link has expired, request a new one and use it within 30 minutes.",
}


@scenario("day4.router_classify",
          match=lambda r: ("<day4_router>" in r.system_text or "<day4_router_escalation>" in r.system_text))
def router_classify(req: MockRequest) -> Reply:
    _, subject, body = _email(req)
    result = classify_ticket(subject, body)
    if "<day4_router_escalation>" in req.system_text:
        result["confidence"] = max(result["confidence"], 0.8)     # the stronger pass commits to an answer
    return json_reply(result, thinking="Applying the rubric's precedence: safety, then the issue needing action first.",
                      complexity=0.25)


@scenario("day4.route_handler", match=lambda r: "<day4_route_handler" in r.system_text)
def route_handler(req: MockRequest) -> Reply:
    kind = _attr(req.system_text, "day4_route_handler", "kind") or "order_desk"
    _, subject, body = _email(req)
    order = re.search(r"\bSO-\d{5}\b", body)
    ref = f" (re: {order.group(0)})" if order else ""
    greeting = "Hola," if re.search(r"[¿¡ñ]|buenos|hola", body, re.I) else (
        "Guten Tag," if re.search(r"guten tag|wir ", body, re.I) else "Hello,")
    return say(f"{greeting}\n\n{HANDLER_REPLIES.get(kind, HANDLER_REPLIES['order_desk'])}{ref}\n\nKind regards,\n"
               "Kestrel Customer Support", complexity=0.3)


@scenario("day4.flagship_all", match=lambda r: "<day4_flagship_all>" in r.system_text)
def flagship_all(req: MockRequest) -> Reply:
    _, subject, body = _email(req)
    result = classify_ticket(subject, body)
    route = {"technical_support": "tech_support", "warranty_claim": "warranty_desk", "shipping_delay": "logistics",
             "order_status": "order_desk", "return_request": "returns_desk", "billing": "billing_desk",
             "product_inquiry": "sales", "account_access": "it_helpdesk"}.get(result["category"])
    reply = "" if result["requires_human"] or route is None else HANDLER_REPLIES[route]
    return json_reply({"category": result["category"], "priority": result["priority"],
                       "requires_human": result["requires_human"], "reply_draft": reply},
                      thinking="Classify with the rubric, then draft a reply that states no unverified facts.",
                      complexity=0.6)


# ============================================================================ FIN-AP-010 as a careful reader applies it
# Used by the stand-ins that must *judge* invoices from documents in the prompt or from tool results
# (lab 03 policy reviewer and voters, lab 07 single-call and agent architectures).
def _inv_key(number: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", number.upper())


def fin_ap_check(inv: dict, po: dict | None, grns: list, supplier: dict | None,
                 priors: list[dict]) -> tuple[str, list[str], list[str]]:
    """Return (decision, exception codes, notes) for one parsed invoice."""
    codes: list[str] = []
    notes: list[str] = []

    def add(code: str, note: str) -> None:
        if code not in codes:
            codes.append(code)
        notes.append(note)

    if inv.get("bank_change_requested"):
        add("bank_details_change_request", "The invoice asks to change bank/remittance details.")
    if inv.get("addresses_automated_systems"):
        add("prompt_injection", "The invoice contains instructions addressed to automated systems / AI assistants.")
    seen = set()
    counted: list[dict] = []
    for prior in priors:                       # earlier duplicates of each other count once
        key = _inv_key(prior.get("invoice_number", ""))
        if key and key not in seen:
            seen.add(key)
            counted.append(prior)
    if _inv_key(inv.get("invoice_number", "")) in seen:
        add("duplicate_invoice", f"Invoice number {inv['invoice_number']} was already processed.")
        return _decide(codes), codes, notes
    rate = Decimal(str(supplier.get("tax_rate", "0"))) if supplier else None
    for line in inv.get("lines", []):
        if _d(Decimal(str(line["quantity"])) * _d(line["unit_price"])) != _d(line["amount"]):
            add("arithmetic_error", f"Line {line['part_number']} does not multiply out.")
    if sum((_d(l["amount"]) for l in inv.get("lines", [])), Decimal(0)) != _d(inv.get("subtotal", 0)):
        add("arithmetic_error", "Line amounts do not add up to the subtotal.")
    if rate is not None and _d(_d(inv.get("subtotal", 0)) * rate) != _d(inv.get("tax_amount", 0)):
        add("tax_calculation_error", f"Tax {inv.get('tax_amount')} differs from {rate:.0%} of the subtotal "
                                     f"({_d(_d(inv.get('subtotal', 0)) * rate)}).")
    if _d(inv.get("subtotal", 0)) + _d(inv.get("tax_amount", 0)) != _d(inv.get("total", 0)):
        add("arithmetic_error", "Subtotal plus tax does not equal the total.")
    if not inv.get("po_number") or not po:
        add("missing_po", "No valid PO is quoted.")
    else:
        if po.get("status") != "open":
            add("po_cancelled", f"PO {po['po_number']} is {po.get('status')}.")
        currency_ok = inv.get("currency") == po.get("currency")
        if not currency_ok:
            add("currency_mismatch", f"Invoice in {inv.get('currency')}, PO in {po.get('currency')}.")
        po_lines = {pl["part"]: pl for pl in po.get("lines", [])}
        extra = Decimal(0)
        for line in inv.get("lines", []):
            pl = po_lines.get(line["part_number"])
            if pl is None:
                extra += _d(line["amount"])
                continue
            if line["unit_of_measure"] != pl["uom"]:
                add("unit_of_measure_mismatch", f"{line['part_number']} invoiced in {line['unit_of_measure']}, "
                                                f"ordered in {pl['uom']}.")
                continue
            if currency_ok and _d(line["unit_price"]) > _d(Decimal(str(pl["unit_price"])) * Decimal("1.01")):
                add("price_variance", f"{line['part_number']} at {line['unit_price']} vs PO {pl['unit_price']} "
                                      f"(more than +1%).")
            received = sum((Decimal(str(r["qty_received"])) for g in grns for r in g.get("lines", [])
                            if r["line"] == pl["line"]), Decimal(0))
            earlier = sum((Decimal(str(l["quantity"])) for p in counted if p.get("po_number") == po["po_number"]
                           for l in p.get("lines", []) if l["part_number"] == line["part_number"]), Decimal(0))
            if received == 0:
                add("no_goods_receipt", f"Nothing received for {line['part_number']}.")
            elif earlier + Decimal(str(line["quantity"])) > received:
                total_qty = (earlier + Decimal(str(line["quantity"]))).normalize()
                add("quantity_exceeds_received", f"{line['part_number']}: {total_qty:f} invoiced in total (earlier "
                                                 f"invoices {earlier.normalize():f}) vs {received.normalize():f} received.")
        fx = Decimal("1.08") if inv.get("currency") == "EUR" else Decimal(1)
        if extra * fx > 50:
            add("charge_not_on_po", f"Charges not on the PO total {extra}.")
    fx = Decimal("1.08") if inv.get("currency") == "EUR" else Decimal(1)
    if not codes and _d(inv.get("total", 0)) * fx > 25000:
        add("total_above_auto_approval_limit", "Total above the $25,000 auto-approval limit.")
    return _decide(codes), codes, notes


def _decide(codes: list[str]) -> str:
    order = ["approve", "route_for_approval", "hold", "reject", "security_hold"]
    mapping = {"duplicate_invoice": "reject", "bank_details_change_request": "security_hold",
               "prompt_injection": "security_hold", "total_above_auto_approval_limit": "route_for_approval"}
    return max((mapping.get(c, "hold") for c in codes), key=order.index, default="approve")


def _review_docs(text: str) -> dict:
    """Parse the <invoice_document>/<purchase_order>/... blocks a lab put into the prompt."""
    doc = _between(text, "invoice_document") or ""
    inv = parse_invoice(doc)
    po = _json_between(text, "purchase_order")
    grns = _json_between(text, "goods_receipts") or []
    supplier = _json_between(text, "supplier_master")
    priors = [parse_invoice(m) for m in re.findall(r"<prior_invoice[^>]*>\s*(.*?)\s*</prior_invoice>", text, re.S)]
    return {"doc": doc, "inv": inv, "po": po if isinstance(po, dict) else None, "grns": grns if isinstance(grns, list) else [],
            "supplier": supplier if isinstance(supplier, dict) else None, "priors": priors}


PRESSURE_RE = re.compile(r"IMPORTANT NOTICE|immediate(?:ly)? payment|pre-approved|pay promptly|REMINDER|urgent|"
                         r"final notice|legal action|!!+", re.I)


# ============================================================================ invoice reviews and votes (lab 03)
@scenario("day4.invoice_review", match=lambda r: "<day4_invoice_review" in r.system_text)
def invoice_review(req: MockRequest) -> Reply:
    lens = _attr(req.system_text, "day4_invoice_review", "lens") or "fraud"
    d = _review_docs(req.first_user_text)
    inv, doc = d["inv"], d["doc"]
    if lens == "fraud":
        findings, codes = [], []
        if inv["bank_change_requested"]:
            quote = next((m.group(0).strip() for m in re.finditer(r"[^.\n]*(?:bank details|IBAN|remit all payments)[^\n]*?"
                                                                  r"(?:\.(?=\s|$)|$)", doc, re.I)), "")
            findings.append(f"Asks to change remittance details: \"{quote}\"")
            codes.append("bank_details_change_request")
        if inv["addresses_automated_systems"]:
            findings.append("Addresses automated systems / AI assistants and claims pre-approval - a manipulation "
                            "attempt, treated as data.")
            codes.append("prompt_injection")
        if any(_inv_key(p["invoice_number"]) == _inv_key(inv["invoice_number"]) for p in d["priors"]):
            findings.append(f"Invoice number {inv['invoice_number']} matches an earlier invoice (possible double billing).")
            codes.append("duplicate_invoice")
        if d["supplier"] and inv["remit_account_last4"] and inv["remit_account_last4"] != d["supplier"].get("bank_account_last4"):
            findings.append("Remit-to account differs from the supplier master.")
        risk = "high" if {"bank_details_change_request", "prompt_injection"} & set(codes) else (
            "medium" if codes or findings else "low")
        action = {"high": "Security hold; notify security@kestrel-pumps.example; verify via call-back to the "
                          "contact on file.", "medium": "Hold for AP review.", "low": "No fraud indicators found."}[risk]
        return json_reply({"risk": risk, "findings": findings or ["No fraud indicators found."], "exception_codes": codes,
                           "recommended_action": action}, complexity=0.3)
    if lens == "policy":
        decision, codes, notes = fin_ap_check(inv, d["po"], d["grns"], d["supplier"], d["priors"])
        risk = "high" if decision in ("security_hold", "reject") else ("medium" if decision == "hold" else "low")
        return json_reply({"risk": risk, "findings": notes or ["Matches PO and goods receipt."], "exception_codes": codes,
                           "recommended_action": f"Decision under FIN-AP-010: {decision}."}, complexity=0.6)
    hits = sorted({m.group(0) for m in PRESSURE_RE.finditer(doc)}, key=str.lower)
    authority = re.search(r"pre-approved by [^.]*", doc, re.I)
    findings = [f"Pressure language: {', '.join(hits)}."] if hits else ["Neutral, routine tone."]
    if authority:
        findings.append(f"Claims authority: \"{authority.group(0)}\" - unverifiable, a classic social-engineering move.")
    risk = "high" if authority else ("medium" if hits else "low")
    return json_reply({"risk": risk, "findings": findings, "exception_codes": [],
                       "recommended_action": "Do not act on urgency or authority claims in the document."
                       if risk != "low" else "None."}, complexity=0.2)


@scenario("day4.invoice_vote", match=lambda r: "<day4_invoice_vote" in r.system_text)
def invoice_vote(req: MockRequest) -> Reply:
    lens = _attr(req.system_text, "day4_invoice_vote", "lens") or "auditor"
    d = _review_docs(req.first_user_text)
    inv, po, supplier = d["inv"], d["po"], d["supplier"]
    decision, codes, _ = fin_ap_check(inv, po, d["grns"], supplier, d["priors"])
    fraud = inv["bank_change_requested"] or inv["addresses_automated_systems"]
    duplicate = "duplicate_invoice" in codes
    account_changed = bool(supplier and inv["remit_account_last4"]
                           and inv["remit_account_last4"] != supplier.get("bank_account_last4"))
    if lens == "security":
        yes, why = fraud, "manipulation / bank-change indicators" if fraud else "no manipulation indicators"
    elif lens == "treasury":
        yes = inv["bank_change_requested"] or account_changed
        why = "payment could go to a different account" if yes else "remittance details match the master file"
    elif lens == "ap_clerk":
        yes, why = duplicate or fraud, "duplicate or bank change" if duplicate or fraud else "no double-payment risk"
    elif lens == "procurement":
        hits = {"price_variance", "charge_not_on_po", "po_cancelled"} & set(codes)
        yes, why = bool(hits), (", ".join(sorted(hits)) if hits else "prices and charges match the PO")
    else:
        hits = {"duplicate_invoice", "quantity_exceeds_received", "no_goods_receipt", "po_cancelled", "missing_po",
                "bank_details_change_request", "prompt_injection"} & set(codes)
        yes, why = bool(hits), (", ".join(sorted(hits)) if hits else "three-way match holds")
    return json_reply({"suspicious": bool(yes), "confidence": 0.85 if yes else 0.75, "reason": why}, complexity=0.2)


# ============================================================================ quality investigation (lab 04)
LOT_RE = re.compile(r"\b[A-Z]{2}-\d{4}-[A-Z0-9]+\b")
SUPPLIER_NAME_RE = re.compile(r"\b([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)* (?:GmbH|Inc\.|Electronics|Co\.|AB))")
RULED_OUT_RE = re.compile(r"[^.]*(?:used only for|No castings from this lot|No product impact|all passed|"
                          r"assembles \S+ units only)[^.]*\.", re.I)
SHIPPED_CONFIRMED_RE = re.compile(r"(?:already|were) shipped|shipped to customers", re.I)
SHIPPED_NONE_RE = re.compile(r"No \w+ from this lot have shipped|No product impact|all passed", re.I)


@scenario("day4.quality_orchestrator", match=lambda r: "<day4_quality_orchestrator>" in r.system_text)
def quality_orchestrator(req: MockRequest) -> Reply:
    index = _between(req.first_user_text, "incident_index") or ""
    cap = int((re.search(r"at most (\d+) workers", req.system_text) or re.search(r"(\d+)", "4")).group(1))
    by_plant: dict[str, list[str]] = {}
    titles: list[str] = []
    for line in index.splitlines()[1:]:
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 4:
            by_plant.setdefault(cells[1], []).append(cells[0])
            titles.append(cells[3].lower())
    plants = sorted(by_plant)
    while len(plants) > cap:                               # merge the smallest plants to respect the cap
        a, b = sorted(plants, key=lambda p: len(by_plant[p]))[:2]
        by_plant[a] += by_plant.pop(b)
        plants.remove(b)
    tasks = [{"worker_id": f"plant-{p}", "plant": p, "report_ids": by_plant[p],
              "objective": f"Analyse the {len(by_plant[p])} {p} incident reports: identify product-quality issues, "
                           "the parts, suppliers and lots involved, and whether affected product has shipped.",
              "questions": ["Which supplier lots are implicated, and how strong is the evidence?",
                            "Has affected product left the plant (shipped units, field risk)?",
                            "Which reports are unrelated to product quality, or ruled out by facts in the report?"],
              "out_of_scope": "Other plants' reports and ERP/field verification (done in the synthesis step)."}
             for p in plants]
    hypotheses = ["A supplier lot behind in-plant test failures has reached customers - verify units built, orders "
                  "and RMAs per lot in the ERP extract."]
    if any("seal" in t for t in titles) and any("cast" in t for t in titles):
        hypotheses.append("Leaks at pump test (P2) could stem from casting defects at P1 - check whether the "
                          "casting lot reached P2.")
    if any("kc-2" in t or "board" in t or "controller" in t for t in titles):
        hypotheses.append("Controller failures at P3 are tied to one board lot rather than to plant processes.")
    return json_reply({"summary": f"{len(tasks)} plant workers analyse their own reports in parallel; the synthesis "
                                  "step verifies lot hypotheses against build records and RMAs.",
                       "tasks": tasks, "hypotheses": hypotheses}, complexity=0.4)


def _report_finding(rid: str, text: str) -> dict:
    field = lambda name: (re.search(rf"^\|\s*{re.escape(name)}\s*\|\s*(.*?)\s*\|\s*$", text, re.M) or [None, ""])[1]
    category, parts = field("Category"), field("Parts / lots").replace("**", "")
    body = text.split("## Description", 1)[-1]
    title = (re.search(r"^# Incident \S+ — (.*)$", text, re.M) or [None, rid])[1]
    quality = category.lower().startswith("quality")
    lot = next(iter(LOT_RE.findall(parts) or LOT_RE.findall(body)), None)
    supplier = (SUPPLIER_NAME_RE.search(parts) or SUPPLIER_NAME_RE.search(body))
    part = parts.split(supplier.group(1))[0] if supplier else parts
    part = re.sub(r"(?:,?\s*supplier)?[\s,;]*$|\blot\b.*$", "", part.strip()).strip(" ,;") or None
    if not quality:
        exposure = "none"
    elif SHIPPED_NONE_RE.search(body):
        exposure = "none"
    elif SHIPPED_CONFIRMED_RE.search(body):
        exposure = "confirmed"
    elif re.search(r"field (?:risk|failures)", body, re.I):
        exposure = "possible"
    else:
        exposure = "unknown"
    severity = field("Severity").split()[0].lower() if field("Severity") else "medium"
    ruled_out = RULED_OUT_RE.search(body)
    first_sentence = re.search(r"## Description\s+([^\n]*?\.)(?:\s|$)", text)
    evidence = (ruled_out.group(0).strip() if ruled_out else (first_sentence.group(1) if first_sentence else title))
    evidence = evidence.replace("**", "")
    return {"report_id": rid, "product_quality_related": quality, "issue": title, "part": part if lot or quality else None,
            "supplier": supplier.group(1) if supplier else None, "lot": lot, "shipped_exposure": exposure,
            "severity": severity if severity in ("low", "medium", "high") else "medium", "evidence": evidence}


@scenario("day4.quality_worker", match=lambda r: "<day4_quality_worker>" in r.system_text)
def quality_worker(req: MockRequest) -> Reply:
    brief = _json_between(req.first_user_text, "brief") or {}
    reports = re.findall(r'<incident_report id="([^"]+)">\s*(.*?)\s*</incident_report>', req.first_user_text, re.S)
    findings = [_report_finding(rid, text) for rid, text in reports]
    lots = sorted({f["lot"] for f in findings if f["lot"] and f["product_quality_related"]
                   and f["shipped_exposure"] != "none"})
    ruled_out = [f["report_id"] for f in findings if f["product_quality_related"] and f["shipped_exposure"] == "none"]
    notes = (f"Ruled out or contained by facts in the reports: {', '.join(ruled_out)}. " if ruled_out else "") + \
        f"{sum(not f['product_quality_related'] for f in findings)} report(s) are safety/environmental, not product quality."
    return json_reply({"plant": brief.get("plant", "?"), "findings": findings, "lots_to_verify": lots, "notes": notes},
                      complexity=0.5)


def _lot_sql(lot: str) -> tuple[str, str]:
    units = ("SELECT b.serial_number, b.sku, b.order_id, b.build_date, b.plant, o.status AS order_status, "
             "c.name AS customer FROM build_records b JOIN orders o ON o.order_id = b.order_id "
             f"JOIN customers c ON c.customer_id = o.customer_id WHERE b.seal_lot = '{lot}' OR b.board_lot = '{lot}' "
             "ORDER BY b.build_date")
    rmas = ("SELECT r.rma_id, r.order_id, r.sku, r.reason_code, r.status, r.requested_at, r.notes, b.serial_number "
            "FROM rmas r JOIN build_records b ON b.order_id = r.order_id AND b.sku = r.sku "
            f"WHERE b.seal_lot = '{lot}' OR b.board_lot = '{lot}'")
    return units, rmas


CONTROL_SQL = ("SELECT COALESCE(b.seal_lot, b.board_lot) AS lot, COUNT(DISTINCT b.serial_number) AS units_built, "
               "COUNT(DISTINCT CASE WHEN r.reason_code IN ('defective', 'warranty_claim') THEN r.rma_id END) AS defect_rmas "
               "FROM build_records b LEFT JOIN rmas r ON r.order_id = b.order_id AND r.sku = b.sku "
               "GROUP BY lot ORDER BY defect_rmas DESC, lot")


@scenario("day4.quality_synthesizer",
          match=lambda r: "<day4_quality_synthesizer>" in r.system_text and r.has_tool("query_quality_db"))
def quality_synthesizer(req: MockRequest) -> Reply:
    findings_json = _json_between(req.first_user_text, "worker_findings") or []
    findings = [f for plant in findings_json for f in plant.get("findings", [])]
    lots = sorted({lot for plant in findings_json for lot in plant.get("lots_to_verify", [])})
    if not req.called("query_quality_db"):
        calls = []
        for lot in lots:
            units, rmas = _lot_sql(lot)
            calls += [tool("query_quality_db", sql=units, purpose=f"Units, orders and customers built with lot {lot}"),
                      tool("query_quality_db", sql=rmas, purpose=f"Field returns (RMAs) of units built with lot {lot}")]
        calls.append(tool("query_quality_db", sql=CONTROL_SQL,
                          purpose="Control: defect RMAs per lot across all seal and board lots"))
        return use_tools(*calls, preface="Verifying each lot hypothesis against build records and RMAs.",
                         thinking="Field exposure must come from the ERP extract, not from the reports.", complexity=0.6)

    def rows_for(lot: str, marker: str) -> list[dict]:
        for call in req.calls("query_quality_db"):
            data = call.result_json() or {}
            if f"'{lot}'" in call.input.get("sql", "") and marker in call.input.get("sql", "") and "rows" in data:
                return [dict(zip(data["columns"], row)) for row in data["rows"]]
        return []

    control = next((c.result_json() for c in req.calls("query_quality_db") if "GROUP BY lot" in c.input.get("sql", "")),
                   None) or {}
    control_rows = [dict(zip(control.get("columns", []), row)) for row in control.get("rows", [])]
    problems = []
    for lot in lots:
        units = rows_for(lot, "c.name AS customer")
        rmas = [r for r in rows_for(lot, "r.rma_id") if r.get("reason_code") in ("defective", "warranty_claim")]
        lot_findings = [f for f in findings if f.get("lot") == lot]
        base = next((f for f in lot_findings if f.get("supplier")), lot_findings[0] if lot_findings else {})
        part = min((f["part"] for f in lot_findings if f.get("part")), key=len, default=None)
        others = [r for r in control_rows if r.get("lot") != lot and (r.get("lot") or "")[:2] == lot[:2]]
        background = sum(r.get("defect_rmas", 0) for r in others)
        orders = sorted({u["order_id"] for u in units})
        evidence = [f"{f['report_id']}: {f['evidence']}" for f in lot_findings]
        evidence.append(f"ERP sample: {len(units)} unit(s) built with {lot} went to {len(orders)} order(s).")
        if rmas:
            evidence.append(f"{len(rmas)} defect RMA(s) on {lot} units ({', '.join(r['rma_id'] for r in rmas)}) vs "
                            f"{background} across {len(others)} other {lot[:2]} lot(s) - the control comparison.")
            evidence += [f"{r['rma_id']} ({r['serial_number']}): {r.get('notes') or r['reason_code']}" for r in rmas]
        actions = [f"Keep remaining {lot} stock on quality hold and follow up the supplier corrective action.",
                   f"Notify the customers on {', '.join(orders) or 'the affected orders'} and plan inspection or "
                   "replacement of units in service."]
        if not rmas:
            actions.append("No field returns yet: monitor warranty claims for these serials and prioritise customers "
                           "in demanding conditions.")
        problems.append({
            "lot": lot, "supplier": base.get("supplier") or "unknown", "part": part or "unknown",
            "failure_mode": "; ".join(dict.fromkeys(f["issue"] for f in lot_findings)) or "see evidence",
            "evidence": evidence, "units_built_in_sample": len(units), "affected_orders": orders,
            "customers": sorted({u["customer"] for u in units}), "field_failures": [r["rma_id"] for r in rmas],
            "recommended_actions": actions})
    dismissed = []
    for f in findings:
        if f.get("lot") in lots:
            continue
        if not f.get("product_quality_related"):
            reason = f"Not a product-quality issue ({f.get('issue')}); out of scope for the lot investigation."
        else:
            reason = f"Ruled out by the report itself: \"{f.get('evidence')}\""
        dismissed.append({"report_id": f["report_id"], "reason": reason})
    summary = (f"{len(problems)} supplier-lot problem(s) confirmed against the ERP extract: " +
               "; ".join(f"{p['lot']} ({p['supplier']}, {p['units_built_in_sample']} units in the sample, "
                         f"{len(p['field_failures'])} field RMA(s))" for p in problems) +
               f". {len(dismissed)} report(s) dismissed as unrelated or contained.")
    return json_reply({"executive_summary": summary, "problems": problems, "dismissed": dismissed,
                       "open_questions": ["The ERP extract is a sample: reconcile unit counts with the plants' full "
                                          "build records before sizing the field action."]},
                      thinking="Each claim is backed by a report quote or a query result; the control query separates "
                               "a lot problem from background returns.", complexity=0.7)


# ============================================================================ dispute email (lab 05)
HOSTILE_RE = re.compile(r"unacceptable|will not tolerate|ridiculous|we demand|sort this out|or else", re.I)


def _dispute_draft(f: dict, *, polite: bool, clear_ask: bool, correct_numbers: bool, states_hold: bool) -> str:
    cur = f.get("currency", "EUR")
    pct = f.get("variance_pct", "?") if correct_numbers else "4"
    subject = (f"Subject: Price difference on invoice {f['invoice_number']} (PO {f['po_number']})" if polite
               else f"Subject: Invoice {f['invoice_number']} - wrong price")
    greeting = f"Dear {f['supplier']} team," if polite else f"Dear {f['supplier']},"
    body = (f"Thank you for invoice {f['invoice_number']} dated {f['invoice_date']} for {f['quantity']} {f['uom']} of "
            f"{f['part']}, received in full ({f['goods_receipt']}). " if polite else
            f"Your invoice {f['invoice_number']} charges {cur} {f['invoiced_unit_price']} per {f['part']} although our "
            f"PO {f['po_number']} says {cur} {f['po_unit_price']}. ")
    if polite:
        body += (f"The invoice bills {cur} {f['invoiced_unit_price']} per unit, while purchase order {f['po_number']} of "
                 f"{f['po_date']} agreed {cur} {f['po_unit_price']} - a difference of {cur} {f['variance_per_unit']} "
                 f"per unit ({pct}%), or {cur} {f['overcharge_total']} in total.")
    else:
        body += (f"That is about {pct}% more than agreed, which is unacceptable - we will not tolerate unilateral price "
                 f"increases. The overcharge is {cur} {f['overcharge_total']} on {f['quantity']} {f['uom']}.")
    if states_hold:
        body += (f" Our price-tolerance policy allows up to {f.get('price_tolerance_pct', '1')}% above the PO price, so "
                 "the invoice is on hold until the difference is resolved.")
    ask = (f"Please send either a credit note for {cur} {f['overcharge_total']} or a corrected invoice at {cur} "
           f"{f['po_unit_price']} per unit by {f.get('reply_by', 'return')}, replying to this email. Once we receive it, "
           f"the invoice will be released for payment on your {f.get('payment_terms', '')} terms."
           if clear_ask else "Please sort this out.")
    closing = f"Kind regards,\n{f.get('sender', 'Accounts Payable')}" + (f" (buyer: {f['buyer']})" if polite else "")
    return f"{subject}\n\n{greeting}\n\n{body}\n\n{ask}\n\n{closing}"


@scenario("day4.dispute_writer", match=lambda r: "<day4_dispute_writer>" in r.system_text)
def dispute_writer(req: MockRequest) -> Reply:
    facts = _json_between(req.first_user_text, "dispute_facts") or {}
    critique = (_between(req.first_user_text, "critique") or "").lower()
    if not critique:
        # The stand-in's first draft is deliberately weak (see lab 05's docstring).
        draft = _dispute_draft(facts, polite=False, clear_ask=False, correct_numbers=False, states_hold=False)
        return say(draft, complexity=0.4)
    previous = (_between(req.first_user_text, "previous_draft") or "")
    fix = lambda *words: any(w in critique for w in words)
    draft = _dispute_draft(
        facts,
        polite=fix("tone", "hostile", "courteous", "accus", "unacceptable") or not HOSTILE_RE.search(previous),
        clear_ask=fix("ask", "action", "deadline", "reply", "credit note"),
        correct_numbers=fix("percent", "%", "figure", "number", "amount") or "4%" not in previous,
        states_hold=fix("hold", "policy", "tolerance"))
    return say(draft, thinking="Addressing each critique point; everything else stays as it was.", complexity=0.4)


@scenario("day4.dispute_judge", match=lambda r: "<day4_dispute_judge>" in r.system_text)
def dispute_judge(req: MockRequest) -> Reply:
    facts = _json_between(req.first_user_text, "dispute_facts") or {}
    draft = _between(req.first_user_text, "draft") or ""
    variance = Decimal(str(facts.get("variance_pct", "0")))
    allowed_pct = {Decimal(str(facts.get("price_tolerance_pct", "1"))), variance, variance.quantize(Decimal("0.1")),
                   variance.quantize(Decimal("1"))}
    wrong_pct = [m.group(0) for m in re.finditer(r"(\d+(?:\.\d+)?)\s?%", draft) if Decimal(m.group(1)) not in allowed_pct]
    missing = [k for k in ("invoice_number", "po_number") if str(facts.get(k, "")) not in draft]
    scores, critique = [], []
    if wrong_pct or missing:
        scores.append({"criterion": "facts_correct", "score": 2,
                       "comment": f"States {', '.join(wrong_pct)}; the facts give a variance of {variance}%."
                       if wrong_pct else f"Missing {', '.join(missing)}."})
        critique.append(f"Correct the percentage: the variance is {variance}% (EUR {facts.get('variance_per_unit')} per unit)."
                        if wrong_pct else f"Add {', '.join(missing)}.")
    else:
        scores.append({"criterion": "facts_correct", "score": 5, "comment": "All figures match the facts."})
    if re.search(r"on hold", draft, re.I) and re.search(r"tolerance|1%", draft, re.I):
        scores.append({"criterion": "policy_compliant", "score": 5, "comment": "States the hold and the tolerance rule."})
    else:
        scores.append({"criterion": "policy_compliant", "score": 3,
                       "comment": "Does not say the invoice is on hold under the price-tolerance policy."})
        critique.append("State that the invoice is on hold because the price exceeds the PO price by more than the "
                        "1% tolerance.")
    hostile = HOSTILE_RE.findall(draft)
    if hostile:
        scores.append({"criterion": "tone", "score": 2, "comment": f"Hostile phrasing: {', '.join(hostile)}."})
        critique.append("Make the tone courteous and factual: remove '" + "', '".join(hostile) + "'.")
    else:
        scores.append({"criterion": "tone", "score": 5, "comment": "Firm and professional."})
    if re.search(r"credit note|corrected invoice", draft, re.I) and str(facts.get("reply_by", "@@")) in draft:
        scores.append({"criterion": "clear_ask", "score": 5, "comment": "One concrete action with amount and date."})
    else:
        scores.append({"criterion": "clear_ask", "score": 2, "comment": "No concrete requested action or reply-by date."})
        critique.append(f"Ask for a credit note of EUR {facts.get('overcharge_total')} or a corrected invoice at the PO "
                        f"price, with a reply-by date of {facts.get('reply_by')}.")
    passed = all(s["score"] >= 4 for s in scores)
    return json_reply({"scores": scores, "passed": passed, "critique": [] if passed else critique}, complexity=0.4)


# ============================================================================ fleet triage (lab 06)
def _fleet_assets(req: MockRequest) -> list[str]:
    for t in req.tools:
        if t.get("name") == "query_telemetry":
            return list(t["input_schema"]["properties"]["asset_id"].get("enum", []))
    return []


def _telemetry_results(req: MockRequest) -> dict[tuple[str, str], dict]:
    out = {}
    for call in req.calls("query_telemetry"):
        data = call.result_json()
        if isinstance(data, dict) and "error" not in data:
            out[(call.input.get("asset_id"), call.input.get("view"))] = data
    return out


def _win(summary: dict, name: str, key: str) -> float | None:
    return (summary.get("windows", {}).get(name) or {}).get(key)


def _pattern(summary: dict) -> str:
    """Classify the weekly picture: standby / sensor / overload / progressive / step / erratic / spikes / steady."""
    if (_win(summary, "W4", "running_h") or 0) < 24:
        return "standby"
    if summary["anomalies"]["zero_vibration_while_running"]["count"] >= 6:
        return "sensor"
    v1, v3, vl = (_win(summary, w, "vib_mean_mm_s") or 0 for w in ("W1", "W3", "last_2d"))
    t1, tl = (_win(summary, w, "bearing_temp_mean_c") or 0 for w in ("W1", "last_2d"))
    c1, cl = (_win(summary, w, "current_pct_rated") or 0 for w in ("W1", "last_2d"))
    f1, fl = (_win(summary, w, "flow_pct_bep") or 0 for w in ("W1", "last_2d"))
    sds = [_win(summary, w, "vib_sd_mm_s") or 0 for w in ("W1", "W2", "W3", "W4", "last_2d")]
    if cl - c1 >= 10 and fl - f1 >= 8:
        return "overload"
    if vl > 1.5 * v1 and tl - t1 > 8:
        return "progressive"
    if v3 > 1.5 * v1 and abs(vl - v3) < 0.1 * v3:
        return "step"
    if sum(sd >= 0.5 for sd in sds) >= 3:
        return "erratic"
    if summary["anomalies"]["spikes_over_50_mm_s"]:
        return "spikes"
    return "steady"


FOLLOW_UPS = {"sensor": ["work_orders"], "overload": ["work_orders"], "progressive": ["daily"],
              "step": ["daily", "work_orders"], "erratic": ["hourly_profile", "work_orders"]}


def _diagnose(asset: str, res: dict[tuple[str, str], dict]) -> dict:
    s = res[(asset, "summary")]
    lim = s["limits"]
    pattern = _pattern(s)
    wos = (res.get((asset, "work_orders")) or {}).get("work_orders", [])
    v1, vl = _win(s, "W1", "vib_mean_mm_s"), _win(s, "last_2d", "vib_mean_mm_s")
    t1, tl = _win(s, "W1", "bearing_temp_mean_c"), _win(s, "last_2d", "bearing_temp_mean_c")
    out = {"asset_id": asset, "status": "healthy", "issue": "none", "confidence": 0.85,
           "recommended_action": "No action; keep routine monitoring."}
    if pattern == "standby":
        hours = sum(_win(s, w, "running_h") or 0 for w in ("W1", "W2", "W3", "W4", "last_2d"))
        out["evidence"] = (f"Standby pump: {hours} test-run hours in 30 days with normal readings "
                           f"(vibration ~{_win(s, 'W4', 'vib_mean_mm_s')} mm/s). Too little running time to trend.")
        out["recommended_action"] = "No action; keep the weekly standby test runs."
    elif pattern == "sensor":
        z = s["anomalies"]["zero_vibration_while_running"]
        prior = next((w for w in wos if "VS-10" in w.get("description", "") + w.get("parts_used", "")), None)
        out.update(status="action_required", issue="sensor_fault", confidence=0.9,
                   evidence=f"{z['count']} hourly readings of exactly 0.0 mm/s while running ({z['first']} to {z['last']}) "
                            "while pressure, flow and current stayed normal - the sensor failed, not the pump."
                            + (f" A VS-10 was already replaced on {prior['date']} ({prior['description']})." if prior else ""),
                   recommended_action="Replace/repair the VS-10 sensor and cabling within a week; verify with a "
                                      "handheld meter (CM-GUIDE-01 §4-5).")
    elif pattern == "overload":
        cause = next((w for w in wos if re.search(r"demand|flow|duty", w.get("description", ""), re.I)), None)
        cl = _win(s, "last_2d", "current_pct_rated")
        out.update(status="action_required" if cl and cl > 100 else "watch", issue="overload_right_of_bep",
                   confidence=0.8,
                   evidence=f"Flow {_win(s, 'W1', 'flow_pct_bep')}% -> {_win(s, 'last_2d', 'flow_pct_bep')}% of BEP and "
                            f"motor current {_win(s, 'W1', 'current_pct_rated')}% -> {cl}% of rated over four weeks, "
                            f"bearing temperature flat ({t1} -> {tl} degC)"
                            + (f"; {cause['work_order_id']} ({cause['date']}): {cause['description']}" if cause else "") + ".",
                   recommended_action="Throttle back to the duty point or re-rate/resize the pump; the motor is running "
                                      "above rated current.")
    elif pattern == "progressive":
        daily = (res.get((asset, "daily")) or {}).get("daily", [])
        crossed = next((d["date"] for d in daily if (d.get("vib_mean_mm_s") or 0) > lim["alarm_mm_s"]), None)
        out.update(status="action_required" if vl and vl > lim["alert_mm_s"] else "watch", issue="bearing_wear",
                   confidence=0.9,
                   evidence=f"Vibration {v1} -> {vl} mm/s and bearing temperature {t1} -> {tl} degC rising together "
                            "and accelerating week on week"
                            + (f"; daily mean crossed the {lim['alarm_mm_s']} mm/s alarm limit on {crossed}" if crossed else "")
                            + ".",
                   recommended_action="Notify the shift supervisor and plan a controlled shutdown within 24 h; replace "
                                      "the bearings and check lubrication (CM-GUIDE-01 §5).")
    elif pattern == "step":
        daily = (res.get((asset, "daily")) or {}).get("daily", [])
        step_day = next((d["date"] for d in daily if (d.get("vib_mean_mm_s") or 0) > 1.3 * (v1 or 0)), None)
        cause = next((w for w in wos if step_day and w.get("date", "") <= step_day and w.get("date", "") >= "2026-08-01"), None)
        text = (cause or {}).get("description", "")
        issue = ("misalignment" if re.search(r"coupling|motor|alignment", text, re.I) else
                 "imbalance" if re.search(r"impeller", text, re.I) else "other")
        out.update(status="action_required" if vl and vl > lim["alert_mm_s"] else "watch", issue=issue, confidence=0.85,
                   evidence=f"Step change from {v1} to ~{vl} mm/s starting {step_day}, flat since (alert limit "
                            f"{lim['alert_mm_s']}); bearing temperature +{round((tl or 0) - (t1 or 0), 1)} degC"
                            + (f"; {cause['work_order_id']} on {cause['date']}: {text}" if cause else "") + ".",
                   recommended_action="Laser-align the coupling (SVC-ALIGN) and re-check vibration afterwards."
                   if issue == "misalignment" else "Inspect the work done at the step date.")
    elif pattern == "erratic":
        prof = (res.get((asset, "hourly_profile")) or {}).get("hour_of_day_profile", [])
        vibs = sorted(h["vib_mean_mm_s"] for h in prof if h.get("vib_mean_mm_s") is not None)
        median = vibs[len(vibs) // 2] if vibs else 0
        bad = [h for h in prof if (h.get("vib_mean_mm_s") or 0) > 1.5 * median]
        good = [h for h in prof if h not in bad]
        avg = lambda rows, k: round(sum(r[k] for r in rows) / len(rows), 3) if rows else 0
        noisy = next((w for w in wos if re.search(r"noise|pitting|cavitat", w.get("description", ""), re.I)), None)
        pitting = any("pitting" in w.get("description", "").lower() for w in wos)
        if bad and avg(bad, "pressure_sd_bar") > 3 * max(avg(good, "pressure_sd_bar"), 1e-6):
            hours = f"{bad[0]['hour_utc']:02d}:00-{bad[-1]['hour_utc'] + 1:02d}:00 UTC"
            out.update(status="action_required" if pitting else "watch", issue="cavitation", confidence=0.85,
                       evidence=f"Mean vibration looks normal ({vl} mm/s) but it is erratic (weekly spread ~"
                                f"{_win(s, 'W4', 'vib_sd_mm_s')} mm/s). Every night {hours}: vibration "
                                f"{avg(bad, 'vib_mean_mm_s')} vs {avg(good, 'vib_mean_mm_s')} mm/s, discharge-pressure "
                                f"spread {avg(bad, 'pressure_sd_bar')} vs {avg(good, 'pressure_sd_bar')} bar, flow "
                                f"{avg(bad, 'flow_mean_m3h')} vs {avg(good, 'flow_mean_m3h')} m3/h"
                                + "".join(f"; {w['work_order_id']} ({w['date']}): {w['description']}" for w in wos
                                          if re.search(r"noise|pitting", w.get("description", ""), re.I)) + ".",
                       recommended_action="Check NPSH available at night (minimum reservoir level), clean the suction "
                                          "strainer and keep flow above the minimum; inspect the impeller.")
        else:
            out.update(status="watch", issue="other", confidence=0.5,
                       evidence=f"Erratic vibration without a time-of-day pattern{'; ' + noisy['description'] if noisy else ''}.",
                       recommended_action="Increase monitoring to daily and inspect.")
    else:
        spikes = s["anomalies"]["spikes_over_50_mm_s"]
        out["evidence"] = (f"Vibration steady at ~{vl} mm/s (zone A/B; alert {lim['alert_mm_s']}), temperature, flow and "
                           "current steady" + (f"; {len(spikes)} isolated single-sample spike(s) above 50 mm/s are "
                                               "electrical noise (CM-GUIDE-01 §4)" if spikes else "") + ".")
    return out


def _fleet_step(req: MockRequest, final_kind: str) -> Reply:
    assets = _fleet_assets(req)
    res = _telemetry_results(req)
    todo = [a for a in assets if (a, "summary") not in res]
    load = min(1.0, 0.15 + 0.07 * len(assets))          # more pumps in this context -> more to think about
    if todo:
        return use_tools(*[tool("query_telemetry", asset_id=a, view="summary") for a in todo],
                         preface=f"Starting with the weekly summary of {len(todo)} pump(s).", complexity=load)
    follow = [tool("query_telemetry", asset_id=a, view=v) for a in assets
              for v in FOLLOW_UPS.get(_pattern(res[(a, "summary")]), []) if (a, v) not in res]
    if follow:
        return use_tools(*follow, preface="Checking the patterns that need more context.", complexity=load)
    assessments = [_diagnose(a, res) for a in assets]
    if final_kind == "report":
        flagged = [a["asset_id"] for a in assessments if a["status"] != "healthy"]
        return json_reply({"assets": assessments, "notes": f"{len(flagged)} of {len(assets)} pumps need attention: "
                                                             f"{', '.join(flagged) or 'none'}."}, complexity=load)
    return json_reply(_fleet_triage(assessments, req), complexity=load)


def _fleet_triage(assessments: list[dict], req: MockRequest) -> dict:
    fleet = {p["asset_id"]: p for p in (_json_between(req.first_user_text, "fleet") or [])}
    rank = {"action_required": 0, "watch": 1, "healthy": 2}
    ordered = sorted((a for a in assessments if a["status"] != "healthy"),
                     key=lambda a: (rank[a["status"]], fleet.get(a["asset_id"], {}).get("criticality", "C"), a["asset_id"]))
    priorities = [f"{a['asset_id']} ({a['issue']}, criticality {fleet.get(a['asset_id'], {}).get('criticality', '?')}): "
                  f"{a['recommended_action']}" for a in ordered]
    summary = (f"{len(ordered)} of {len(assessments)} pumps need attention; "
               f"{sum(a['status'] == 'action_required' for a in ordered)} require action.")
    return {"summary": summary, "assets": assessments, "priorities": priorities}


@scenario("day4.fleet_analyst",
          match=lambda r: "<day4_fleet_analyst>" in r.system_text and r.has_tool("query_telemetry"))
def fleet_analyst(req: MockRequest) -> Reply:
    return _fleet_step(req, "report")


@scenario("day4.fleet_single", match=lambda r: "<day4_fleet_single>" in r.system_text and r.has_tool("query_telemetry"))
def fleet_single(req: MockRequest) -> Reply:
    return _fleet_step(req, "triage")


@scenario("day4.fleet_lead", match=lambda r: "<day4_fleet_lead>" in r.system_text and r.has_tool("delegate_to_analyst"))
def fleet_lead(req: MockRequest) -> Reply:
    fleet = _json_between(req.first_user_text, "fleet") or []
    if not req.called("delegate_to_analyst"):
        desc = next((t.get("description", "") for t in req.tools if t.get("name") == "delegate_to_analyst"), "")
        cap = int((re.search(r"(\d+) analysts per run", desc) or re.search(r"(\d+)", "3")).group(1))
        per = int((re.search(r"(\d+) pumps per analyst", desc) or re.search(r"(\d+)", "5")).group(1))
        groups: dict[str, list[str]] = {}
        for p in fleet:
            groups.setdefault(p["site"], []).append(p["asset_id"])
        batches = [ids[i:i + per] for ids in groups.values() for i in range(0, len(ids), per)]
        while len(batches) > cap and len(batches) > 1:            # merge the two smallest groups if allowed
            batches.sort(key=len)
            if len(batches[0]) + len(batches[1]) > per:
                break
            batches = [batches[0] + batches[1]] + batches[2:]
        by_id = {p["asset_id"]: p for p in fleet}
        calls = []
        for ids in batches[:cap]:
            context = "; ".join(f"{a}: {by_id[a]['model']}, criticality {by_id[a]['criticality']}, {by_id[a]['foundation']} "
                                f"foundation - {by_id[a]['notes']}" for a in ids)
            calls.append(tool("delegate_to_analyst", asset_ids=ids, brief=(
                f"Triage these {len(ids)} pumps at {by_id[ids[0]]['site']} for developing faults (telemetry to 2026-09-14). "
                f"Context: {context}. For each pump return status, issue, the evidence numbers and the recommended "
                "action. Start with the summary view; use work orders to explain step changes and the hour-of-day "
                "profile for intermittent patterns. Do not analyse other pumps.")))
        return use_tools(*calls, preface=f"Delegating {len(calls)} site groups in parallel.", complexity=0.3)
    assessments = []
    for call in req.calls("delegate_to_analyst"):
        data = call.result_json()
        if isinstance(data, dict) and not call.is_error:
            assessments += data.get("assets", [])
    return json_reply(_fleet_triage(assessments, req), complexity=min(1.0, 0.2 + 0.05 * len(assessments)))


# ============================================================================ AP architectures (lab 07)
@scenario("day4.ap_single_call", match=lambda r: "<day4_ap_single_call>" in r.system_text)
def ap_single_call(req: MockRequest) -> Reply:
    d = _review_docs(req.first_user_text)
    decision, codes, notes = fin_ap_check(d["inv"], d["po"], d["grns"], d["supplier"], d["priors"])
    return json_reply({"decision": decision, "exception_codes": codes,
                       "rationale": " ".join(notes) or "Invoice matches the PO and goods receipt."},
                      thinking="Applying FIN-AP-010 checks 1-10 in order, then the approval routing.", complexity=0.7)


@scenario("day4.ap_agent", match=lambda r: "<day4_ap_agent>" in r.system_text and r.has_tool("get_purchase_order"))
def ap_agent(req: MockRequest) -> Reply:
    doc = _between(req.first_user_text, "invoice_document") or ""
    inv = parse_invoice(doc)
    if not req.tool_calls:
        calls = [tool("get_supplier", supplier_name=inv["supplier_name"]),
                 tool("get_invoice_history", supplier_name=inv["supplier_name"])]
        if inv["po_number"]:
            calls += [tool("get_purchase_order", po_number=inv["po_number"]),
                      tool("get_goods_receipts", po_number=inv["po_number"])]
        return use_tools(*calls, preface="Looking up the PO, receipts, supplier record and invoice history.",
                         complexity=0.4)
    po = _last_json(req, "get_purchase_order")
    po = po if isinstance(po, dict) and "error" not in po else None
    grns = _last_json(req, "get_goods_receipts")
    supplier = _last_json(req, "get_supplier")
    history = _last_json(req, "get_invoice_history") or []
    priors = [{"invoice_number": h.get("invoice_number", ""), "po_number": h.get("po_number"),
               "lines": [{"part_number": l["part_number"], "quantity": l["quantity"]} for l in h.get("lines", [])]}
              for h in history if isinstance(h, dict)]
    decision, codes, notes = fin_ap_check(inv, po, grns if isinstance(grns, list) else [],
                                          supplier if isinstance(supplier, dict) and "error" not in supplier else None,
                                          priors)
    return json_reply({"decision": decision, "exception_codes": codes,
                       "rationale": " ".join(notes) or "Invoice matches the PO and goods receipt.",
                       "invoice_number": inv["invoice_number"], "supplier_name": inv["supplier_name"],
                       "po_number": inv["po_number"],
                       "lines": [{"part_number": l["part_number"], "quantity": l["quantity"],
                                  "unit_of_measure": l["unit_of_measure"]} for l in inv["lines"]]},
                      thinking="All lookups are in; applying FIN-AP-010.", complexity=0.7)
