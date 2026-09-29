"""Mock policies for Day 1 - Durable agents.

Labs 01, 02, 06 and 07 run Kestrel's real support agent (its system prompt and its toolset), which the first
course's `kestrel.support_agent` policy already drives - nothing to add for them.  The policies below cover the
toolsets the other labs introduce:

* `adv.day1.credit`    - the goodwill-credit agent of lab 03 (get_order -> post_credit -> reply);
* `adv.day1.saga`      - the replacement saga of lab 04 (get_order -> check_return_eligibility ->
                         arrange_replacement -> escalate on failure -> reply);
* `adv.day1.approvals` - lab 05's refund flow, which must report a *declined* approval to the customer instead
                         of escalating again the way the first course's policy does for any refund error.

Like every scenario policy, they derive everything they "say" from the request: amounts come from the customer's
text, references from tool results, and an error result changes the next step - so the labs exercise the real
mechanics (a declined approval really is a tool error the model has to react to).
"""

from __future__ import annotations

import re

from labkit.mock import MockRequest, Reply, say, scenario, tool, use_tools


def _json(call) -> dict:
    data = call.result_json() if call else None
    return data if isinstance(data, dict) else {}


def _last(req: MockRequest, name: str):
    calls = req.calls(name)
    return calls[-1] if calls else None


