"""Lab 05 - Evaluator-optimizer: draft a supplier dispute email, grade it against a rubric, revise.

Objective
    INV-07 bills MS-250 seals at EUR 248.00 against a PO price of EUR 236.00 (+5.08%, tolerance +1%).
    A generator drafts the dispute email to Precision Seals from facts that CODE computed (extraction
    + PO + goods receipt); an evaluator grades each draft against a rubric (facts correct, policy
    compliant, tone, clear ask) and returns a critique; the generator revises until the draft passes
    or the round limit is hit. A deterministic fact check can veto any draft the judge lets through.

Concepts
    Evaluator-optimizer; rubric design (independently gradeable criteria); structured judge output;
    separating generator and evaluator (different prompt, different model); code checks alongside an
    LLM judge; stop conditions (pass / max rounds / no progress); the cost of each extra round.

Run
    python day4_workflows_multi_agent/labs/05_evaluator_optimizer.py [--max-rounds 3]
        [--judge-model claude-sonnet-5]

What to observe
    * Every round prints the draft, the judge's per-criterion scores and critique, and the code check.
    * The loop stops as soon as the draft passes - extra rounds are pure cost.
    * In mock mode the stand-in generator writes a weak first draft on purpose (hostile tone, vague
      ask, a wrong percentage) so the loop has something to fix; live Claude's first draft often
      passes in round 1 - measuring how often the loop changes the outcome is part of the lesson.
"""

# test: expect=PASSED in round
# test: expect=facts_correct

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from _ap import extract, load_invoices, load_master
from _common import Tally, effort_kwargs, mock_note, parsed, table, text_blocks
from labkit import MID_MODEL, MODEL, get_client, header, step, wrap

Criterion = Literal["facts_correct", "policy_compliant", "tone", "clear_ask"]
PASS_SCORE = 4


class CriterionScore(BaseModel):
    criterion: Criterion
    score: int = Field(description="1 (fails) to 5 (excellent)")
    comment: str


class Evaluation(BaseModel):
    scores: list[CriterionScore]
    passed: bool = Field(description=f"True only if every criterion scores {PASS_SCORE} or more")
    critique: list[str] = Field(description="Specific, actionable edits for the writer; empty if passed")


RUBRIC = """\
facts_correct     Every number, date, identifier and name matches <dispute_facts>; no invented facts.
policy_compliant  States that the invoice is on hold under Kestrel's price-tolerance policy (PO price + 1%);
                  does not promise payment of the disputed amount; reveals no internal exception codes,
                  system names or that software drafted the email.
tone              Firm, professional and courteous; no accusations, threats or sarcasm.
clear_ask         One concrete requested action (credit note for the overcharge OR a corrected invoice at the
                  PO price), with the amount and a reply-by date; says how to reply."""

WRITER_SYSTEM = """<day4_dispute_writer>
You write emails from Kestrel Pumps & Controls' accounts-payable team to suppliers. Use ONLY the facts in
<dispute_facts>; never invent amounts, dates or names. Plain text, under 180 words, with a subject line.
When a <critique> is provided, revise the previous draft to address every point and change nothing else.
</day4_dispute_writer>"""

JUDGE_SYSTEM = """<day4_dispute_judge>
You grade a draft supplier email for Kestrel's accounts-payable team against this rubric. Score each
criterion 1-5 independently; check every figure against <dispute_facts>. passed is true only if every
criterion scores {pass_score} or more. Critique items must be specific edits the writer can make.

<rubric>
{rubric}
</rubric>
</day4_dispute_judge>"""


def dispute_facts(client, model: str, tally: Tally) -> dict:
    """Facts come from code: the extraction step plus master data - the writer computes nothing."""
    master = load_master()
    raw = next(text for name, text in load_invoices() if name.startswith("INV-07"))
    inv = extract(client, raw, model=model, effort="low", tally=tally)
    po = master.purchase_orders[inv.po_number]
    supplier = master.suppliers[po["supplier_id"]]
    line, po_line = inv.lines[0], po["lines"][0]
    grn = master.receipts[po["po_number"]][0]
    invoiced, agreed = Decimal(str(line.unit_price)), Decimal(str(po_line["unit_price"]))
    qty = Decimal(str(line.quantity))
    return {
        "supplier": supplier["name"], "supplier_email": supplier["remit_email"],
        "invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date,
        "po_number": po["po_number"], "po_date": po["order_date"], "buyer": po["buyer"],
        "part": line.part_number, "description": po_line["description"], "quantity": int(qty), "uom": po_line["uom"],
        "currency": inv.currency, "invoiced_unit_price": f"{invoiced:.2f}", "po_unit_price": f"{agreed:.2f}",
        "variance_per_unit": f"{invoiced - agreed:.2f}", "variance_pct": f"{(invoiced / agreed - 1) * 100:.2f}",
        "price_tolerance_pct": "1", "invoice_total": f"{invoiced * qty:,.2f}",
        "total_at_po_price": f"{agreed * qty:,.2f}", "overcharge_total": f"{(invoiced - agreed) * qty:,.2f}",
        "goods_receipt": f"{grn['grn_id']} on {grn['received_date']}: "
                         f"{grn['lines'][0]['qty_received']} {po_line['uom']} received",
        "payment_terms": supplier["payment_terms"], "reply_by": "2026-09-22", "sender": "Kestrel Accounts Payable",
    }


# ----------------------------------------------------------------------------- the deterministic veto
MONEY_RE = re.compile(r"(?:EUR|€|USD|\$)\s?([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s?(?:EUR|€)")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")


