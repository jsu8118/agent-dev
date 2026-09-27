"""Mock policies for Day 2 - Tool use & the agent loop.

Three rule-based stand-ins for Claude, each matched by a marker in the lab's system prompt
(so they never capture another day's requests):

  day2.order_desk  <day2_order_desk>  labs 01-05 and 08, the Day 2 solutions: an internal order-desk
                                      assistant with read tools (get_order_status, get_invoice,
                                      search_policies, get_shipment_tracking) and one write tool
                                      (open_logistics_case).
  day2.hitl        <day2_hitl>        lab 07: a customer-facing assistant whose write tools
                                      (update_delivery_address, cancel_order) pass a human approval gate.
  day2.extract     <day2_extract>     lab 08: structured-output extraction (the alternative to forcing a tool).

They behave like a careful tool-using model, deterministically:
  * decide from the latest question which look-ups are needed; independent look-ups are emitted as
    PARALLEL tool_use blocks in one turn, dependent ones in a later turn;
  * read is_error results and recover: switch to the tool the error names, or ask for a corrected ID
    (never guess another ID);
  * respect tool_choice: auto / any / tool / none and disable_parallel_tool_use (forced choices are
    honoured on EVERY turn, exactly the behaviour that makes a permanently forced loop never finish);
  * write the final answer only from tool results that are in the request.
Live Claude will word things differently and may order calls differently; the labs only rely on structure.
"""

from __future__ import annotations

import re
from typing import Any

from ..registry import scenario
from ..reply import Reply, json_reply, say, tool, use_tools
from ..request import MockRequest, ToolCall

ORDER_DESK = "<day2_order_desk>"
HITL = "<day2_hitl>"
EXTRACT = "<day2_extract>"

SO_ID = re.compile(r"\bSO-[0-9A-Z]{3,7}\b")
AR_ID = re.compile(r"\bAR-\d{5}\b")
MISLABELLED_ORDER = re.compile(r"\border\s+(AR-\d{5})\b", re.I)

WANTS_POLICY = re.compile(r"compensat|entitled|policy|what do we owe|rights", re.I)
WANTS_INVOICE = re.compile(r"invoice|paid|payment|\bdue\b|balance|billing", re.I)
WANTS_TRACKING = re.compile(r"track|where|\beta\b|arriv|stuck|carrier|customs|deliver|\blate\b|business days", re.I)
WANTS_CASE = re.compile(r"logistics case|chase the carrier|open a case|trace", re.I)
ASKS_IN_TRANSIT = re.compile(r"which of (these|those|them).*(transit|on the way|not (yet )?delivered)|still in transit", re.I)


# ---------------------------------------------------------------------------------------------- helpers
def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _segment(req: MockRequest) -> tuple[str, list[ToolCall]]:
    """The latest question (a user turn with text and no tool results) and the tool calls made since it."""
    msgs = req.messages
    q_idx, question = 0, ""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") != "user":
            continue
        blocks = _blocks(m.get("content"))
        if any(b.get("type") == "tool_result" for b in blocks):
            continue
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if text:
            q_idx, question = i, text
            break
    later = {b.get("id") for m in msgs[q_idx + 1:] if m.get("role") == "assistant"
             for b in _blocks(m.get("content")) if b.get("type") == "tool_use"}
    return question, [c for c in req.tool_calls if c.id in later]


def _data(call: ToolCall | None) -> dict:
    value = call.result_json() if call else None
    return value if isinstance(value, dict) else {}


def _error(call: ToolCall) -> str:
    data = _data(call)
    return str(data.get("error") or call.result or "")


def _called_with(calls: list[ToolCall], name: str, **args: Any) -> bool:
    return any(c.name == name and all(str(c.input.get(k, "")).upper() == str(v).upper() for k, v in args.items())
               for c in calls)


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def _choice(req: MockRequest) -> tuple[str, str | None, bool]:
    choice = req.tool_choice or {}
    return choice.get("type", "auto"), choice.get("name"), bool(choice.get("disable_parallel_tool_use"))