def _money(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------------------- lab 03: goodwill credit
@scenario("adv.day1.credit", match=lambda r: "<adv_day1_credit>" in r.system_text, priority=20)
def credit_policy(req: MockRequest) -> Reply:
    text = req.first_user_text
    order_id = (re.search(r"\bSO-\d{5}\b", text) or re.search(r"SO-\d{5}", "SO-00000")).group(0)
    amount_match = re.search(r"\$(\d+(?:\.\d+)?)", text)
    amount = float(amount_match.group(1)) if amount_match else 0.0
    if not req.called("get_order"):
        return use_tools(tool("get_order", order_id=order_id), preface="Let me pull up the order first.")
    order_call = _last(req, "get_order")
    if order_call.is_error:
        return say("I couldn't open that order: " + _json(order_call).get("error", "unknown error"))
    if not req.called("post_credit"):
        return use_tools(tool("post_credit", order_id=order_id, amount_usd=amount,
                              reason=f"Goodwill credit for the late delivery of {order_id}"))
    result = _json(_last(req, "post_credit"))
    if "error" in result:
        return say(f"I was not able to apply the credit: {result['error']} I've noted it for a colleague to follow up.")
    return say(f"Done - a goodwill credit of {_money(result.get('amount_usd', amount))} (reference "
               f"{result.get('credit_id', 'pending')}) has been applied to your account for order {order_id}. "
               "It will show on your next statement.")


# ---------------------------------------------------------------------------- lab 04: replacement saga
@scenario("adv.day1.saga", match=lambda r: "<adv_day1_saga>" in r.system_text, priority=20)
def saga_policy(req: MockRequest) -> Reply:
    text = req.first_user_text
    order_id = (re.search(r"\bSO-\d{5}\b", text) or re.search(r"SO-\d{5}", "SO-00000")).group(0)
    skus = re.findall(r"\b(IMP-250-[AD]|MS-\d{3}|BRG-\d{4}|KV-\d{2}-[A-Z])\b", text)
    if not req.called("get_order"):
        return use_tools(tool("get_order", order_id=order_id), preface="Let me look at the order.")
    order_call = _last(req, "get_order")
    if order_call.is_error:
        return say("I couldn't open that order: " + _json(order_call).get("error", "unknown error"))
    order = _json(order_call)
    lines = order.get("lines") or []
    line = next((l for l in lines if l["sku"] in skus), None) or (lines[0] if lines else None)
    if line is None:
        return say(f"I couldn't find any items on order {order_id}; could you tell me the part number?")
    received = next((s for s in skus if s != line["sku"]), "the wrong part")
    if not req.called("check_return_eligibility"):
        return use_tools(tool("check_return_eligibility", order_id=order_id, sku=line["sku"], qty=line["qty"],
                              reason="wrong_item"))
    check = _json(_last(req, "check_return_eligibility"))
    if not check.get("eligible"):
        return say("I'm sorry, but we can't process this as a return: " + " ".join(check.get("reasons", [])))
    if not req.called("arrange_replacement"):
        return use_tools(tool("arrange_replacement", order_id=order_id, sku=line["sku"], qty=line["qty"],
                              received_sku=received), preface="Arranging the replacement now.")
    arranged = _json(_last(req, "arrange_replacement"))
    if "error" in arranged:
        if not req.called("escalate_to_human"):
            return use_tools(tool("escalate_to_human", queue="order_desk", priority="P2", order_id=order_id,
                                  summary=f"Wrong item shipped on {order_id} ({received} instead of {line['sku']}); "
                                          f"automatic replacement failed: {arranged['error']} Please arrange manually."))
        esc = _json(_last(req, "escalate_to_human"))
        return say(f"I'm sorry about the mix-up on order {order_id}. I wasn't able to complete the replacement "
                   f"automatically ({arranged['error'].rstrip('.')}), so I've passed it to our order desk as a "
                   f"priority case (reference {esc.get('escalation_id', 'pending')}); they will confirm the "
                   "collection and the replacement shipment by email.")
    pickup = arranged.get("pickup") or {}
    return say(f"I'm sorry about the mix-up on order {order_id}: you received {received} instead of {line['sku']}. "
               f"I've opened return {arranged.get('rma_id')} at no cost to you, reserved {line['qty']} x {line['sku']} "
               f"for a replacement shipment, and booked a collection with {pickup.get('carrier', 'our carrier')} "
               f"(reference {pickup.get('pickup_id', 'pending')}, {pickup.get('window', 'to be confirmed')}). "
               "A confirmation with the return label has been sent to you.")


# ---------------------------------------------------------------------------- lab 05: approvals
@scenario("adv.day1.approvals", match=lambda r: "<adv_day1_approvals>" in r.system_text, priority=20)
def approvals_policy(req: MockRequest) -> Reply:
    text = req.first_user_text
    rma_match = re.search(r"\bRMA-\d{4}\b", text)
    if rma_match is None:
        return say("Could you share the RMA number (it looks like RMA-7012) so I can check the refund?")
    rma_id = rma_match.group(0)
    if not req.called("get_customer_profile"):
        return use_tools(tool("get_customer_profile"), preface="Let me pull up your account.")
    if not req.called("get_rma"):
        return use_tools(tool("get_rma", rma_id=rma_id))
    rma_call = _last(req, "get_rma")
    if rma_call.is_error:
        return say("I couldn't open that return: " + _json(rma_call).get("error", "unknown error"))
    rma = _json(rma_call)
    if rma.get("status") != "received" or rma.get("refund_due_usd") is None:
        if rma.get("status") == "refunded":
            return say(f"The refund for {rma_id} has already been issued; it reaches your original payment method "
                       "within 10 business days.")
        return say(f"{rma_id} is currently '{rma.get('status', 'unknown')}'. Refunds are issued once the returned "
                   "items are received and inspected; we'll confirm as soon as that happens.")
    due = rma["refund_due_usd"]
    refund_call = _last(req, "issue_refund")
    if refund_call is None:
        return use_tools(tool("issue_refund", rma_id=rma_id, amount_usd=due,
                              reason="Returned item received and inspected; refund confirmed with the customer"))
    if not refund_call.is_error:
        refund = _json(refund_call)
        return say(f"Your refund of {_money(refund.get('amount_usd', due))} for {rma_id} has been issued (reference "
                   f"{refund.get('refund_id')}, approved by {refund.get('approved_by', 'our team')}). It will reach "
                   "your original payment method within 10 business days.")
    error = _json(refund_call).get("error", "")
    if "no decision" in error.lower() or "timed out" in error.lower():
        return say(f"The refund of {_money(due)} for {rma_id} is still awaiting approval from our finance team; "
                   "I've flagged it as overdue and you'll hear from a colleague within one business day. "
                   "Nothing has been refunded yet.")
    if error.startswith("Declined by"):
        note = error.split(":", 1)[1].split(". Tell the customer")[0].strip() if ":" in error else "not approved"
        return say(f"I'm sorry - the refund of {_money(due)} for {rma_id} could not be approved at this time "
                   f"({note}). A member of our team will contact you about the next steps; nothing has been "
                   "charged or refunded in the meantime.")
    return say(f"I couldn't issue the refund for {rma_id} yet: {error}")
