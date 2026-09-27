"""Step 1 of the pipeline: a deterministic input screen (Day 6, guardrails layer 1).

It runs BEFORE any model call and costs nothing. It looks for three things:

* **security signals**: instructions aimed at an AI, payment-detail changes, and sender domains
  that imitate a customer's domain (lookalike or cousin domains). Any signal quarantines the email:
  no agent, no tools, security reviews it (SOP-SUP-007 section 5).
* **safety signals** (SOP-SUP-007 section 1): a high-recall regex backstop *under* the LLM
  triage. If the classifier misses a P1, this net catches it; if it fires on a non-emergency, the
  cost is one unnecessary call from the on-call engineer. That asymmetry is deliberate.
* **identifiers** (order IDs, serial numbers) for the quality-hold detector.

Why regexes and not another LLM call? They are predictable, instant and auditable, and they
cannot be talked out of their job by the text they inspect. Their weakness is recall on
paraphrases and other languages, which is why they back up the classifier rather than replace it.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from labkit.data import ops_db

KESTREL_DOMAIN = "kestrel-pumps.example"

# ------------------------------------------------------------------------------------ security
# Patterns are case-sensitive unless they start with (?i): "IBAN" is a signal, "Iban" is a first name.
INJECTION = [
    (r"(?i)\bignore (?:all |any )?(?:the |your )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?|"
     r"policies)", "instruction override"),
    (r"(?i)\b(?:system|admin(?:istrator)?|developer|debug) (?:override|mode)\b", "fake system/admin mode"),
    # "note to the agent" could address a human support agent, so only unambiguous AI addressees count.
    (r"(?i)\b(?:note|message|instructions?) (?:to|for) (?:the |any )?(?:ai|a\.i\.|llm|(?:chat)?bot|language model|"
     r"ai (?:assistant|agent|system|model))s?\b", "note to the AI"),
    (r"(?i)\bai (?:system|assistant|agent|model)s?\b[^.\n]{0,40}(?::|,|\bplease\b)", "addresses an AI system"),
    (r"(?i)\bpre-?approved (?:for )?(?:a |the )?refunds?\b", "claims pre-approval"),
    (r"(?i)\bskip (?:the )?(?:identity |id )?verification\b", "asks to skip verification"),
    (r"(?i)\bdo not (?:escalate|flag)\b", "asks not to escalate"),
    (r"(?i)\bdisregard (?:your|the|all) (?:rules|guidelines|policy|policies|instructions)\b", "instruction override"),
]
PAYMENT_CHANGE = [
    (r"\bIBAN\b|\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b", "IBAN in message"),
    (r"(?i)\b(?:bank|banking|payment|remittance) (?:details|information|account)s?\b[^.\n]{0,30}\b"
     r"(?:changed|updated|new)\b", "payment details changed"),
    (r"(?i)\bnew (?:bank|banking|remittance) (?:details|account)", "new bank account"),
    (r"(?i)\b(?:routing|sort|swift|bic) (?:number|code)\b", "bank routing data"),
]

# ------------------------------------------------------------------------------------ safety (SOP-SUP-007 s.1)
HAZARD_FLUID = (r"\b(?:acid|sulfuric|sulphuric|hydrochloric|caustic|chemical|ammonia|chlorine|hypochlorite|solvent|"
                r"toxic|hazardous|steam|scalding|hot water|fuel|diesel|s[aä]ure|[aá]cido|qu[ií]mico)\w*")
LEAK = r"\b(?:leak\w*|spray\w*|spill\w*|burst|rupture\w*|fuga\w*|leck\w*|undicht)"
SAFETY_RULES = [
    ("hazardous_leak", lambda t: _near(HAZARD_FLUID, LEAK, t, 80)),
    ("pressure_boundary", lambda t: bool(re.search(
        r"\bcracked (?:casing|housing|volute)|\b(?:burst|blown|cracked) (?:flange|hose|pipe)|flange (?:burst|blew)", t))),
    ("fire_smoke", lambda t: bool(re.search(
        r"\b(?:smoke|smoking|flames?|sparks?|sparking|burning smell|smells? (?:of )?burning|scorch\w*|caught fire|"
        r"on fire|humo|incendio|rauch|brand(?:geruch)?|feuer)\b", t))),
    ("injury", lambda t: bool(re.search(
        r"\b(?:injur\w*|(?:was|were|got|been|is|are) (?:badly |seriously )?hurt|burned (?:his|her|their|my)|"
        r"hospitali[sz]ed|near[- ]miss|first aid|ambulance|herid\w*|verletz\w*)\b", t))),
    ("atex_fault", lambda t: bool(re.search(r"\b(?:atex|zone [012]|explosion[- ]proof|hazardous area|kp-250-x)\b", t))
     and bool(re.search(r"\b(?:leak\w*|fault|trip\w*|fail\w*|alarm|spark\w*|overheat\w*|smok\w*|won'?t (?:start|restart)|"
                        r"will not (?:start|restart))", t))),
    ("critical_outage", lambda t: bool(re.search(
        r"\b(?:drinking water|potable|residents|hospital|fire protection|sprinkler|fire pump|data ?cent(?:er|re) cooling|"
        r"(?:whole|entire) (?:plant|line)|production (?:line )?(?:is )?(?:completely )?(?:stopped|down))\b", t))
     and bool(re.search(r"\b(?:down|stopped|trip\w*|won'?t (?:start|restart)|will not (?:start|restart)|not running|"
                        r"offline|outage|failed|pressure dropped|runs? low|no backup|no redundancy)\b", t))),
    ("controller_safety_fault", lambda t: bool(re.search(r"\bf0?7\b|\bf17\b", t))
     and bool(re.search(r"\b(?:again|recur\w*|keeps|repeated\w*|after (?:we )?reset|after a reset|second time)\b", t))),
]

ORDER_RE = re.compile(r"\bSO-\d{5}\b")
SERIAL_RE = re.compile(r"\b(?:KP\d{3}|KC\d)-\d{4}-\d{4}\b")


def _near(a: str, b: str, text: str, window: int) -> bool:
    """True when a match of `a` and a match of `b` occur within `window` characters of each other."""
    pos_a = [m.start() for m in re.finditer(a, text)]
    pos_b = [m.start() for m in re.finditer(b, text)]
    return any(abs(i - j) <= window for i in pos_a for j in pos_b)


# ------------------------------------------------------------------------------------ domains
@functools.lru_cache(maxsize=1)
def known_domains() -> frozenset[str]:
    conn = ops_db()
    try:
        return frozenset({r[0].lower() for r in conn.execute("SELECT email_domain FROM customers")} | {KESTREL_DOMAIN})
    finally:
        conn.close()


def _edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance: insertions, deletions, substitutions and adjacent swaps."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _base(domain: str) -> str:
    return domain.rsplit(".", 1)[0]


def _deconfuse(name: str) -> str:
    """Undo common homoglyph tricks: rn->m, vv->w, 0->o, 1/l->i."""
    return name.replace("rn", "m").replace("vv", "w").replace("0", "o").replace("1", "i").replace("l", "i")


def domain_impersonation(sender_domain: str) -> str | None:
    """Describe how `sender_domain` imitates a known domain, or None if it doesn't."""
    sender_domain = sender_domain.lower()
    known = known_domains()
    if sender_domain in known or any(sender_domain.endswith("." + d) for d in known):
        return None                    # the domain itself, or a subdomain (only its owner can create one)
    s = _base(sender_domain)
    for domain in sorted(known):
        k = _base(domain)
        if len(k) >= 5 and _edit_distance(s, k) <= (1 if len(k) < 8 else 2):
            return f"lookalike domain {sender_domain} ~ {domain}"
        if _deconfuse(s) == _deconfuse(k):
            return f"homoglyph domain {sender_domain} ~ {domain}"
        if len(k) >= 5 and k in s:
            return f"cousin domain {sender_domain} contains {domain.split('.')[0]}"
    return None


# ------------------------------------------------------------------------------------ result
@dataclass
class ScreenResult:
    security_flags: list[str] = field(default_factory=list)
    safety_hits: list[str] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    serials: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(self.security_flags)

    @property
    def safety(self) -> bool:
        return bool(self.safety_hits)

    def to_dict(self) -> dict:
        return {"security_flags": self.security_flags, "safety_hits": self.safety_hits,
                "order_ids": self.order_ids, "serials": self.serials}


def screen_email(from_email: str, subject: str, body: str) -> ScreenResult:
    text = f"{subject}\n{body}"
    lowered = text.lower()
    result = ScreenResult()
    for pattern, label in INJECTION + PAYMENT_CHANGE:
        if re.search(pattern, text) and label not in result.security_flags:
            result.security_flags.append(label)
    domain = from_email.rsplit("@", 1)[-1] if "@" in from_email else ""
    impersonation = domain_impersonation(domain) if domain else None
    if impersonation:
        result.security_flags.append(impersonation)
    result.safety_hits = [name for name, rule in SAFETY_RULES if rule(lowered)]
    result.order_ids = list(dict.fromkeys(ORDER_RE.findall(text)))
    result.serials = list(dict.fromkeys(SERIAL_RE.findall(text)))
    return result
