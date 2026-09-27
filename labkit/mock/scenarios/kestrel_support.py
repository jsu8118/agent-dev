"""Mock policy for Kestrel's reference support agent (kestrel.support_agent + kestrel.support_tools).

A deterministic, rule-based stand-in for Claude so the support agent, the ticket-inbox labs
and the Day 6 eval harness run offline.  It reads the customer's email, decides which tools
to call (one step per turn, re-derived from the conversation each time), and writes the final
reply ONLY from tool results.  Like a real model it *tries* actions and reacts to tool errors
(e.g. it attempts issue_refund and escalates when the tool refuses) - which is exactly what
lets an eval catch a regression in the tool's policy code.
"""

from __future__ import annotations

import json
import re

from ..registry import scenario
from ..reply import Reply, say, tool, use_tools
from ..request import MockRequest

SKU_RE = re.compile(r"\b(KP-\d{3}-[A-Z]|KV-\d{2}-[A-Z]|KC-1-FAN|KC-[12]|MS-\d{3}|IMP-250-[AD]|BRG-\d{4}|CPL-250|"
                    r"GSK-KIT-250|ORK-100|LUB-EP2|FLT-SUC-100|VS-10|PT-40|FT-60)\b")
WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10, "twelve": 12}
INJECTION = re.compile(r"ignore (all )?(previous|prior) instructions|system override|admin(istrator)? mode|"
                       r"pre-approved|skip verification|note to the ai|ai system|iban|bank details", re.I)
SAFETY = re.compile(r"acid|chemical leak|hazard|smoke|burning|fire pump|injur|evacuat|residents|zone 1|atex|"
                    r"\bf17\b|sprinkler|won't restart|will not restart", re.I)
OUT_OF_SCOPE = re.compile(r"limerick|poem|joke|write me a|song|ex-boss|recipe", re.I)


def _json(call) -> dict:
    data = call.result_json() if call else None
    return data if isinstance(data, dict) else {}


def _last(req: MockRequest, name: str):
    calls = req.calls(name)
    return calls[-1] if calls else None


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _qty(text: str, default: int) -> int:
    m = re.search(r"\b(\d+)\s+of\s+(?:the\s+)?\d+\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(one|two|three|four|five|six|eight|ten|twelve)\s+of\b", text, re.I)
    if m:
        return WORD_NUM[m.group(1).lower()]
    m = re.search(r"\b(?:return|send back)\s+(?:the\s+)?(\d+)\b", text, re.I) or \
        re.search(r"\b(\d+)\s+(?:unopened|unused|new)\b", text, re.I)
    if m:
        return int(m.group(1))
    return default


def _po(text: str) -> str | None:
    m = re.search(r"\bPO(?:\s+number)?(?:\s+is|:|#)?\s+([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)", text)
    return m.group(1) if m else None


def _step(name: str, **kw) -> Reply:
    return use_tools(tool(name, **kw))


def _profile_step(req: MockRequest) -> Reply | None:
    if not req.called("get_customer_profile"):
        return use_tools(tool("get_customer_profile"), preface="Let me pull up your account.")
    return None


# ---------------------------------------------------------------- intents
def _security(req: MockRequest, text: str) -> Reply:
    if not req.called("escalate_to_human"):
        return _step("escalate_to_human", queue="security", priority="P2",
                     summary="Message contains instructions aimed at the AI assistant and/or a payment/refund demand "
                             "that bypasses policy. Not acted on.")
    esc = _json(_last(req, "escalate_to_human"))
    extra = ""
    if re.search(r"wrong (size|item)|received", text, re.I):
        extra = (" Regarding the coupling: please reply with the order number and a photo of the part label, and we "
                 "will check the order and arrange the correct part through our normal returns process.")
    return say("Thank you for your message. We are not able to act on the payment or refund instructions it contains; "
               f"the request has been referred to the appropriate team for review (reference "
               f"{esc.get('escalation_id', 'pending')}).{extra}")


