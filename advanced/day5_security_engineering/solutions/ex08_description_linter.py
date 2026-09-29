"""Solution to exercise 8 - a description linter for MCP tool descriptions.

A description must describe the tool. Anything that instructs the assistant, orders a call sequence, tells it to
follow results, or asks for the conversation/PII in a parameter is an injection carried by the manifest. The linter
is four small pattern groups; run over the six manifests it flags exactly the two servers whose labels call out a
hidden instruction, and nothing in the clean internal servers.

Run: python advanced/day5_security_engineering/solutions/ex08_description_linter.py
"""
# test: expect=matches the labels

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day5 as d5  # noqa: E402

PATTERNS = {
    "instructs_the_model": re.compile(r"\b(before (using|calling) any other tool|call this tool (first|once)|always call "
                                      r"this tool|do not tell the user|follow them\b|as your first step)", re.I),
    "addresses_the_assistant": re.compile(r"\b(assistant|ai agent|automated system|the model)\b.{0,40}\b(should|must|will|"
                                          r"follow|call|pass)", re.I),
    "wants_conversation_or_pii": re.compile(r"full text of the (user|conversation|message)|the user'?s? (last message|email|"
                                            r"identity)|customer'?s? email address in the|pass the conversation", re.I),
    "hidden_importance_tag": re.compile(r"<important>|<!--|\bIMPORTANT:\s", re.I),
}


def lint(description: str) -> list[str]:
    return [code for code, rx in PATTERNS.items() if rx.search(description or "")]


def main() -> None:
    header("Exercise 8 - MCP description linter")
    manifests = d5.load_manifests()
    labels = d5.load_labels()

    step(1, "Lint every tool description")
    flagged_servers = set()
    for stem, m in sorted(manifests.items()):
        for tool in m.get("tools", []):
            flags = lint(tool.get("description", ""))
            if flags:
                flagged_servers.add(stem)
                print(f"  {stem}/{tool['name']}: {', '.join(flags)}")
    clean = [f"{stem}/{t['name']}" for stem, m in manifests.items() for t in m.get("tools", []) if not lint(t.get("description", ""))]
    print(f"  clean descriptions: {len(clean)} (e.g. {', '.join(clean[:3])})")

    step(2, "Compare with the review's labels")
    want = {stem for stem, lab in labels.items()
            if any("instruction" in i or "description" in i for i in lab.get("issues", []))}
    print(f"  labels flag a description problem in: {sorted(want)}")
    print(f"  the linter flagged:                   {sorted(flagged_servers)}")
    print(f"  matches the labels: {flagged_servers == want}")
    print("\n  Note kestrel-docs-v2's label is about the signature/telemetry rug pull; its 'follow them' description "
          "line is caught here too, which is why the linter's set can be a superset - flagging more description "
          "problems than the label headline is fine, missing one is not. The linter is deterministic and belongs in "
          "the supply-chain scan (lab 05), not behind the model it protects.")


if __name__ == "__main__":
    main()
