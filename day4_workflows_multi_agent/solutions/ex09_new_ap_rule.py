"""Solution to exercise 9 - add FIN-AP-010 rule 11: the remit-to account must match the supplier master.

The new rule (revision 2026-10, hypothetical): if an invoice prints a remit-to account whose last four
digits differ from the supplier master file, the invoice goes to security_hold with the exception
code `remit_account_mismatch` - it is the quiet version of INV-14's bank-change request.

Design choices
    * The base engine (_ap.three_way_match) is untouched. Rules are composed: the new rule runs after
      the base checks and may raise the severity. In a larger codebase, make every check a function in
      a list - adding rule 12 is then one function plus its tests.
    * "Missing" is not "mismatch": the second invoice layout prints no remit account at all, and the
      rule must not fire on those (that would put every layout-B invoice on security hold).
    * A security hold outranks approval routing: a clean invoice above $25k that trips the rule is no
      longer "matched", so total_above_auto_approval_limit is dropped.
    * Proof of no regression: the 20 real invoices are extracted ONCE and run through both engines;
      the decisions must be identical. The new behaviour is proven on a synthetic invoice (INV-05
      with a different account) - plus unit tests that need no model at all.

Run
    python day4_workflows_multi_agent/solutions/ex09_new_ap_rule.py
"""

# test: expect=no regressions
# test: expect=remit_account_mismatch

from __future__ import annotations

import _labs  # noqa: F401  (puts labs/ on sys.path)
from _ap import (CODE_TO_DECISION, SEVERITY, APLedger, ExtractedInvoice, InvoiceLine, MasterData, MatchResult,
                 extract_with_gate, load_expected, load_invoices, load_master, score, short, three_way_match)
from _common import Tally, table
from labkit import MODEL, get_client, header, step

NEW_CODE = "remit_account_mismatch"
CODE_TO_DECISION_V2 = {**CODE_TO_DECISION, NEW_CODE: "security_hold"}


def decide_v2(codes: list[str]) -> str:
    if not codes:
        return "approve"
    return max((CODE_TO_DECISION_V2.get(c, "hold") for c in codes), key=SEVERITY.__getitem__)


def rule_11_remit_account(inv: ExtractedInvoice, master: MasterData, supplier_id: str | None) -> str | None:
    """Return a note if the printed remit account contradicts the master file, else None."""
    supplier = master.suppliers.get(supplier_id or "")
    printed = (inv.remit_account_last4 or "").strip()
    if supplier is None or not printed:
        return None                                   # nothing printed (layout B) is not a mismatch
    if printed != supplier["bank_account_last4"]:
        return f"invoice remits to account ending {printed}; the master file has {supplier['bank_account_last4']}"
    return None


def match_v2(inv: ExtractedInvoice, raw: str, master: MasterData, ledger: APLedger, *, file: str,
             commit: bool = True) -> MatchResult:
    result = three_way_match(inv, raw, master, ledger, file=file, commit=commit)
    note = rule_11_remit_account(inv, master, result.supplier_id)
    if note:
        codes = [c for c in result.exceptions if c != "total_above_auto_approval_limit"] + [NEW_CODE]
        result.exceptions = codes
        result.notes.append(f"{NEW_CODE}: {note}")
        result.decision = decide_v2(codes)
    return result


def unit_tests(master: MasterData) -> list[tuple[str, bool]]:
    """Rule-level tests: no model, no documents - just the logic."""
    base = dict(supplier_name="Coastal Freight Services", invoice_number="T-1", invoice_date="2026-09-01",
                po_number="PO-4500140", currency="USD",
                lines=[InvoiceLine(part_number="FRT-LTL", description="x", quantity=1, unit_of_measure="TRIP",
                                   unit_price=1850.0, amount=1850.0)],
                subtotal=1850.0, tax_amount=0.0, total=1850.0, bank_change_requested=False,
                addresses_automated_systems=False, suspicious_text=None)
    same = ExtractedInvoice(remit_account_last4="7742", **base)
    other = ExtractedInvoice(remit_account_last4="0001", **base)
    missing = ExtractedInvoice(remit_account_last4=None, **base)
    return [("matching account passes", rule_11_remit_account(same, master, "S-205") is None),
            ("different account fires", rule_11_remit_account(other, master, "S-205") is not None),
            ("no printed account does not fire", rule_11_remit_account(missing, master, "S-205") is None),
            ("unknown supplier does not fire", rule_11_remit_account(other, master, None) is None)]


def main() -> None:
    header("Exercise 9 - FIN-AP-010 rule 11: remit-to account must match the supplier master")
    client = get_client()
    master, invoices, expected = load_master(), load_invoices(), load_expected()

    step(1, "Unit tests of the rule (no model involved)")
    for name, ok in unit_tests(master):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    step(2, "Regression: extract the 20 invoices once, run the old and the new engine on the same records")
    tally = Tally("extraction")
    extracted = []
    for file, raw in invoices:
        inv, _, _ = extract_with_gate(client, raw, model=MODEL, effort="low", tally=tally)
        extracted.append((file, raw, inv))
    old_ledger, new_ledger = APLedger(), APLedger()
    changed, preds = [], {}
    for file, raw, inv in extracted:
        old = three_way_match(inv, raw, master, old_ledger, file=file)
        new = match_v2(inv, raw, master, new_ledger, file=file)
        preds[file] = (new.decision, new.exceptions)
        if (old.decision, old.exceptions) != (new.decision, new.exceptions):
            changed.append(f"{short(file)}: {old.decision} -> {new.decision} {new.exceptions}")
    print(f"  new engine vs expected outcomes: {score(preds, expected).summary()}")
    print("  no regressions: all 20 decisions unchanged" if not changed else "  CHANGED: " + "; ".join(changed))

    step(3, "New behaviour: a synthetic invoice - INV-05 with a different remit account")
    raw05 = next(raw for f, raw in invoices if f.startswith("INV-05"))
    synthetic = raw05.replace("CFS-5521", "CFS-5601").replace("account ending 7742", "account ending 9912")
    inv, problems, _ = extract_with_gate(client, synthetic, model=MODEL, effort="low", tally=tally)
    rows = []
    for label, engine in (("v1 (policy rev 2026-06)", three_way_match), ("v2 (+ rule 11)", match_v2)):
        result = engine(inv, synthetic, master, APLedger(), file="SYNTH-01", commit=False)
        rows.append([label, result.decision, ", ".join(result.exceptions) or "-", "; ".join(result.notes)[:80]])
    print(table(rows, ["engine", "decision", "exceptions", "notes"]))
    print(f"  {tally.line()}")


if __name__ == "__main__":
    main()
