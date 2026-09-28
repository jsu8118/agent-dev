"""Production traces of the Service Desk Copilot (the base course's capstone) for Day 6: eval science and release.

480 traces over four weeks of a fictional deployment history, generated with the correlations a real
system shows: harder ticket categories fail more, higher effort fails less but costs more, a cheaper
model on a canary fails more on complex tickets, and a prompt revision (v15) fixes two failure modes while
quietly introducing a third.  Day 6 mines these traces into evals and has to find that regression.

Outputs (advanced/data/traces/):
    traces.jsonl               one trace per line (deployment arm, tools, usage, cost, outcome, optional review)
    deployments.json           the arms and their dates, as a release manager would document them
    pairwise_judgments.jsonl   human A/B preferences over reply pairs (with a second annotator on a subset)
    splits.json                ticket-level train / validation / test split
"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

from .common import AS_OF, BASE_DATA_DIR, SEED, iso, write_json, write_jsonl

PRICES = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}   # $/M in, $/M out
CACHE_READ, CACHE_WRITE = 0.1, 1.25

# category -> (tool sequence, difficulty 0..1, base input tokens, base output tokens, turns)
CATEGORIES = {
    "order_status":      (["get_customer_profile", "get_order", "list_shipments_for_order"], 0.15, 5200, 260, 3),
    "shipping_delay":    (["get_customer_profile", "get_order", "list_shipments_for_order", "track_shipment"], 0.30, 6800, 340, 4),
    "return_request":    (["get_customer_profile", "get_order", "check_return_eligibility", "create_rma"], 0.45, 7900, 420, 4),
    "warranty_claim":    (["get_customer_profile", "get_order", "check_warranty", "search_knowledge_base", "create_rma"], 0.60, 9400, 480, 5),
    "billing":           (["get_customer_profile", "get_invoice", "get_order"], 0.40, 6100, 330, 3),
    "technical_support": (["get_customer_profile", "search_knowledge_base", "search_knowledge_base", "check_warranty"], 0.65, 10800, 620, 4),
    "product_inquiry":   (["search_knowledge_base", "get_customer_profile"], 0.25, 4600, 380, 2),
    "safety_incident":   (["get_customer_profile", "escalate_to_human"], 0.20, 4100, 210, 2),
    "account_access":    (["get_customer_profile", "escalate_to_human"], 0.30, 3900, 200, 2),
    "other":             (["get_customer_profile", "escalate_to_human"], 0.35, 3600, 190, 2),
}
ISSUES = ["wrong_policy", "hallucinated_id", "missing_citation", "over_promised", "unnecessary_escalation", "tone", "incomplete"]

DEPLOYMENTS = [
    {"arm": "baseline", "model": "claude-sonnet-5", "effort": "medium", "prompt_version": "v14",
     "from": "2026-08-17", "to": "2026-09-14", "traffic": "100% until 2026-08-30, then 50%"},
    {"arm": "opus-v15", "model": "claude-opus-5", "effort": "medium", "prompt_version": "v15",
     "from": "2026-08-31", "to": "2026-09-14", "traffic": "50% (A/B against baseline)"},
    {"arm": "opus-v15-high", "model": "claude-opus-5", "effort": "high", "prompt_version": "v15",
     "from": "2026-09-07", "to": "2026-09-14", "traffic": "effort staircase, 1/3 of the opus arm"},
    {"arm": "opus-v15-low", "model": "claude-opus-5", "effort": "low", "prompt_version": "v15",
     "from": "2026-09-07", "to": "2026-09-14", "traffic": "effort staircase, 1/3 of the opus arm"},
    {"arm": "haiku-fastpath", "model": "claude-haiku-4-5", "effort": "medium", "prompt_version": "v15",
     "from": "2026-09-09", "to": "2026-09-14", "traffic": "canary: 30% of order_status and product_inquiry"},
]
EFFORT_FACTOR = {"low": (1.15, 0.65, 0.75), "medium": (1.0, 1.0, 1.0), "high": (0.70, 1.55, 1.35)}   # (error, output tokens, latency)
MODEL_FACTOR = {"claude-opus-5": 0.80, "claude-sonnet-5": 1.0, "claude-haiku-4-5": 1.55}

REPLIES = {
    "order_status": "Hi {name}, order {order} shipped on {date} with {carrier} (tracking {tracking}); delivery is expected {eta}.",
    "shipping_delay": "Hi {name}, {order} is held at {place}. I have asked the carrier to prioritise it and will update you by {eta}.",
    "return_request": "Hi {name}, I have opened {rma} for the {qty} units on {order}. A {fee}% restocking fee applies under the returns policy.",
    "warranty_claim": "Hi {name}, the unit on {order} is within warranty (ends {eta}). I have opened {rma} for an inspection and replacement.",
    "billing": "Hi {name}, invoice {invoice} for {order} shows {amount} outstanding, due {eta}. I have noted your dispute for our billing team.",
    "technical_support": "Hi {name}, fault {code} on the {family} indicates {cause}. The manual (section {section}) recommends {fix}.",
    "product_inquiry": "Hi {name}, the {family} is rated for {rating}; the datasheet is attached and the lead time is {lead} days.",
    "safety_incident": "Hi {name}, I have escalated this as a P1 safety incident to our on-call engineer, who will call you within 30 minutes.",
    "account_access": "Hi {name}, I have passed your access request to our account team; they will confirm by email within one business day.",
    "other": "Hi {name}, this is outside what I can help with here, so I have passed it to a colleague who will reply within one business day.",
}


def _load_tickets() -> list[dict]:
    tickets = {json.loads(l)["ticket_id"]: json.loads(l) for l in (BASE_DATA_DIR / "support" / "tickets.jsonl").open(encoding="utf-8")}
    labels = [json.loads(l) for l in (BASE_DATA_DIR / "support" / "ticket_labels.jsonl").open(encoding="utf-8")]
    out = []
    for lab in labels:
        t = tickets[lab["ticket_id"]]
        out.append({"ticket_id": lab["ticket_id"], "customer_id": lab.get("customer_id"), "category": lab["category"],
                    "priority": lab["priority"], "order_id": lab.get("order_id"), "from_email": t["from_email"],
                    "name": t["from_email"].split("@")[0].split(".")[0].title(), "requires_human": lab["requires_human"]})
    return out


def _arm_for(rng: random.Random, day: dt.date, category: str) -> dict:
    if day <= dt.date(2026, 8, 30):
        return DEPLOYMENTS[0]
    if day >= dt.date(2026, 9, 9) and category in ("order_status", "product_inquiry") and rng.random() < 0.30:
        return DEPLOYMENTS[4]
    if rng.random() < 0.5:
        return DEPLOYMENTS[0]
    if day >= dt.date(2026, 9, 7):
        return rng.choice([DEPLOYMENTS[1], DEPLOYMENTS[2], DEPLOYMENTS[3]])
    return DEPLOYMENTS[1]


def _issues_for(rng: random.Random, ticket: dict, arm: dict) -> list[str]:
    spec = CATEGORIES[ticket["category"]]
    p = 0.12 + 0.40 * spec[1]
    p *= EFFORT_FACTOR[arm["effort"]][0] * MODEL_FACTOR[arm["model"]]
    issues: list[str] = []
    if rng.random() < p:
        weights = {"wrong_policy": 3, "hallucinated_id": 2, "missing_citation": 3, "over_promised": 2, "tone": 1, "incomplete": 2}
        if arm["prompt_version"] == "v15":                 # v15 fixed citations and policy wording ...
            weights["missing_citation"] = 1
            weights["wrong_policy"] = 1
            weights["tone"] = 2
        issues.append(rng.choices(list(weights), weights=list(weights.values()))[0])
    if arm["prompt_version"] == "v15" and ticket["category"] == "return_request" and rng.random() < 0.22:
        issues.append("unnecessary_escalation")           # ... and introduced this regression on returns
    if arm["model"] == "claude-haiku-4-5" and ticket["category"] == "product_inquiry" and rng.random() < 0.18:
        issues.append("hallucinated_id")
    return sorted(set(issues))


def _reply(rng: random.Random, ticket: dict, issues: list[str]) -> str:
    fill = {"name": ticket["name"], "order": ticket["order_id"] or "SO-10" + str(rng.randint(100, 320)),
            "date": "2026-09-0" + str(rng.randint(1, 9)), "carrier": rng.choice(["NorthLine Freight", "SwiftParcel", "BlueRiver Logistics"]),
            "tracking": "NLF" + str(rng.randint(10**9, 10**10 - 1)), "eta": "2026-09-" + str(rng.randint(16, 28)),
            "place": rng.choice(["customs in Dublin", "the carrier's Memphis hub", "the port of Rotterdam"]),
            "rma": f"RMA-{rng.randint(3001, 3099)}", "qty": rng.randint(1, 6), "fee": rng.choice([0, 15]),
            "invoice": f"AR-{rng.randint(90100, 90300)}", "amount": f"${rng.randint(800, 24000):,}.00",
            "code": rng.choice(["F17", "E42", "F03"]), "family": rng.choice(["KP-250", "KC-2", "KP-100"]),
            "cause": rng.choice(["a seal temperature excursion", "a supply-voltage dip", "a blocked suction strainer"]),
            "section": rng.choice(["6.2", "7.4", "9.1"]), "fix": rng.choice(["checking the seal flush line", "a controlled restart", "cleaning the strainer"]),
            "rating": rng.choice(["120 m3/h at 45 m", "22 kW, IE3", "0-40 bar"]), "lead": rng.choice([3, 7, 35])}
    text = REPLIES[ticket["category"]].format(**fill)
    if "unnecessary_escalation" in issues:
        text = f"Hi {ticket['name']}, I have escalated your return request to a colleague, who will review it and reply within two business days."
    if "over_promised" in issues:
        text += " We will also refund the shipping cost in full."
    if "hallucinated_id" in issues:
        text = text.replace(fill["order"], "SO-10999").replace(fill["rma"], "RMA-4200")
    if "tone" in issues:
        text = "I am so incredibly sorry for this completely unacceptable experience. " + text
    return text


def build(out_dir: Path) -> dict:
    rng = random.Random(SEED)
    tickets = _load_tickets()
    start = dt.date(2026, 8, 17)
    traces = []
    for i in range(480):
        day = start + dt.timedelta(days=int(i * 29 / 480))
        ticket = tickets[(i * 7 + rng.randint(0, 3)) % len(tickets)]
        arm = _arm_for(rng, day, ticket["category"])
        tools, difficulty, base_in, base_out, turns = CATEGORIES[ticket["category"]]
        err_f, out_f, lat_f = EFFORT_FACTOR[arm["effort"]]
        tools = list(tools)
        if rng.random() < 0.12:
            tools.insert(rng.randint(1, len(tools)), tools[rng.randint(0, len(tools) - 1)])        # a retry / repeated call
            turns += 1
        issues = _issues_for(rng, ticket, arm)
        if ticket["requires_human"] and "escalate_to_human" not in tools:
            tools.append("escalate_to_human")
        input_tokens = int(base_in * turns / 3 * rng.uniform(0.85, 1.2))
        cache_read = int(input_tokens * (0.55 if day >= dt.date(2026, 8, 24) else 0.0) * rng.uniform(0.8, 1.0))
        cache_write = int(input_tokens * 0.12 * rng.uniform(0.5, 1.0)) if cache_read else 0
        output_tokens = int(base_out * out_f * rng.uniform(0.8, 1.3))
        thinking = int(output_tokens * {"low": 0.4, "medium": 1.2, "high": 2.6}[arm["effort"]]) if arm["model"] != "claude-haiku-4-5" else 0
        pin, pout = PRICES[arm["model"]]
        uncached = input_tokens - cache_read - cache_write
        cost = (uncached * pin + cache_read * pin * CACHE_READ + cache_write * pin * CACHE_WRITE + (output_tokens + thinking) * pout) / 1e6
        latency = int((900 * turns + (output_tokens + thinking) * {"claude-opus-5": 24, "claude-sonnet-5": 14, "claude-haiku-4-5": 7}[arm["model"]])
                      * lat_f * rng.uniform(0.8, 1.25))
        escalated = "escalate_to_human" in tools
        status = "escalated" if escalated else ("failed" if "hallucinated_id" in issues and rng.random() < 0.5 else "resolved")
        csat = None if rng.random() < 0.55 else max(1, min(5, round(rng.gauss(4.3 - 1.4 * len(issues) - (0.4 if escalated else 0), 0.7))))
        reviewed = rng.random() < 0.35
        review = None
        if reviewed:
            found = [x for x in issues if rng.random() < 0.85]          # reviewers miss some
            if not found and rng.random() < 0.05:
                found = [rng.choice(ISSUES)]                             # ... and sometimes flag a non-issue
            review = {"reviewer": rng.choice(["QA-1", "QA-2", "QA-3"]), "issues": found,
                      "score": max(1, min(5, 5 - len(found) - (1 if rng.random() < 0.15 else 0)))}
        traces.append({
            "trace_id": f"TR-{i + 1:05d}", "ts": iso(day, 7 + (i * 5) % 11, (i * 13) % 60), "ticket_id": ticket["ticket_id"],
            "customer_id": ticket["customer_id"], "category": ticket["category"], "priority": ticket["priority"],
            "deployment": {"arm": arm["arm"], "model": arm["model"], "effort": arm["effort"], "prompt_version": arm["prompt_version"]},
            "turns": turns, "tools": tools,
            "usage": {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
                      "output_tokens": output_tokens, "thinking_tokens": thinking},
            "latency_ms": latency, "cost_usd": round(cost, 5),
            "outcome": {"status": status, "csat": csat, "human_edited": bool(issues) and rng.random() < 0.7 or rng.random() < 0.08},
            "reply": _reply(rng, ticket, issues), "review": review,
            "_truth": {"issues": issues},                                # hidden ground truth; labs must not read it (tests do)
        })

    # pairwise judgments: the same ticket answered by two arms, a human picks (a subset gets a second annotator)
    pairs = []
    by_ticket: dict[str, list[dict]] = {}
    for t in traces:
        by_ticket.setdefault(t["ticket_id"], []).append(t)
    n = 0
    for ticket_id, group in sorted(by_ticket.items()):
        arms = {}
        for t in group:
            arms.setdefault(t["deployment"]["arm"], t)
        keys = sorted(arms)
        for a in range(len(keys)):
            for b in range(a + 1, len(keys)):
                if n >= 150:
                    break
                ta, tb = arms[keys[a]], arms[keys[b]]
                qa, qb = 5 - len(ta["_truth"]["issues"]), 5 - len(tb["_truth"]["issues"])
                def judge(noise: float) -> str:
                    if abs(qa - qb) < 0.5:
                        return rng.choice(["A", "B", "tie", "tie"])
                    better = "A" if qa > qb else "B"
                    return better if rng.random() > noise else ("B" if better == "A" else "A")
                n += 1
                row = {"pair_id": f"PJ-{n:04d}", "ticket_id": ticket_id, "A": {"trace_id": ta["trace_id"], "arm": keys[a], "reply": ta["reply"]},
                       "B": {"trace_id": tb["trace_id"], "arm": keys[b], "reply": tb["reply"]},
                       "judgments": [{"annotator": "H1", "preferred": judge(0.12),
                                      "reason": rng.choice(["more accurate", "cites policy", "shorter and clearer", "did not escalate needlessly",
                                                            "correct IDs", "tone"])}]}
                if rng.random() < 0.3:
                    row["judgments"].append({"annotator": "H2", "preferred": judge(0.2), "reason": rng.choice(["accuracy", "clarity", "policy"])})
                pairs.append(row)
    ticket_ids = sorted(by_ticket)
    rng.shuffle(ticket_ids)
    k1, k2 = int(len(ticket_ids) * 0.6), int(len(ticket_ids) * 0.8)
    splits = {"train": sorted(ticket_ids[:k1]), "validation": sorted(ticket_ids[k1:k2]), "test": sorted(ticket_ids[k2:]),
              "note": "Split by ticket, not by trace, so the same ticket never appears on both sides of a comparison."}

    write_jsonl(out_dir / "traces.jsonl", traces)
    write_json(out_dir / "deployments.json", {"as_of": AS_OF.isoformat(), "arms": DEPLOYMENTS,
                                               "release_notes": {"v15": "Reworded policy citations; added 'be brief' guidance; "
                                                                        "new instruction to hand off ambiguous returns to a colleague."}})
    write_jsonl(out_dir / "pairwise_judgments.jsonl", pairs)
    write_json(out_dir / "splits.json", splits)
    return {"traces": len(traces), "reviewed": sum(t["review"] is not None for t in traces), "pairs": len(pairs),
            "tickets": len(ticket_ids)}