# ------------------------------------------------------------------------------------ order facts
def order_facts(data: dict) -> dict | None:
    """Normalise an order look-up result - curated JSON or raw table rows (lab 05) - to one shape."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("orders"), list):                     # verbose variant: raw rows per table
        order = data["orders"][0] if data["orders"] else {}
        ship = (data.get("shipments") or [{}])[0] or {}
        invoice = (data.get("invoices") or [{}])[0] or {}
        return {"order_id": order.get("order_id"), "status": order.get("status"),
                "promised_date": order.get("promised_date"), "shipment": ship or None,
                "invoice_id": invoice.get("invoice_id"), "account_manager": None}
    if "order_id" in data and "status" in data:
        facts = dict(data)
        if "shipment" not in facts and "tracking_number" in data:   # tracking-tool shape (exercise 9)
            facts["shipment"] = data
        facts.setdefault("shipment", None)
        return facts
    return None


def describe_order(o: dict) -> str:
    text = _describe_order_status(o)
    late = o.get("lateness") or {}                     # computed by the tracking tool (exercise 9)
    days = late.get("business_days_late")
    if isinstance(days, int) and days > 0:
        text += (f" Expected arrival {late.get('expected_arrival')} is {days} business day(s) after the promised "
                 "date.")
    elif days == 0:
        text += " It is on schedule for the promised date."
    return text


def _describe_order_status(o: dict) -> str:
    oid, status = o.get("order_id"), o.get("status") or "unknown"
    ship = o.get("shipment") or {}
    promised = o.get("promised_date")
    if status in ("pending", "confirmed", "in_production", "on_hold"):
        return (f"{oid} is {status.replace('_', ' ')} and has not shipped yet; the promised date is {promised}.")
    if status == "cancelled":
        return f"{oid} was cancelled."
    if ship.get("status") == "exception":
        return (f"{oid} shipped on {ship.get('ship_date')} with {ship.get('carrier')} (tracking "
                f"{ship.get('tracking_number')}), promised for {promised}, but the carrier reports an exception: "
                f"{ship.get('exception_reason')}.")
    if ship.get("delivered_date"):
        return (f"{oid} was delivered on {ship.get('delivered_date')} by {ship.get('carrier')} (tracking "
                f"{ship.get('tracking_number')}); the promised date was {promised}.")
    if ship:
        last = f" Last carrier scan: {ship['last_event']}." if ship.get("last_event") else ""
        return (f"{oid} shipped on {ship.get('ship_date')} with {ship.get('carrier')}, tracking number "
                f"{ship.get('tracking_number')}; the current ETA is {ship.get('eta_date')} (promised {promised}).{last}")
    return f"{oid} has status '{status}'."


def _policy_sentences(text: str, keywords: tuple[str, ...]) -> list[str]:
    out = []
    for line in re.split(r"\n|(?<=\.)\s+(?=[A-Z*])", text):
        clean = " ".join(line.replace("**", "").lstrip("*- ").split())
        if clean and any(k.lower() in clean.lower() for k in keywords):
            out.append(clean)
    return out


# ----------------------------------------------------------------------------------- order desk
def _plan(req: MockRequest, question: str, calls: list[ToolCall]) -> tuple[list[dict], str | None]:
    """Return (tool calls to make now, preface text) - empty list when ready to answer."""
    tools = req.tool_names
    mislabelled = [m.upper() for m in MISLABELLED_ORDER.findall(question)]
    orders = _unique([o.upper() for o in SO_ID.findall(question)] + mislabelled)
    invoices = [i for i in _unique(AR_ID.findall(question)) if i not in mislabelled]
    track_tool = "get_shipment_tracking" in tools and WANTS_TRACKING.search(question)
    lookup = "get_shipment_tracking" if track_tool else "get_order_status"

    # Stage 1 - look up every ID the question mentions (independent -> parallel).
    stage1: list[dict] = []
    if lookup in tools:
        stage1 += [tool(lookup, order_id=o) for o in orders if not _called_with(calls, lookup, order_id=o)]
    if "get_invoice" in tools:
        stage1 += [tool("get_invoice", invoice_id=i) for i in invoices
                   if not _called_with(calls, "get_invoice", invoice_id=i)]
    if stage1:
        ids = ", ".join(orders + invoices)
        return stage1, (f"Let me look up {ids}." if not calls else None)

    # Stage 2 - follow-ups that depend on stage-1 results.
    stage2: list[dict] = []
    facts = [(c, order_facts(_data(c))) for c in calls if c.name in ("get_order_status", "get_shipment_tracking")
             and not c.is_error]
    for c in calls:
        if not c.is_error:
            continue
        err = _error(c)
        wrong_id = str(c.input.get("order_id") or c.input.get("invoice_id") or "")
        if "get_invoice" in err and "get_invoice" in tools and AR_ID.fullmatch(wrong_id) \
                and not _called_with(calls, "get_invoice", invoice_id=wrong_id):
            stage2.append(tool("get_invoice", invoice_id=wrong_id))
        if "get_order_status" in err and "get_order_status" in tools and SO_ID.fullmatch(wrong_id) \
                and not _called_with(calls, "get_order_status", order_id=wrong_id):
            stage2.append(tool("get_order_status", order_id=wrong_id))
    # An invoice found via recovery answers "which order is this?" - look that order up if the question is "where".
    for c in calls:
        if c.name == "get_invoice" and not c.is_error and WANTS_TRACKING.search(question):
            oid = _data(c).get("order_id")
            if oid and "get_order_status" in tools and not _called_with(calls, "get_order_status", order_id=oid):
                stage2.append(tool("get_order_status", order_id=oid))
    if WANTS_INVOICE.search(question) and "get_invoice" in tools:
        for _, f in facts:
            inv = (f or {}).get("invoice_id")
            if inv and not _called_with(calls, "get_invoice", invoice_id=inv):
                stage2.append(tool("get_invoice", invoice_id=inv))
    if WANTS_POLICY.search(question) and "search_policies" in tools and not any(c.name == "search_policies"
                                                                                  for c in calls):
        exceptions = " ".join(str(((f or {}).get("shipment") or {}).get("exception_reason") or "") for _, f in facts)
        query = "late delivery compensation"
        for word in ("customs", "weather", "damage", "address"):
            if word in exceptions.lower():
                query += f" {word}" + (" hold" if word == "customs" else "")
        stage2.append(tool("search_policies", query=query))
    if stage2:
        return stage2, None

    # Stage 3 - writes come last, after the facts are known.
    if WANTS_CASE.search(question) and "open_logistics_case" in tools and not any(
            c.name == "open_logistics_case" for c in calls):
        target = next((f for _, f in facts if f), None)
        if target:
            ship = target.get("shipment") or {}
            summary = (f"{target['order_id']}: carrier exception ({ship.get('exception_reason') or 'late'}); "
                       f"promised {target.get('promised_date')}. Please chase {ship.get('carrier') or 'the carrier'} "
                       "and update the customer.")
            return [tool("open_logistics_case", order_id=target["order_id"], summary=summary)], None
    return [], None


def _answer(req: MockRequest, question: str, calls: list[ToolCall]) -> str:
    parts: list[str] = []
    recovered: set[str] = set()
    for c in calls:
        if c.name == "get_invoice" and not c.is_error:
            recovered.add(str(c.input.get("invoice_id", "")).upper())
    facts: list[dict] = []
    for c in calls:
        if c.name in ("get_order_status", "get_shipment_tracking"):
            if c.is_error:
                wrong = str(c.input.get("order_id", ""))
                err = _error(c)
                if wrong.upper() in recovered:
                    parts.append(f"{wrong} is an invoice number, not an order number.")
                elif "not found" in err:
                    parts.append(f"I couldn't find order {wrong} in Atlas ERP. Please double-check the number with "
                                 "the customer (format SO-12345) - I haven't guessed an alternative.")
                elif re.search(r"sender's account|not verified|not a verified", err):
                    parts.append(f"I can't share any details about {wrong} from this email address. If it is your "
                                 "company's order, please write to us from your company email address, or send the "
                                 "order number together with its purchase-order (PO) number.")
                else:
                    first = re.split(r"(?<=\.)\s", err, maxsplit=1)[0]
                    parts.append(f"I couldn't look up {wrong}: {first}")
                continue
            f = order_facts(_data(c))
            if f and f.get("order_id") not in [x.get("order_id") for x in facts]:
                facts.append(f)
                parts.append(describe_order(f))
        elif c.name == "get_invoice":
            d = _data(c)
            if c.is_error:
                parts.append(f"I couldn't open invoice {c.input.get('invoice_id')}: {_error(c)}")
                continue
            paid, amount = d.get("paid_amount_usd", 0) or 0, d.get("amount_usd", 0) or 0
            paid_txt = ("nothing has been paid yet" if not paid else
                        f"{_money(paid)} has been paid" + (f" (overpaid by {_money(paid - amount)})" if paid > amount
                                                           else ""))
            parts.append(f"Invoice {d.get('invoice_id')} (order {d.get('order_id')}) is for {_money(amount)}, due "
                         f"{d.get('due_date')}; status '{d.get('status')}' - {paid_txt}.")
    policy = next((c for c in calls if c.name == "search_policies" and not c.is_error), None)
    if policy is not None:
        passages = _data(policy).get("results") or []
        text = "\n".join(p.get("text", "") for p in passages)
        cite = passages[0].get("citation", "the shipping policy") if passages else "the shipping policy"
        exceptions = " ".join(str((f.get("shipment") or {}).get("exception_reason") or "").lower() for f in facts)
        outside = _policy_sentences(text, ("not within Kestrel",))
        entitled = _policy_sentences(text, ("refund of the freight",))
        if outside and any(w in exceptions for w in ("customs", "weather")):
            parts.append(f"Compensation: per {cite}, {outside[0][0].lower() + outside[0][1:]} So the late-delivery "
                         "compensation (freight refund or free expedited freight) does not apply to this delay.")
        elif entitled:
            parts.append(f"Compensation: per {cite}: {entitled[0]}")
        if "eori" in exceptions:
            eori = _policy_sentences(text, ("EORI",))
            if eori:
                parts.append(f"{eori[0]} Ask the customer to send their EORI number so the hold can clear.")
        notify = _policy_sentences(text, ("notifies the customer",))
        if notify and re.search(r"nobody|no one|not (been )?(called|contacted|told)", question, re.I):
            parts.append(f"Note that the policy says: \"{notify[0]}\" The customer says nobody contacted them, so "
                         "acknowledge that and apologise.")
    case = next((c for c in calls if c.name == "open_logistics_case"), None)
    if case is not None:
        if case.is_error:
            declined = re.search(r"DECLINED by (.+?): (.+?)\.", _error(case))
            parts.append(f"I did not open a logistics case: {declined.group(1)} declined it ({declined.group(2)}), so "
                         "logistics has not been asked to chase the carrier yet." if declined
                         else f"I could not open a logistics case: {_error(case)}")
        else:
            d = _data(case)
            parts.append(f"I opened logistics case {d.get('case_id')} for {d.get('order_id')} "
                         f"({d.get('sla', 'logistics will follow up')}).")
    if not parts:
        return ("I need an order number (SO-12345) or an invoice number (AR-12345) to look this up.")
    return " ".join(parts)


def _in_transit_summary(req: MockRequest) -> str:
    """Answer 'which of these orders are still in transit?' from every look-up earlier in the conversation."""
    facts: dict[str, dict] = {}
    for c in req.tool_calls:
        if c.name in ("get_order_status", "get_shipment_tracking") and not c.is_error:
            f = order_facts(_data(c))
            if f and f.get("order_id"):
                facts[f["order_id"]] = f
    moving = [oid for oid, f in facts.items() if (f.get("shipment") or {}).get("status") in ("in_transit", "exception")]
    others = [oid for oid in facts if oid not in moving]
    if not facts:
        return "I haven't looked up any orders in this conversation yet."
    return (f"Still in transit: {', '.join(moving) or 'none'}. Not in transit: {', '.join(others) or 'none'} "
            "(based on the look-ups earlier in this conversation).")


def _forced_call(req: MockRequest, name: str, question: str, calls: list[ToolCall]) -> dict:
    """What a model produces when tool_choice forces `name` - even if it has nothing new to ask."""
    orders = SO_ID.findall(question) or [str(c.input.get("order_id")) for c in calls if c.input.get("order_id")]
    if name in ("get_order_status", "get_shipment_tracking", "open_logistics_case"):
        args: dict[str, Any] = {"order_id": orders[0] if orders else "SO-00000"}
        if name == "open_logistics_case":
            args["summary"] = "Requested by the order desk."
        return tool(name, **args)
    if name == "get_invoice":
        found = AR_ID.findall(question)
        return tool(name, invoice_id=found[0] if found else "AR-00000")
    if name == "search_policies":
        return tool(name, query=" ".join(question.split()[:8]))
    schema_props = next((t.get("input_schema", {}).get("properties", {}) for t in req.tools if t.get("name") == name),
                        {})
    return tool(name, **{k: "" for k in schema_props})


@scenario("day2.order_desk", match=lambda r: ORDER_DESK in r.system_text, priority=20)
def order_desk(req: MockRequest) -> Reply:
    question, calls = _segment(req)
    kind, forced_name, no_parallel = _choice(req)

    if kind == "tool" and forced_name:
        return use_tools(_forced_call(req, forced_name, question, calls))

    if ASKS_IN_TRANSIT.search(question) and not SO_ID.search(question):
        if kind == "any":
            first = next((c for c in req.tool_calls if c.name == "get_order_status"), None)
            return use_tools(tool("get_order_status", order_id=(first.input.get("order_id") if first else "SO-00000")))
        return say(_in_transit_summary(req), complexity=0.3)

    planned, preface = ([], None) if kind == "none" else _plan(req, question, calls)
    if planned:
        if no_parallel:
            planned = planned[:1]
        return use_tools(*planned, preface=preface,
                         thinking="Decide which look-ups the question needs; independent ones can run in parallel.")
    if kind == "any":           # forced to use SOME tool although everything needed is known: repeat a look-up
        if calls:
            return use_tools(tool(calls[-1].name, **calls[-1].input))
        return use_tools(_forced_call(req, "get_order_status", question, calls))
    if kind == "none" and not calls:
        ids = ", ".join(SO_ID.findall(question) + AR_ID.findall(question)) or "that"
        return say(f"I can't check live data for {ids} without my look-up tools, so I won't guess a status or date. "
                   "Please look it up in Atlas ERP, or ask me again with tools enabled.", complexity=0.2)
    return say(_answer(req, question, calls), complexity=0.45,
               thinking="Compose the answer only from the tool results; quote IDs, dates and amounts exactly.")


# ----------------------------------------------------------------------------------------- HITL (lab 07)
def _new_address(text: str) -> str | None:
    m = re.search(r"(?:address|warehouse)[^:\n]*:\s*(\d+[^\n]*?)(?:\.\s|\.$|\n|$)", text, re.I)
    return m.group(1).strip().rstrip(".") if m else None


@scenario("day2.hitl", match=lambda r: HITL in r.system_text, priority=20)
def hitl(req: MockRequest) -> Reply:
    text = req.first_user_text
    calls = req.tool_calls
    orders = SO_ID.findall(text)
    if not orders:
        return say("Could you tell me the order number? It looks like SO-10234.", complexity=0.2)
    oid = orders[0]
    status = next((c for c in reversed(calls) if c.name == "get_order_status"), None)
    if status is None:
        return use_tools(tool("get_order_status", order_id=oid), preface="Let me check the order first.")
    if status.is_error:
        return say(f"I couldn't find order {oid}: {_error(status)}", complexity=0.2)
    order = _data(status)
    manager = order.get("account_manager") or "your account manager"

    if re.search(r"\bcancel", text, re.I):
        attempt = next((c for c in reversed(calls) if c.name == "cancel_order"), None)
        if attempt is None:
            sentence = next((x for x in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if "cancel" in x.lower()),
                            "")
            reason = sentence.split(" - ", 1)[1].rstrip(".") if " - " in sentence else "customer request"
            return use_tools(tool("cancel_order", order_id=oid, reason=reason),
                             thinking="Cancellation is irreversible; the tool will route it for approval.")
        if not attempt.is_error:
            d = _data(attempt)
            return say(f"Order {oid} has been cancelled as you requested (reference {d.get('cancellation_id')}). "
                       "You'll receive a cancellation confirmation by email.", complexity=0.3)
        err = _error(attempt)
        if "declined" in err.lower():
            return say(f"Thank you for letting us know. I wasn't able to cancel {oid} directly: the order is "
                       f"{str(order.get('status', '')).replace('_', ' ')} and a cancellation at this stage needs a "
                       f"review with {manager}, who will contact you to go through the options and any charges. "
                       "Until then the order remains active.", complexity=0.45,
                       thinking="The reviewer declined; do not retry. Explain the outcome and the next step.")
        return say(f"I couldn't cancel {oid}: {err}", complexity=0.3)

    new_address = _new_address(text)
    if re.search(r"address|warehouse", text, re.I):
        if order.get("status") in ("shipped", "delivered"):
            return say(f"Order {oid} has already shipped, so its delivery address can no longer be changed. Please "
                       "contact the carrier with the tracking number to ask for a redirect.", complexity=0.3)
        if not new_address:
            return say(f"Order {oid} hasn't shipped yet, so the address can still be changed - what is the full new "
                       "delivery address?", complexity=0.2)
        attempt = next((c for c in reversed(calls) if c.name == "update_delivery_address"), None)
        if attempt is None:
            return use_tools(tool("update_delivery_address", order_id=oid, new_address=new_address))
        if not attempt.is_error:
            d = _data(attempt)
            return say(f"Done: order {oid} has not shipped yet (status: {order.get('status')}), so I've updated the "
                       f"delivery address to {d.get('new_address')} (change reference {d.get('change_id')}). You'll "
                       "receive the confirmation by email.", complexity=0.3)
        err = _error(attempt)
        if "declined" in err.lower():
            return say(f"I've passed your address change for {oid} to our order desk for a manual check - it "
                       f"couldn't be applied automatically. {manager} will confirm the new delivery address with you "
                       "before the order ships.", complexity=0.4)
        return say(f"I couldn't change the address for {oid}: {err}", complexity=0.3)
    return say(describe_order(order_facts(order) or order), complexity=0.3)


# --------------------------------------------------------------------------------- extraction (lab 08)
@scenario("day2.extract", match=lambda r: EXTRACT in r.system_text and r.output_schema is not None, priority=20)
def extract(req: MockRequest) -> Reply:
    text = req.last_user_text or req.first_user_text
    if re.search(r"invoice|paid|payment|billing", text, re.I):
        intent = "billing"
    elif re.search(r"return|send back|refund", text, re.I):
        intent = "return"
    elif SO_ID.search(text) or re.search(r"where|status|track|deliver", text, re.I):
        intent = "order_status"
    else:
        intent = "other"
    return json_reply({"order_ids": _unique(SO_ID.findall(text)), "invoice_ids": _unique(AR_ID.findall(text)),
                       "intent": intent}, complexity=0.2)