def code_check(draft: str, facts: dict) -> list[str]:
    """Every amount and percentage in the draft must be one of the facts; key identifiers must be present."""
    money_facts = {Decimal(v.replace(",", "")) for k, v in facts.items()
                   if k.endswith(("price", "total", "per_unit")) and isinstance(v, str)}
    variance = Decimal(facts["variance_pct"])
    pct_facts = {Decimal(facts["price_tolerance_pct"]), variance, variance.quantize(Decimal("0.1")),
                 variance.quantize(Decimal("1")), Decimal(100), Decimal(101)}      # "100% received", "101% of PO"
    problems, amounts = [], set()
    for m in MONEY_RE.finditer(draft):
        value = Decimal((m.group(1) or m.group(2)).replace(",", "").rstrip("."))
        amounts.add(value)
        if value not in money_facts:
            problems.append(f"amount {m.group(0).strip()} is not one of the dispute facts")
    for m in PCT_RE.finditer(draft):
        if Decimal(m.group(1)) not in pct_facts:
            problems.append(f"percentage {m.group(0)} does not match the facts (variance {facts['variance_pct']}%, "
                            f"tolerance {facts['price_tolerance_pct']}%)")
    for key in ("invoice_number", "po_number"):
        if facts[key] not in draft:
            problems.append(f"{key} {facts[key]} is missing")
    if Decimal(facts["overcharge_total"].replace(",", "")) not in amounts:
        problems.append(f"the overcharge ({facts['currency']} {facts['overcharge_total']}) is not stated")
    return problems


def write(client, facts: dict, previous: str | None, critique: list[str], tally: Tally) -> str:
    content = "<dispute_facts>\n" + json.dumps(facts, indent=1) + "\n</dispute_facts>\n\n"
    if previous is None:
        content += "Write the dispute email for this invoice."
    else:
        content += ("<previous_draft>\n" + previous + "\n</previous_draft>\n\n<critique>\n- " + "\n- ".join(critique) +
                    "\n</critique>\n\nRevise the draft.")
    response = client.messages.create(model=MODEL, max_tokens=8000, system=WRITER_SYSTEM,
                                      messages=[{"role": "user", "content": content}], **effort_kwargs(MODEL, "medium"))
    tally.add(response)
    return text_blocks(response)


def judge(client, facts: dict, draft: str, model: str, tally: Tally) -> Evaluation:
    response = client.messages.parse(
        model=model, max_tokens=6000, system=JUDGE_SYSTEM.format(pass_score=PASS_SCORE, rubric=RUBRIC),
        messages=[{"role": "user", "content": "<dispute_facts>\n" + json.dumps(facts, indent=1) +
                   "\n</dispute_facts>\n\n<draft>\n" + draft + "\n</draft>\n\nGrade the draft."}],
        output_format=Evaluation, **effort_kwargs(model, "low"))
    tally.add(response)
    return parsed(response, "judge")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--judge-model", default=MID_MODEL, help="evaluator model (a different model than the writer)")
    args = parser.parse_args()

    header("Lab 05 - Evaluator-optimizer: a supplier dispute email for INV-07's price variance")
    mock_note("the stand-in writer's first draft is deliberately weak so the loop has work to do; the judge and "
              "the code check really read each draft.")
    client = get_client()
    writer_t, judge_t, extract_t = Tally("writer"), Tally("judge"), Tally("extraction")

    step(1, "Facts from code (extraction + PO + goods receipt), not from the writer")
    facts = dispute_facts(client, MODEL, extract_t)
    print(table([[k, v] for k, v in facts.items()], ["fact", "value"]))

    draft, critique, history = None, [], []
    for round_no in range(1, args.max_rounds + 1):
        step(f"2.{round_no}", f"Round {round_no}: {MODEL} writes, {args.judge_model} grades")
        draft = write(client, facts, draft, critique, writer_t)
        print(wrap(draft, "    | "))
        evaluation = judge(client, facts, draft, args.judge_model, judge_t)
        problems = code_check(draft, facts)
        rows = [[s.criterion, s.score, s.comment] for s in evaluation.scores]
        print(table(rows, ["criterion", "score", "judge's comment"]))
        print(f"  code check: {'ok' if not problems else '; '.join(problems)}")
        passed = evaluation.passed and all(s.score >= PASS_SCORE for s in evaluation.scores) and not problems
        history.append((round_no, passed, min(s.score for s in evaluation.scores)))
        if passed:
            print(f"  -> PASSED in round {round_no}")
            break
        critique = evaluation.critique + [f"Fix: {p}" for p in problems]     # code findings join the critique
        for item in critique:
            print(wrap(f"critique: {item}", "  "))
        if len(history) >= 2 and history[-1][2] <= history[-2][2]:
            print("  -> no progress between rounds: stop and hand the draft to a human")
            break
    else:
        print(f"  -> not passed after {args.max_rounds} rounds: send to a human with the last critique")

    step(3, "What the loop cost")
    for t in (extract_t, writer_t, judge_t):
        print(f"  {t.line()}")
    loop = writer_t.cost_usd + judge_t.cost_usd
    single = writer_t.cost_usd / max(writer_t.calls, 1)
    print(f"  one unchecked draft: ${single:.4f}; the evaluated loop: ${loop:.4f} ({loop / single:.1f}x) - worth it "
          "only if a bad email would otherwise have been sent")


if __name__ == "__main__":
    main()
