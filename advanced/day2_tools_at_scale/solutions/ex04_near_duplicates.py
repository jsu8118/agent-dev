"""Solution to exercise 4 - descriptions for near-duplicate tools, checked with lab 07's selection eval.

Objective
    Rewrite the three money tools (issue_refund, issue_credit_note, apply_late_fee_waiver) so their trigger phrases no
    longer overlap, and measure the change on the 30-task selection eval against description sets A and B.

Concepts
    disjoint trigger phrases, naming the neighbour tool instead of repeating its trigger words, paired comparison on
    the same tasks, the risk of tuning on the eval set

Run
    python advanced/day2_tools_at_scale/solutions/ex04_near_duplicates.py

What to observe
    * Set B's one remaining confusion (a 'goodwill credit' sent to the fee waiver) disappears in set C.
    * No task that B got right breaks.
"""
# test: expect=set C

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day2 as d2  # noqa: E402

SYSTEM = d2.MARK_SELECT + "\nYou are Kestrel's operations copilot. Call the one tool that fits the request."

# Set C = set B with the money tools rewritten so each trigger phrase belongs to exactly one tool.
DESCRIPTIONS_C = {
    **d2.DESCRIPTIONS_B,
    "apply_late_fee_waiver": "Cancel the late-payment fees or charges on an overdue invoice (AR-90244) when the delay was "
                             "ours: 'waive the late fee', 'drop the late-payment charge'. Only touches the fees; any other "
                             "reduction of what the customer owes is issue_credit_note.",
    "issue_credit_note": "Reduce what a customer owes on an invoice (AR-90257) without moving money: 'knock $500 off', "
                         "'goodwill credit', 'credit them', 'reduce the balance or invoice by 5%'. Amount in USD (compute "
                         "percentages from the invoice amount). Needs approval. No cash goes back to the customer.",
}


def evaluate(client, descriptions: dict[str, str] | None) -> list[str | None]:
    tools = [d2.api_tool(d2.entry(n), description=(descriptions or {}).get(n)) for n in d2.SELECTION_TOOLS]
    out = []
    for text, _ in d2.SELECTION_TASKS:
        r = client.messages.create(model=MODEL, max_tokens=2000, system=d2.cached_system(SYSTEM), tools=tools,
                                   messages=[{"role": "user", "content": text}])
        out.append(next((b.name for b in r.content if b.type == "tool_use"), None))
    return out


def main() -> None:
    client = get_client()
    header("Exercise 4 - near-duplicate descriptions")
    step(1, "The rewritten descriptions (set C)")
    for name in ("apply_late_fee_waiver", "issue_credit_note"):
        print(f"  {name}")
        print(wrap("B: " + d2.DESCRIPTIONS_B[name], "      "))
        print(wrap("C: " + DESCRIPTIONS_C[name], "      "))

    step(2, "Selection eval: A, B, C on the same 30 tasks")
    expected = [e for _, e in d2.SELECTION_TASKS]
    results = {"A": evaluate(client, None), "B": evaluate(client, d2.DESCRIPTIONS_B), "C": evaluate(client, DESCRIPTIONS_C)}
    for label, got in results.items():
        wrong = [(e, g) for e, g in zip(expected, got) if e != g]
        print(f"  set {label}: {len(expected) - len(wrong)}/{len(expected)}  confusions: {wrong or 'none'}")
    broken = [i for i in range(len(expected)) if results["B"][i] == expected[i] and results["C"][i] != expected[i]]
    print(f"  tasks set B got right that set C breaks: {broken or 'none'}")
    if is_mock():
        print("[mock] a lexical stand-in: it rewards removing 'goodwill' from the waiver's text. A model is also helped by\n"
              "       the sentence naming issue_credit_note - run the same eval live, and on held-out tasks (exercise 9).")


if __name__ == "__main__":
    main()