def _safety(req: MockRequest, text: str, orders: list[str]) -> Reply:
    if not req.called("escalate_to_human"):
        summary = "Safety/critical incident reported by customer: " + " ".join(text.split())[:300]
        kw = {"order_id": orders[0]} if orders else {}
        return use_tools(tool("escalate_to_human", queue="field_service", priority="P1", summary=summary, **kw),
                         preface="This needs our on-call engineer immediately.")
    esc = _json(_last(req, "escalate_to_human"))
    return say("Please follow your site safety procedures now: keep people clear of the area and isolate and "
               "de-energize the equipment (lockout/tagout). Do not attempt repairs. I have escalated this as a P1 "
               f"incident (reference {esc.get('escalation_id')}); our on-call field service engineer will contact you "
               "within 1 hour, 24/7.")


def _refund_rma(req: MockRequest, rma_id: str) -> Reply:
    if not req.called("get_rma"):
        return _step("get_rma", rma_id=rma_id)
    rma = _json(_last(req, "get_rma"))
    if rma.get("status") != "received" or rma.get("refund_due_usd") is None:
        if rma.get("status") == "refunded":
            return say(f"The refund for {rma_id} has already been issued; it reaches your original payment method "
                       "within 10 business days.")
        return say(f"{rma_id} is currently '{rma.get('status', 'unknown')}'. Refunds are issued once the returned items "
                   "are received and inspected; we'll confirm as soon as that happens.")
    due = rma["refund_due_usd"]
    refund_call = _last(req, "issue_refund")
    if refund_call is None:
        return _step("issue_refund", rma_id=rma_id, amount_usd=due, reason="Returned items received and inspected")
    if not refund_call.is_error:
        refund = _json(refund_call)
        return say(f"Your refund of {_money(refund.get('amount_usd', due))} for {rma_id} has been issued (reference "
                   f"{refund.get('refund_id')}). It will reach your original payment method within 10 business days.")
    error = _json(refund_call).get("error", "")
    queue = "finance" if "Finance" in error else "support_manager"
    if not req.called("escalate_to_human"):
        return _step("escalate_to_human", queue=queue, priority="P3", order_id=rma.get("order_id"),
                     summary=f"Refund of {_money(due)} due on {rma_id} exceeds the agent approval limit; "
                             "please approve.")
    esc = _json(_last(req, "escalate_to_human"))
    approver = "Finance Director" if queue == "finance" else "Support Manager"
    return say(f"We received and inspected the return under {rma_id}. The refund due is {_money(due)} (the returned "
               f"line value less the 15% restocking fee for non-defective returns). Because it is above $2,500, it needs "
               f"approval from our {approver}; I've submitted it for approval (reference {esc.get('escalation_id')}). "
               "Once approved, the refund reaches your original payment method within 10 business days.")


def _invoice(req: MockRequest, invoice_id: str, text: str) -> Reply:
    if not req.called("get_invoice"):
        return _step("get_invoice", invoice_id=invoice_id)
    inv_call = _last(req, "get_invoice")
    if inv_call.is_error:
        return say("I couldn't open that invoice: " + _json(inv_call).get("error", ""))
    inv = _json(inv_call)
    if re.search(r"twice|duplicate|double", text, re.I) or inv.get("overpaid_usd"):
        if not req.called("escalate_to_human"):
            return _step("escalate_to_human", queue="finance", priority="P3", order_id=inv.get("order_id"),
                         summary=f"Duplicate payment on {invoice_id}: overpaid {_money(inv.get('overpaid_usd', 0))}. "
                                 "Please verify and refund or credit.")
        esc = _json(_last(req, "escalate_to_human"))
        return say(f"Thanks for flagging this. Our records show invoice {invoice_id} ({_money(inv['amount_usd'])}) was "
                   f"paid twice - a duplicate payment of {_money(inv['overpaid_usd'])}. I've passed it to our Finance "
                   f"team to return the duplicate (reference {esc.get('escalation_id')}); they'll confirm the refund "
                   "or credit within 1 business day.")
    return say(f"Invoice {invoice_id} is for {_money(inv['amount_usd'])}, issued {inv['issue_date']} and due "
               f"{inv['due_date']}. Our records show {_money(inv['paid_amount_usd'])} paid, so the status is "
               f"'{inv['status']}'. If you have a remittance for this invoice, please send it and our billing team "
               "will match it.")


