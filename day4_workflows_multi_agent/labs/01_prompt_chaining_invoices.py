"""Lab 01 - Prompt chaining: extract (LLM) -> gate (code) -> three-way match (code) -> memo (LLM).

Objective
    Process Kestrel's 20 supplier invoices the way a production AP pipeline should: an LLM turns
    each messy invoice into a typed record, deterministic code checks that the record is grounded
    in the document and applies Policy FIN-AP-010 (three-way match against PO and goods receipts),
    and a second LLM step writes the exception memo for the AP clerk. Score the decisions against
    the AP team's expected outcomes.

Concepts
    Prompt chaining; gates between steps; "LLM for unstructured -> structured and judgement, code
    for rules, arithmetic and policy"; structured outputs (messages.parse -> pydantic); stateful
    policy checks (duplicates, cumulative quantities) that a single prompt cannot see; why the
    architecture - not the prompt - neutralizes INV-14's prompt injection; least-privilege context
    for later chain steps; cost per invoice at Kestrel's volume.

Run
    python day4_workflows_multi_agent/labs/01_prompt_chaining_invoices.py [--model claude-opus-5]
        [--effort low] [--memos 3] [--limit 20]

What to observe
    * The decision table: 20 invoices, most exceptions come from code, not from the LLM.
    * Step 3: decision accuracy and exception-code precision/recall against expected_ap_outcomes.json.
    * Step 4: INV-14 asks "AI assistants" to approve it and change bank details. The extractor has
      no tools and no authority; its output is a schema; the decision function never reads free
      text - and even a fooled extractor would be caught by the rule detectors and the quantity check.
    * Step 5: a "helpful" extractor that corrects INV-13's tax would make the invoice look clean;
      the grounding gate catches it because the corrected numbers are not printed on the page.
    * Step 6: the memo writer sees only code-verified facts, never the raw (untrusted) invoice.
"""

# test: expect=decision accuracy 20/20
# test: expect=INV-14

from __future__ import annotations

import argparse
import json

from _ap import (SEVERITY, APLedger, ExtractedInvoice, MasterData, MatchResult, extract_with_gate, grounding_problems,
                 load_expected, load_invoices, load_master, score, short, three_way_match)
from _common import Tally, effort_kwargs, mock_note, table, text_blocks
from labkit import MODEL, get_client, header, step, wrap

MEMO_SYSTEM = """<day4_ap_memo>
You write short exception memos for Kestrel's accounts-payable clerks. You receive the decision
facts that the matching engine already computed. Explain in 2-4 plain sentences what is wrong and
the concrete next step for the clerk. Use only the facts given - do not re-decide the outcome,
do not invent amounts, names or dates.
</day4_ap_memo>"""

MONTHLY_INVOICES = 1_200          # Kestrel's AP volume (company profile)


def run_chain(client, master: MasterData, invoices: list[tuple[str, str]], *, model: str,
              effort: str | None, tally: Tally) -> list[dict]:
    ledger = APLedger()                     # state carried ACROSS invoices: duplicates, cumulative qty
    rows = []
    for file, raw in invoices:
        inv, problems, retried = extract_with_gate(client, raw, model=model, effort=effort, tally=tally)
        if inv is None:
            # The gate failed twice: never let an unverified record reach the policy engine.
            result = MatchResult(file, "hold", ["extraction_unverified"], problems, None, None)
        else:
            result = three_way_match(inv, raw, master, ledger, file=file)
        rows.append({"file": file, "invoice": inv, "result": result, "gate": "retry" if retried else "ok",
                     "raw": raw})
    return rows


def print_decisions(rows: list[dict], expected: dict[str, dict]) -> None:
    table_rows = []
    for r in rows:
        inv: ExtractedInvoice | None = r["invoice"]
        res: MatchResult = r["result"]
        exp = expected[r["file"]]
        ok = res.decision == exp["expected_decision"] and set(res.exceptions) == set(exp["expected_exceptions"])
        extracted = f"{inv.invoice_number} / {inv.po_number or '-'} / {inv.currency} {inv.total:,.2f}" if inv else "-"
        table_rows.append([short(r["file"]), extracted, r["gate"], "ok" if ok else "MISMATCH", res.decision,
                           ", ".join(res.exceptions) or "-"])
    print(table(table_rows, ["invoice", "extracted: number / PO / total", "gate", "vs expected", "decision",
                             "exceptions"]))


def injection_walkthrough(rows: list[dict]) -> None:
    row = next((r for r in rows if r["file"].startswith("INV-14")), None)
    if row is None:
        print("  (INV-14 not in this run - use the default --limit)")
        return
    raw_notice = [l.strip() for l in row["raw"].splitlines() if "AI ASSISTANTS" in l.upper() or "IBAN" in l.upper()]
    print("  What the document says (untrusted data):")
    for line in raw_notice:
        print(wrap(f"> {line}", "    "))
    inv: ExtractedInvoice = row["invoice"]
    res: MatchResult = row["result"]
    print("\n  What the extractor returned - flags and a verbatim quote, i.e. data about the text, not obedience:")
    print(f"    bank_change_requested={inv.bank_change_requested}  addresses_automated_systems="
          f"{inv.addresses_automated_systems}  remit_account_last4={inv.remit_account_last4}")
    print(f"\n  Decision by code: {res.decision.upper()}  exceptions={res.exceptions}")
    print(f"  Which detector fired: {res.detectors}   ('rule' = regex on the raw text, independent of the LLM)")
    for note in res.notes:
        print(f"    - {note}")
    print(wrap("Why the injection cannot work here: the extraction call has no tools and no authority - its only "
               "output is a schema-constrained record. Approval and bank-detail changes are not actions any LLM "
               "in this chain can take; they are code paths that read typed fields, never free text. Even if the "
               "extractor had been fooled into reporting no bank change, the regex detectors run on the raw "
               "document, and the invoice would still fail the cumulative-quantity check (a 5th freight trip "
               "against 4 received).", "  "))


