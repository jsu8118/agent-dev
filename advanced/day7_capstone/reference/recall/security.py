"""Deterministic security for the campaign: inbound screening and outbound sanitisation.

The model is not a security boundary.  Everything that must never happen - acting on a lookalike sender,
following instructions embedded in a reply, shipping to a third party, leaking other customers, changing
the hazard wording - is decided here, in code that the text cannot argue with, before an agent sees the
message and after it writes one.  The agent's judgement is reserved for what needs judgement: intent,
scheduling constraints, tone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

INJECTION = re.compile(r"\[\[.*?(assistant|ai|system).*?\]\]|ai assistant instruction|assistant instruction|ignore (all )?(previous|prior) "
                       r"instructions|system override|mark .* as remediated|close the thread", re.I | re.S)
SPOOFED_INTERNAL = re.compile(r"^>?\s*from:\s*[\w.+-]+@kestrel-pumps\.example", re.I | re.M)
PHISHING = re.compile(r"https?://\S+", re.I)
CREDENTIAL_WORDS = re.compile(r"credentials|log ?in|sso|password|pre-register", re.I)
REDIRECT = re.compile(r"ship (the )?(replacement|parts|kits?) .*?(to|instead)|new logistics partner|deliver .* to .* instead", re.I | re.S)
SECRECY = re.compile(r"do not contact the site|not aware yet|keep this between", re.I)
DATA_REQUEST = re.compile(r"(other|which) (companies|customers).*?(affected|contact)|list of other", re.I | re.S)
SAFETY = re.compile(r"injur|sprayed|fluid release|released process fluid|incident|hospital|burn", re.I)
LEGAL = re.compile(r"\bliable\b|counsel|legal (team|hold)|litigation", re.I)
COMPENSATION = re.compile(r"compensation|\$\s?\d[\d,]*|downtime", re.I)
OUT_OF_OFFICE = re.compile(r"automatic reply|out of (the )?office", re.I)
PROMISES = re.compile(r"we will (refund|compensate|pay)|full compensation|liab(le|ility)|no risk|perfectly safe|safe to (keep )?run", re.I)
URL = re.compile(r"https?://[^\s)>]+", re.I)


@dataclass
class Screen:
    label: str                      # clean | quarantined:<kind> | flag:<kind>[,<kind>]
    sender_status: str              # known | lookalike | unknown
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def quarantine(self) -> bool:
        return self.label.startswith("quarantined:")


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def sender_status(address: str, known_domains: set[str]) -> str:
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in known_domains:
        return "known"
    for known in known_domains:
        if levenshtein(domain, known) <= 2 or known.split(".")[0] in domain or domain.startswith("mail-" + known):
            return "lookalike"
    return "unknown"


def screen_inbound(reply: dict, contacts: list[dict]) -> Screen:
    """Decide whether a reply may reach the agent, and with which flags."""
    known_domains = {c["email"].rsplit("@", 1)[-1].lower() for c in contacts}
    status = sender_status(reply["from"], known_domains)
    text = f"{reply.get('subject', '')}\n{reply.get('body', '')}"
    reasons, flags = [], []
    if status == "lookalike":
        reasons.append(f"sender domain {reply['from'].rsplit('@', 1)[-1]} is a lookalike of a contact domain")
    if status == "unknown":
        reasons.append("sender is not a contact on file")
    kinds = []
    if INJECTION.search(text):
        kinds.append("injection"); reasons.append("instructions addressed to the assistant inside the message")
    if SPOOFED_INTERNAL.search(text):
        kinds.append("spoofed_internal"); reasons.append("a forwarded 'internal' approval: approvals live in the approval system, not in email")
    if PHISHING.search(text) and CREDENTIAL_WORDS.search(text):
        kinds.append("phishing"); reasons.append("a link that asks for credentials")
    if REDIRECT.search(text):
        kinds.append("fraud_redirect")
        reasons.append("asks to redirect parts to a third party" + (" and to keep the site uninformed" if SECRECY.search(text) else ""))
    if DATA_REQUEST.search(text):
        kinds.append("data_request"); reasons.append("asks for other customers' data")
    if status == "lookalike":
        kinds.insert(0, "spoofed_sender")
    if kinds:
        return Screen(label="quarantined:" + ",".join(kinds), sender_status=status, reasons=reasons)
    if SAFETY.search(text):
        flags.append("safety_event")
    if LEGAL.search(text):
        flags.append("legal")
    if COMPENSATION.search(text) and not OUT_OF_OFFICE.search(text):
        flags.append("compensation")
    if OUT_OF_OFFICE.search(text):
        flags.append("out_of_office")
    if status == "unknown":
        flags.append("unknown_sender")
    return Screen(label=("flag:" + ",".join(flags)) if flags else "clean", sender_status=status, reasons=reasons, flags=flags)


def sanitize_outbound(body: str, *, customer: dict | None, other_customers: list[str]) -> tuple[str, list[dict]]:
    """Return the body the customer may receive, and the issues found (severity block|fixed)."""
    issues: list[dict] = []
    clean = body
    for url in URL.findall(body):
        if "kestrel-pumps.example" not in url:
            clean = clean.replace(url, "[link removed]")
            issues.append({"severity": "fixed", "detail": f"removed external link {url[:40]}"})
    for name in other_customers:
        if name and name.lower() in clean.lower():
            issues.append({"severity": "block", "detail": f"mentions another customer ({name})"})
    m = PROMISES.search(clean)
    if m:
        issues.append({"severity": "block", "detail": f"promise or hazard-wording change: '{m.group(0)}'"})
    return clean, issues