def _prepaid_refund(req: MockRequest, order_id: str) -> Reply:
    if not req.called("get_order"):
        return _step("get_order", order_id=order_id)
    order = _json(_last(req, "get_order"))
    invoice_id = (order.get("invoice") or {}).get("invoice_id")
    if invoice_id and not req.called("get_invoice"):
        return _step("get_invoice", invoice_id=invoice_id)
    inv = _json(_last(req, "get_invoice"))
    paid = inv.get("paid_amount_usd", 0.0)
    if not req.called("escalate_to_human"):
        return _step("escalate_to_human", queue="finance", priority="P3", order_id=order_id,
                     summary=f"Order {order_id} cancelled before shipment; prepayment of {_money(paid)} to be refunded "
                             "(above $10,000: Finance Director approval).")
    esc = _json(_last(req, "escalate_to_human"))
    return say(f"Order {order_id} is cancelled and our records show your prepayment of {_money(paid)} on invoice "
               f"{invoice_id}. Because it exceeds $10,000, the refund must be approved by our Finance Director; I've sent "
               f"it to Finance for approval (reference {esc.get('escalation_id')}). You'll receive confirmation once it "
               "is processed.")


def _address_change(req: MockRequest, order_id: str) -> Reply:
    if not req.called("get_order"):
        return _step("get_order", order_id=order_id)
    order = _json(_last(req, "get_order"))
    if order.get("status") in ("shipped", "delivered"):
        return say(f"Order {order_id} has already shipped, so the delivery address can no longer be changed. Please "
                   "contact the carrier with the tracking number to request a redirect.")
    if not req.called("escalate_to_human"):
        return _step("escalate_to_human", queue="order_desk", priority="P3", order_id=order_id,
                     summary="Customer requests a delivery address change before shipment: " +
                             " ".join(req.first_user_text.split())[:200])
    esc = _json(_last(req, "escalate_to_human"))
    return say(f"Good news: order {order_id} has not shipped yet (status: {order.get('status')}), so the address can "
               f"still be changed. I've asked our order desk to update it (reference {esc.get('escalation_id')}); "
               "they'll confirm by email.")


def _return(req: MockRequest, order_id: str, text: str) -> Reply:
    if not req.called("get_order"):
        return _step("get_order", order_id=order_id)
    order_call = _last(req, "get_order")
    if order_call.is_error:
        return say(_json(order_call).get("error", "I couldn't find that order."))
    order = _json(order_call)
    lines = order.get("lines", [])
    skus = SKU_RE.findall(text)
    line = next((l for l in lines if l["sku"] in skus), None) or (lines[0] if lines else None)
    if line is None:
        return say(f"I couldn't find any items on order {order_id}.")
    if re.search(r"damag|crack|crush|broken", text, re.I):
        reason = "damaged_in_transit"
    elif re.search(r"wrong|received .*instead|but received|received bronze", text, re.I):
        reason = "wrong_item"
    else:
        reason = "no_longer_needed"
    qty = min(_qty(text, line["qty"]), line["qty"])
    if not req.called("check_return_eligibility"):
        return _step("check_return_eligibility", order_id=order_id, sku=line["sku"], qty=qty, reason=reason)
    check = _json(_last(req, "check_return_eligibility"))
    if not check.get("eligible"):
        reasons = " ".join(check.get("reasons", []))
        alt = (" The controllers remain covered by our 12-month warranty if they develop a fault." if "Configured" in
               reasons else "")
        return say(f"I'm sorry, but we can't accept this return. {reasons}{alt}")
    if not req.called("create_rma"):
        return _step("create_rma", order_id=order_id, sku=line["sku"], qty=qty, reason=reason,
                     notes=" ".join(text.split())[:200])
    rma = _json(_last(req, "create_rma"))
    d = check.get("details", {})
    if reason == "no_longer_needed":
        money = (f" A 15% restocking fee applies to non-defective returns, so the refund after inspection will be "
                 f"{_money(d.get('refund_after_inspection_usd', 0))} (line value {_money(d.get('line_value_usd', 0))}, "
                 f"fee {_money(d.get('restocking_fee_usd', 0))}).")
        ship = " Please ship the items unused in their original packaging within 15 days, quoting the RMA number."
    elif reason == "wrong_item":
        money = " There is no restocking fee, and we'll send a prepaid return label."
        ship = f" The correct {line['sku']} will ship as soon as possible - you don't need to wait for the return."
    else:
        money = " There is no fee, and we'll file the claim with the carrier ourselves."
        ship = " Replacements ship immediately; no need to wait for the damaged items to come back."
    return say(f"I've approved your return under {rma.get('rma_id')} for {qty} x {line['sku']} from order "
               f"{order_id}.{money}{ship}")