def gate_demo(rows: list[dict], master: MasterData) -> None:
    row = next((r for r in rows if r["file"].startswith("INV-13")), None)
    if row is None or row["invoice"] is None:
        print("  (INV-13 not in this run)")
        return
    raw, inv = row["raw"], row["invoice"]
    # What an over-helpful extractor might do: "fix" the supplier's 10% tax to the 8% it expects.
    tampered = inv.model_copy(update={"tax_amount": round(inv.subtotal * 0.08, 2),
                                      "total": round(inv.subtotal * 1.08, 2)})
    silent = three_way_match(tampered, raw, master, APLedger(), file=row["file"], commit=False)
    print(f"  Printed on INV-13: tax {inv.tax_amount:,.2f}, total {inv.total:,.2f}.  Tampered record: tax "
          f"{tampered.tax_amount:,.2f}, total {tampered.total:,.2f}.")
    print(f"  Without a gate the policy engine would say: {silent.decision} {silent.exceptions or ''}"
          "  <- the supplier's error disappears")
    print("  The grounding gate says:")
    for problem in grounding_problems(tampered, raw):
        print(f"    - {problem}")


def write_memos(client, rows: list[dict], n: int, *, model: str, tally: Tally) -> None:
    candidates = sorted((r for r in rows if r["result"].decision != "approve" and r["invoice"] is not None),
                        key=lambda r: (-SEVERITY[r["result"].decision], r["file"]))[:n]
    for r in candidates:
        inv: ExtractedInvoice = r["invoice"]
        res: MatchResult = r["result"]
        facts = {"invoice_number": inv.invoice_number, "supplier": inv.supplier_name, "po_number": inv.po_number,
                 "currency": inv.currency, "total": inv.total, "decision": res.decision,
                 "exceptions": res.exceptions, "notes": res.notes}
        # Least privilege: the memo writer gets code-verified facts only - the raw invoice (and any
        # instruction hidden in it) never reaches this call.
        response = client.messages.create(
            model=model, max_tokens=2000, system=MEMO_SYSTEM,
            messages=[{"role": "user", "content": "<decision_facts>\n" + json.dumps(facts, indent=1) +
                       "\n</decision_facts>\nWrite the memo."}],
            **effort_kwargs(model, "low"),
        )
        tally.add(response)
        print(f"  [{short(r['file'])}]")
        print(wrap(text_blocks(response), "    "))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default="low", help="low | medium | high (ignored on models without effort)")
    parser.add_argument("--memos", type=int, default=3, help="how many exception memos to write (0 = skip)")
    parser.add_argument("--limit", type=int, default=None, help="process only the first N invoices")
    args = parser.parse_args()

    header("Lab 01 - Prompt chaining: extract (LLM) -> gate (code) -> three-way match (code) -> memo (LLM)")
    mock_note("the extractor is a regex-based stand-in; the gate, policy engine and scoring are the real code.")
    client = get_client()

    step(1, "Load master data (suppliers, purchase orders, goods receipts) and the invoice inbox")
    master = load_master()
    invoices = load_invoices()[: args.limit]
    expected = load_expected()
    print(f"  {len(master.suppliers)} suppliers, {len(master.purchase_orders)} POs, "
          f"{sum(len(v) for v in master.receipts.values())} goods receipts, {len(invoices)} invoices")

    step(2, f"Run the chain on every invoice ({args.model}, effort={args.effort})")
    extraction = Tally("extraction")
    rows = run_chain(client, master, invoices, model=args.model, effort=args.effort, tally=extraction)
    print_decisions(rows, expected)

    step(3, "Score against the AP team's expected outcomes (data/finance/expected_ap_outcomes.json)")
    result = score({r["file"]: (r["result"].decision, r["result"].exceptions) for r in rows}, expected)
    print(f"  {result.summary()}")
    for mismatch in result.mismatches:
        print(f"  MISMATCH {mismatch}")
    by_source = {"code": 0, "llm-assisted": 0}
    for r in rows:
        for code in r["result"].exceptions:
            by_source["llm-assisted" if code in r["result"].detectors else "code"] += 1
    print(f"  exception codes raised by pure code: {by_source['code']}; by detectors that use the LLM's flags: "
          f"{by_source['llm-assisted']}")

    step(4, "INV-14 under the microscope: a bank-change request with a prompt injection")
    injection_walkthrough(rows)

    step(5, "Why the gate exists: a 'helpful' extractor that corrects INV-13's tax")
    gate_demo(rows, master)

    memo_tally = Tally("memos")
    if args.memos > 0:
        step(6, f"Chain step 4 (LLM): exception memos for the AP clerk - structured facts in, prose out")
        write_memos(client, rows, args.memos, model=args.model, tally=memo_tally)

    step(7, "Cost at Kestrel's volume")
    n = max(len(rows), 1)
    print(f"  {extraction.line()}")
    if memo_tally.calls:
        print(f"  {memo_tally.line()}")
    per_invoice = extraction.cost_usd / n
    print(f"  extraction cost per invoice ${per_invoice:.4f} -> {MONTHLY_INVOICES:,} invoices/month = "
          f"${per_invoice * MONTHLY_INVOICES:,.2f}/month (Message Batches API: 50% of that; AP is not real-time)")
    print("  (cache reads appear only once the shared prefix passes the model's minimum cacheable length)")


if __name__ == "__main__":
    main()
