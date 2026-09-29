"""Exercise 8 starter - a description linter for MCP tool descriptions.

A tool description is text the model reads as guidance, but a server author controls it. Write a linter that flags a
description which tries to INSTRUCT the assistant (rather than describe the tool), run it over the six manifests, and
compare with the review's labels (the manifests whose issues mention a hidden instruction).

Fill in `lint(description)` so it returns a list of finding codes. Aim to flag kestrel-weather/get_weather_at_site
and kestrel-ops-tools/search_orders_fast, and to leave the clean internal descriptions alone.

Run: python advanced/day5_security_engineering/exercises/ex08_description_linter.py
"""
# test: expect=TODO

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day5 as d5  # noqa: E402


def lint(description: str) -> list[str]:
    """Return finding codes for one tool description. TODO: flag instruction-like text.

    Ideas for what to catch (each a separate code):
      - text addressed to the assistant / AI / automated system
      - ordering directives ("before using any other tool", "call this first", "always call this tool")
      - "follow the results" / "process this before answering"
      - demands to pass the conversation or the user's email/identity into a parameter
      - hidden-importance markup (<important>, <!-- -->, "IMPORTANT:")
    """
    findings: list[str] = []
    # TODO: inspect `description` and append a code for each pattern you detect.
    return findings


def main() -> None:
    header("Exercise 8 - MCP description linter (starter)")
    manifests = d5.load_manifests()
    labels = d5.load_labels()
    step(1, "Run your linter over every tool description")
    for stem, m in sorted(manifests.items()):
        for tool in m.get("tools", []):
            flags = lint(tool.get("description", ""))
            marker = "TODO (implement lint)" if not flags else ", ".join(flags)
            print(f"  {stem}/{tool['name']}: {marker}")
    step(2, "Compare with labels.json")
    want = {stem for stem, lab in labels.items() if any("instruction" in i or "description" in i for i in lab.get("issues", []))}
    print(f"  manifests whose label mentions a description problem: {sorted(want)}")
    print("  TODO: your linter should flag a tool in each of those, and none in the clean internal servers.")


if __name__ == "__main__":
    main()
