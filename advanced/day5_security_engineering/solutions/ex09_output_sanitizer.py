"""Solution to exercise 9 - an output sanitiser (DLP) for customer-facing replies.

The sanitiser is the last independent layer: it does not care whether the model was fooled, only what is about to
leave. Three passes - external images out, out-of-allowlist or data-bearing links out, sensitive strings and
secret-like tokens withheld - using the regexes in _day5 (IMAGE_MARKDOWN, LINK_MARKDOWN, BARE_URL) and
d5._url_carries_data. This is the same logic as d5.sanitize_reply; here it is spelled out.

Run: python advanced/day5_security_engineering/solutions/ex09_output_sanitizer.py
"""
# test: expect=all cases pass

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day5 as d5  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))
import ex09_output_sanitizer as starter  # noqa: E402  (reuse its CASES)


@dataclass
class Result:
    text: str
    findings: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()


def sanitize(reply: str, *, allowed_domains: set[str], sensitive: set[str]) -> Result:
    out, findings, removed = reply, [], []

    def domain_ok(url: str) -> bool:
        h = _host(url)
        return any(h == d or h.endswith("." + d) for d in allowed_domains)

    for m in list(d5.IMAGE_MARKDOWN.finditer(out)):        # 1. external images: always out
        findings.append("external_image" + ("_with_data" if d5._url_carries_data(m.group(1), sensitive) else ""))
        removed.append(m.group(0))
        out = out.replace(m.group(0), "[image removed]")
    for m in list(d5.LINK_MARKDOWN.finditer(out)):         # 2. links outside the allowlist or carrying data
        url = m.group(2)
        if not domain_ok(url) or d5._url_carries_data(url, sensitive):
            findings.append("link_" + ("outside_allowlist" if not domain_ok(url) else "with_data"))
            removed.append(url)
            out = out.replace(m.group(0), m.group(1))
    for url in list(d5.BARE_URL.findall(out)):
        if not domain_ok(url) or d5._url_carries_data(url, sensitive):
            findings.append("url_" + ("outside_allowlist" if not domain_ok(url) else "with_data"))
            removed.append(url)
            out = out.replace(url, "[link removed]")
    for value in sorted(sensitive, key=len, reverse=True):  # 3. sensitive strings and secrets
        if value and value in out:
            findings.append("internal_detail")
            removed.append(value)
            out = out.replace(value, "[withheld]")
    for m in list(d5.SECRET_RE.finditer(out)):
        findings.append("secret_like_token")
        removed.append(m.group(0))
        out = out.replace(m.group(0), "[secret withheld]")
    return Result(out, findings, removed)


def main() -> None:
    header("Exercise 9 - output sanitiser")
    step(1, "Run the sanitiser on the starter's cases")
    ok_all = True
    for label, reply, allow, sensitive, must_remove in starter.CASES:
        res = sanitize(reply, allowed_domains=allow, sensitive=sensitive)
        gone = must_remove is None or must_remove not in res.text
        # the internal-link case must SURVIVE (nothing removed)
        survived_ok = (must_remove is not None) or (not res.findings)
        ok = gone and survived_ok
        ok_all = ok_all and ok
        print(f"  {label:22s} findings={res.findings or 'none'}  [{'PASS' if ok else 'FAIL'}]")
        print(f"      cleaned: {d5.short(res.text, 90)}")
    step(2, "Verdict")
    print(f"  all cases pass: {ok_all}")
    print("  The data-bearing image and the secret are removed; the internal link survives untouched. Run this after "
          "the model writes and BEFORE the reply is sent - it is the exit-side twin of lab 02's input tagging.")


if __name__ == "__main__":
    main()