def _warranty(req: MockRequest, order_id: str, text: str) -> Reply:
    if not req.called("get_order"):
        return _step("get_order", order_id=order_id)
    order = _json(_last(req, "get_order"))
    lines = order.get("lines", [])
    skus = SKU_RE.findall(text)
    line = next((l for l in lines if l["sku"] in skus), None)
    if line is None:
        family = next((f for f in ("KP-100", "KP-250", "KP-400", "KP-600", "KC-2", "KC-1") if f in text), None)
        line = next((l for l in lines if family and l["sku"].startswith(family)), None) or (lines[0] if lines else None)
    if line is None:
        return say(f"I couldn't find the product on order {order_id}; could you tell me the part number?")
    if not req.called("check_warranty"):
        return _step("check_warranty", order_id=order_id, sku=line["sku"])
    warranty = _json(_last(req, "check_warranty"))
    details = warranty.get("details", {})
    if not warranty.get("eligible"):
        return say(f"I checked order {order_id}: the {line['sku']} shipped on {details.get('ship_date')} and its "
                   f"warranty expired on {details.get('warranty_end')}, so this failure is outside the warranty period. "
                   "We can still help: our service team can quote a repair or replacement parts (for example a bearing "
                   "kit) - would you like a quote?")
    qty = 2 if re.search(r"\bboth\b", text, re.I) else 1
    qty = min(qty, line["qty"])
    if not req.called("create_rma"):
        return _step("create_rma", order_id=order_id, sku=line["sku"], qty=qty, reason="warranty_claim",
                     notes=" ".join(text.split())[:200])
    rma = _json(_last(req, "create_rma"))
    msg = (f"I've opened a warranty claim for {qty} x {line['sku']} from order {order_id} under {rma.get('rma_id')}. The "
           f"unit is within warranty until {details.get('warranty_end')}, and final coverage is confirmed after our "
           "technicians inspect it.")
    if re.search(r"\bF10\b|dry", text, re.I):
        msg += (" Because the controller logged F10 (dry-run protection), the inspection will check for dry-running "
                "damage, which the warranty excludes; a manufacturing defect is covered.")
    if details.get("advance_replacement_eligible"):
        msg += " As a strategic account you qualify for an advance replacement, which we'll ship before the failed unit is returned."
    msg += " We'll send a prepaid return label; please quote the RMA number on the shipment."
    return say(msg)


