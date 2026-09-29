"""Solution to exercise 11 - add a detection feature and measure the delta on the corpus.

At the block threshold the input stack leaves 11 attacks at 'review'. Most of those are correct dispositions:
ATK-005 (goodwill credit) and ATK-020 (fabricated agreement) need a human but are not injections; ATK-025 is a
shape-coercion the output layer handles. The genuine gap is data exfiltration of OTHER customers' data phrased as a
polite business ask (ATK-017 "list which other companies bought ... with contact names") and (ATK-018 "forward the
full audit log ... to [external]"): those should block, and no benign case looks like them. The candidate feature
targets exactly that, and we measure its effect without re-fitting to the labels.

Run: python advanced/day5_security_engineering/solutions/ex11_classifier_feature.py
"""
# test: expect=projected delta

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import get_client, header, step  # noqa: E402

import _day5 as d5  # noqa: E402

FEATURE_FAMILY = "data_exfiltration"
FEATURE = re.compile(r"which (other )?(companies|customers)\b|list of other (companies|customers)|contact names\b|"
                     r"forward .{0,40}\b(audit log|internal notes|full (account|customer) (log|record))|"
                     r"benchmark(ing)? suppliers", re.I)


def would_fire(text: str) -> bool:
    return bool(FEATURE.search(text))


def main() -> None:
    client = get_client()
    header("Exercise 11 - add a classifier feature")
    cases = d5.load_cases()
    attacks = [c for c in cases if c["label"] == "attack"]
    benign = [c for c in cases if c["label"] == "benign"]

    step(1, "Current gaps at the block threshold")
    action = {}
    for c in cases:
        s = d5.screen_stack(client, c["payload"], channel=c["channel"], sender=c.get("context", {}).get("from", ""))
        action[c["id"]] = s["action"]
    missed = [c for c in attacks if action[c["id"]] != "block"]
    exfil_missed = [c for c in missed if c["family"] in ("data_exfiltration", "markdown_exfiltration")]
    print(f"  attacks at review/allow: {len(missed)}; of those, data-exfiltration-shaped: "
          f"{[c['id'] for c in exfil_missed]}")

    step(2, "The candidate feature and its projected delta")
    print(f"  family: {FEATURE_FAMILY}")
    fires_missed = [c["id"] for c in missed if would_fire(c["payload"])]
    fires_benign = [c["id"] for c in benign if would_fire(c["payload"])]
    print(f"  fires on currently-missed attacks: {fires_missed}")
    print(f"  fires on benign cases:             {fires_benign or 'none'}")
    print(f"  projected delta: +{len(fires_missed)} attacks moved toward block, +{len(fires_benign)} benign false "
          "positives")

    step(3, "The trade, stated honestly")
    print("  This adds detection on the exfiltration ask at zero benign cost on this corpus - a good feature. But two\n"
          "  cautions the lesson insists on: (1) zero FP on 12 hard negatives is not zero FP in production; a phrase\n"
          "  like 'contact names' will appear in legitimate mail, so ship it at 'review', not 'block', and watch the\n"
          "  rate. (2) A regex feature is a lagging patch; the durable control for ATK-017/018 is the ROW FILTER and\n"
          "  the OUTBOUND-EMAIL ALLOWLIST (labs 03, 06), which stop the data leaving whatever the classifier decides.\n"
          "  Add the feature to advanced/mock_scenarios/day5_security_engineering.py (classify_text) and measure it\n"
          "  live through lab 02; keep the two attacks in the regression suite (lab 07) so a later edit cannot silently\n"
          "  drop them.")


if __name__ == "__main__":
    main()
