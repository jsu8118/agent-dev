"""Exercise 11 starter - add a detection feature and measure the delta on the corpus.

The mock classifier is a set of weighted feature regexes (advanced/mock_scenarios/day5_security_engineering.py,
`classify_text`). You cannot edit that module from here, but you CAN measure the current stack and reason about a
feature you would add. This starter builds the per-case table you need; your job is to (a) find a family the input
stack under-catches at the block threshold, (b) write a candidate feature (a regex + weight), and (c) estimate its
effect on detection and false positives WITHOUT re-fitting to the labels.

Fill in `candidate_feature()` and `would_fire()`; the harness prints the current gaps and your feature's projected
delta. (The real change goes in the scenario module and is measured live in lab 02 / lab 07.)

Run: python advanced/day5_security_engineering/exercises/ex11_classifier_feature.py
"""
# test: expect=TODO

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import get_client, header, step  # noqa: E402

import _day5 as d5  # noqa: E402


def candidate_feature() -> tuple[str, re.Pattern] | None:
    """Return (family, compiled_regex) for a feature you would add, or None (starter). TODO."""
    return None  # TODO: e.g. ("data_exfiltration", re.compile(r"...", re.I))


def would_fire(text: str) -> bool:
    feat = candidate_feature()
    return bool(feat and feat[1].search(text))


def main() -> None:
    client = get_client()
    header("Exercise 11 - add a classifier feature (starter)")
    cases = d5.load_cases()
    attacks = [c for c in cases if c["label"] == "attack"]
    benign = [c for c in cases if c["label"] == "benign"]

    step(1, "Where does the input stack land each case now?")
    missed_at_block = []
    for c in cases:
        s = d5.screen_stack(client, c["payload"], channel=c["channel"], sender=c.get("context", {}).get("from", ""))
        c["_action"] = s["action"]
        if c["label"] == "attack" and s["action"] != "block":
            missed_at_block.append(c)
    print(f"  attacks not BLOCKED at the block threshold (review or allow): {len(missed_at_block)}")
    for c in missed_at_block:
        print(f"    {c['id']} ({c['family']}): action={c['_action']}  {d5.short(c['payload'], 55)}")

    step(2, "Your candidate feature's projected effect")
    if candidate_feature() is None:
        print("  TODO: implement candidate_feature() and would_fire(), then re-run to see the delta.")
        return
    fam, _ = candidate_feature()
    new_block = sum(1 for c in missed_at_block if would_fire(c["payload"]))
    new_fp = sum(1 for c in benign if would_fire(c["payload"]))
    print(f"  feature family: {fam}")
    print(f"  would move {new_block}/{len(missed_at_block)} currently-missed attacks toward block")
    print(f"  would newly fire on {new_fp}/{len(benign)} benign cases (watch this - it is the cost)")
    print("  Justify the trade in solutions/README.md terms: detection gained vs false positives added.")


if __name__ == "__main__":
    main()