KNOWLEDGE_TOPICS = [
    (re.compile(r"grease|regreas|lubric", re.I), "KP-250 bearing regreasing interval grease",
     lambda t: "2,000" in t and "15 g" in t,
     "Per the KP-250 IOM manual (section 6), regrease both bearings every 2,000 operating hours with 15 g of LUB-EP2 "
     "each. Don't over-grease - excess grease raises bearing temperature."),
    (re.compile(r"KP-400.*mm/s|mm/s.*KP-400|split-case.*vibration", re.I | re.S), "KP-400 vibration limits flexible",
     lambda t: "3.5" in t and "7.1" in t,
     "For a KP-400 on a flexible (steel-skid) foundation, the IOM-KP400 limits are: normal up to 3.5 mm/s, alert 3.5-7.1 "
     "mm/s, alarm/shutdown above 7.1 mm/s. At 4.1 mm/s you're in the alert zone: no shutdown needed, but investigate "
     "within a week - check the trend, bearing temperature, alignment, and the suction piping."),
    (re.compile(r"\bF05\b|overheat", re.I), "KC-1 F05 drive overtemperature fan heatsink",
     lambda t: "F05" in t,
     "F05 is drive overtemperature (KC-1 manual, fault table). Check the cooling fan first - replace the KC-1-FAN module "
     "if it doesn't run - then clean the heatsink and filters and check that the ambient temperature stays below 40 °C "
     "(application note AN-KC1-05: most afternoon F05 events were fixed by a new fan and a clean heatsink; others needed "
     "better ventilation)."),
    (re.compile(r"gravel|jumping|cavitat|noisy|ruido|noise", re.I), "cavitation noise gravel suction strainer NPSH",
     lambda t: "gravel" in t.lower() or "Cavitation" in t,
     "Noise like gravel with a jumping pressure gauge is the classic sign of cavitation (IOM-KP250 §7.2). Check the "
     "suction side: clean the suction strainer (FLT-SUC-100), make sure the suction valve is fully open, check the "
     "suction tank level and liquid temperature, and move the duty point toward the best-efficiency point."),
    (re.compile(r"runs? hot|90 ?°?C|bearing housing", re.I), "bearing overheating over-greasing",
     lambda t: "over-greas" in t.lower(),
     "The most common cause of hot bearings right after a bearing change is over-greasing (IOM-KP250 §7.5): remove "
     "the excess grease and run the pump for 2 hours with the vent plug out. Also check alignment and that C3-clearance "
     "bearings were used."),
    (re.compile(r"elastomer|toluene|solvent", re.I), "seal elastomer compatibility FKM solvent",
     lambda t: "FKM" in t,
     "Per the KP-250 manual (seal leakage table), use FKM elastomers for oils and many solvents rather than the "
     "standard EPDM; please confirm toluene compatibility against the materials chart before ordering."),
    (re.compile(r"modbus|registers?", re.I), "KC-2 fault history Modbus registers diagnostic log",
     lambda t: "41000" in t,
     "Yes. The controller stores the last 32 faults; read them over Modbus from registers 41000-41255 (KC-1/KC-2 manual, "
     "section 6)."),
    (re.compile(r"\bHz\b|energy", re.I), "minimum frequency P02 minimum continuous flow",
     lambda t: "25 Hz" in t or "minimum continuous flow" in t.lower(),
     "Be careful: the KC-1's default minimum frequency (P02) is 25 Hz to keep the pump above its minimum continuous "
     "flow (25% of BEP for the KP-250). Running at 30 Hz is acceptable only if the flow stays above that minimum; long "
     "periods below it cause recirculation, heating and vibration."),
]


def _knowledge(req: MockRequest, text: str) -> Reply | None:
    topic = next((t for t in KNOWLEDGE_TOPICS if t[0].search(text)), None)
    if topic is None:
        return None
    if not req.called("search_knowledge_base"):
        return use_tools(tool("search_knowledge_base", query=topic[1]), preface="Let me check the manuals.")
    found = _json(_last(req, "search_knowledge_base"))
    corpus = " ".join(r.get("text", "") for r in found.get("results", []))
    if topic[2](corpus):
        return say(topic[3], complexity=0.4)
    return say("I couldn't find a definitive answer in our manuals; I've noted your question for a technical "
               "specialist, who will follow up.")


def _order_status(req: MockRequest, order_id: str, text: str, po: str | None) -> Reply:
    if not req.called("get_order"):
        kw = {"customer_po": po} if po else {}
        return _step("get_order", order_id=order_id, **kw)
    call = _last(req, "get_order")
    if call.is_error:
        err = _json(call).get("error", "")
        if "not verified" in err:
            return say("I'd be glad to help. Because this message didn't come from an email address on the account, "
                       "I first need to verify it: please send the order number together with your company's purchase "
                       "order (PO) number for that order, or write to us from your company email address.")
        return say(err)
    order = _json(call)
    ship = order.get("shipment") or {}
    status = order.get("status")
    if re.search(r"late|compensation", text, re.I) and ship.get("delivered_date"):
        delivered, promised = ship["delivered_date"], order.get("promised_date")
        on_time = promised and delivered <= promised
        verdict = (f"which was on or before the promised date of {promised}, so the late-delivery compensation in our "
                   "shipping policy doesn't apply." if on_time else
                   f"after the promised date of {promised}. Under our shipping policy, if a delivery is more than 5 "
                   "business days late for reasons within our control you may request a freight refund or free "
                   "expedited freight on your next order.")
        return say(f"I checked order {order_id}: it was delivered on {delivered}, {verdict} If there's something else "
                   "we can do to help plan your next deliveries, let me know.")
    if status in ("confirmed", "pending", "in_production"):
        return say(f"Order {order_id} is {status.replace('_', ' ')} and has not shipped yet. The promised date is "
                   f"{order.get('promised_date')}. We'll send the tracking number as soon as it ships.")
    if status == "cancelled":
        return say(f"Order {order_id} was cancelled. {order.get('notes') or ''}".strip())
    if ship.get("status") == "exception":
        return say(f"Order {order_id} shipped on {ship.get('ship_date')} with {ship.get('carrier')} (tracking "
                   f"{ship.get('tracking_number')}), but the carrier has reported an exception: "
                   f"{ship.get('exception_reason')}. I'll keep you updated as soon as it moves again.")
    if ship.get("status") == "delivered":
        return say(f"Order {order_id} was delivered on {ship.get('delivered_date')} by {ship.get('carrier')} "
                   f"(tracking {ship.get('tracking_number')}).")
    return say(f"Order {order_id} shipped on {ship.get('ship_date')} with {ship.get('carrier')}, tracking number "
               f"{ship.get('tracking_number')}. The current ETA is {ship.get('eta_date')}.")


def respond_support(req: MockRequest) -> Reply:
    text = req.first_user_text
    orders = re.findall(r"\bSO-\d{5}\b", text)
    rmas = re.findall(r"\bRMA-\d{4}\b", text)
    invoices = re.findall(r"\bAR-\d{5}\b", text)
    po = _po(text)

    if INJECTION.search(text):
        return _security(req, text)
    if SAFETY.search(text):
        return _safety(req, text, orders)
    if OUT_OF_SCOPE.search(text):
        return say("Thanks for reaching out! I can only help with Kestrel products, orders and services, so I'll have "
                   "to pass on that one. If there's anything about your equipment or orders I can do, just ask.",
                   complexity=0.1)

    step = _profile_step(req)
    if step:
        return step
    profile = _json(_last(req, "get_customer_profile"))

    if not profile.get("verified") and not (orders and po):
        return say("I'd be glad to help. Because this message came from an email address that isn't on your company's "
                   "account, I need to verify the request first: please reply with the order number and your company's "
                   "purchase order (PO) number for that order, or write to us from your company email address.")

    if rmas and re.search(r"refund|money back|processed", text, re.I):
        return _refund_rma(req, rmas[0])
    if invoices:
        return _invoice(req, invoices[0], text)
    if orders and re.search(r"cancel+ed", text, re.I) and re.search(r"refund|prepa", text, re.I):
        return _prepaid_refund(req, orders[0])
    if orders and re.search(r"change the delivery address|new warehouse|delivery address", text, re.I):
        return _address_change(req, orders[0])
    if orders and re.search(r"\breturn|send back|over-?ordered|didn't need|damag|cracked|wrong|instead of|"
                            r"received bronze|can't use", text, re.I) and not re.search(r"warranty|F20|leak", text, re.I):
        return _return(req, orders[0], text)
    if orders and re.search(r"warranty|leak|seized|\bF20\b|replace|pitting|failed", text, re.I):
        return _warranty(req, orders[0], text)
    knowledge = _knowledge(req, text)
    if knowledge is not None:
        return knowledge
    if orders:
        return _order_status(req, orders[0], text, po)
    if rmas:
        return _refund_rma(req, rmas[0])
    return say("Thanks for your message. Could you share the order number (it looks like SO-10234) so I can look into "
               "this for you?", complexity=0.2)


@scenario("kestrel.support_agent",
          match=lambda r: r.has_tool("get_customer_profile") and r.has_tool("escalate_to_human"), priority=10)
def kestrel_support(req: MockRequest) -> Reply:
    reply = respond_support(req)
    # Like a real model, never call a tool the request didn't offer (e.g. an agent run with a read-only toolset).
    missing = [b["name"] for b in reply.content if b.get("type") == "tool_use" and not req.has_tool(b["name"])]
    if missing:
        action = missing[0].replace("_", " ")
        return say(f"I can't complete the next step ({action}) from here, so I've noted your request for a colleague, "
                   "who will confirm it with you.")
    return reply
