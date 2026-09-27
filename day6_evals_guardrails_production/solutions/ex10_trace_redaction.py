"""Solution to exercise 10 - redact PII from traces at capture time, without losing what makes traces useful.

Lab 05 found raw requester email addresses in span attributes. Policy PRV-004 keeps agent traces for 90 days
and forbids full payment numbers anywhere, so the fix belongs in the tracer - before any exporter, log shipper
or vendor sees a span - not in a clean-up job afterwards.

Design
  * emails   -> keyed pseudonyms (HMAC-SHA256): the same sender always maps to the same token, so you can still
               join a customer's tickets, count repeat contacts, and debug - but only the key holder can re-identify.
               The domain is kept: it identifies the customer COMPANY, which per-customer metrics need.
  * IBANs / card numbers -> last 4 digits only (the same masking as the output guardrail)
  * phone numbers, street addresses -> placeholders
  * applied to span attributes AND events when a span ends, and once more at export (errors are recorded late)

Run
    python day6_evals_guardrails_production/solutions/ex10_trace_redaction.py
"""

# test: expect=PII values in exported traces: 0

from __future__ import annotations

import copy
import hashlib
import hmac
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _evalkit as ek  # noqa: E402
import _telemetry as tm  # noqa: E402
from _guardrails import mask_payment_numbers  # noqa: E402
from kestrel.support_agent import run_support_agent  # noqa: E402
from kestrel.support_tools import SupportDesk  # noqa: E402
from labkit import DATA_DIR, get_client, header, runs_dir, step  # noqa: E402
from labkit.data import scratch_db  # noqa: E402
from labkit.tracing import Span, Tracer  # noqa: E402

TICKETS = ["T-1001", "T-1103", "T-1005", "T-1101", "T-1201", "T-1206", "T-1207", "T-1403", "T-1507", "T-1801"]


class Redactor:
    def __init__(self, key: bytes, *, keep_domain: bool = True) -> None:
        self.key = key
        self.keep_domain = keep_domain

    def pseudonym(self, email: str) -> str:
        """Deliberately NOT email-shaped, so no downstream parser (or PII scanner) mistakes it for one."""
        digest = hmac.new(self.key, email.strip().lower().encode(), hashlib.sha256).hexdigest()[:12]
        domain = email.rsplit("@", 1)[-1].lower()
        return f"user:{digest}/{domain}" if self.keep_domain else f"user:{digest}"

    def text(self, value: str) -> str:
        value = tm.PII_PATTERNS["email"].sub(lambda m: self.pseudonym(m.group(0)), value)
        value = mask_payment_numbers(value)[0]
        value = tm.PII_PATTERNS["phone"].sub("[phone]", value)
        return tm.PII_PATTERNS["street_address"].sub("[address]", value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v) for v in value]
        return value

    def span(self, span: Span) -> None:
        span.attributes = self.value(span.attributes)
        span.events = self.value(span.events)


class RedactingTracer(Tracer):
    """A Tracer whose spans are redacted when they end and again at export."""

    def __init__(self, service: str, redactor: Redactor, **kwargs: Any) -> None:
        super().__init__(service, **kwargs)
        self.redactor = redactor

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        with super().span(name, **self.redactor.value(attributes)) as span:
            try:
                yield span
            finally:
                self.redactor.span(span)

    def export(self, path: Path | None = None) -> Path:
        for span in self.spans:                 # catches values set after the span body (e.g. error.message)
            self.redactor.span(span)
        return super().export(path)


def load_key() -> bytes:
    key = os.environ.get("KESTREL_TRACE_PSEUDONYM_KEY")
    if key:
        return key.encode()
    print("  (no KESTREL_TRACE_PSEUDONYM_KEY set - using a demo key; in production it lives in a secret store)")
    return b"demo-key-rotate-me"


def run(client, tickets: dict[str, dict], tracer_factory) -> dict[str, Tracer]:
    tracers = {}
    for tid in TICKETS:
        t = tickets[tid]
        tracer = tracer_factory()
        desk = SupportDesk(t["from_email"], db=scratch_db(f"day6_ex10_{tid}.db"), ticket_ref=tid)
        run_support_agent(client, f"Subject: {t['subject']}\n\n{t['body']}", t["from_email"], desk=desk,
                          tracer=tracer, ticket_ref=tid)
        tracers[tid] = tracer
    return tracers


def main() -> None:
    header("Exercise 10 - PII redaction for agent traces")
    client = get_client()
    tickets = {t["ticket_id"]: t for t in ek.load_jsonl(DATA_DIR / "support" / "tickets.jsonl")}
    redactor = Redactor(load_key())

    step(1, "Before: the plain tracer from lab 05")
    plain = run(client, tickets, lambda: Tracer("support-agent"))
    hits = [h for t in plain.values() for h in tm.find_pii(t.spans)]
    kinds = sorted({(h["kind"], h["field"]) for h in hits})
    print(f"  {len(hits)} PII values in {len(TICKETS)} traces: " + ", ".join(f"{k} in {f}" for k, f in kinds))

    step(2, "After: redact at capture")
    out = runs_dir("day6_traces_redacted")
    safe = run(client, tickets, lambda: RedactingTracer("support-agent", redactor))
    for tid, tracer in safe.items():
        tracer.export(out / f"{tid}.jsonl")
    exported = [s for tid in TICKETS for s in ek.load_jsonl(out / f"{tid}.jsonl")]
    print(f"  PII values in exported traces: {len(tm.find_pii(exported))}")
    before = plain["T-1005"].spans
    after = safe["T-1005"].spans
    esc_before = next(s for s in before if s.name == "tool.escalate_to_human").attributes["tool.input"]
    esc_after = next(s for s in after if s.name == "tool.escalate_to_human").attributes["tool.input"]
    print(f"  requester  before: {before[0].attributes['requester']}\n             after:  {after[0].attributes['requester']}")
    print(f"  escalation before: ...{esc_before[esc_before.find('new warehouse'):][:70]}\n"
          f"             after:  ...{esc_after[esc_after.find('new warehouse'):][:70]}")

    step(3, "Still useful: joins and metrics survive")
    same_sender = {safe[t].spans[0].attributes["requester"] for t in ("T-1001", "T-1103")}
    print(f"  T-1001 and T-1103 (same sender) share one pseudonym: {len(same_sender) == 1} -> {same_sender.pop()}")
    # Compare the SAME spans with and without redaction (two separate runs would differ in prompt-cache state).
    copies = {tid: copy.deepcopy(t.spans) for tid, t in plain.items()}
    for spans in copies.values():
        for span in spans:
            redactor.span(span)
    m_plain = tm.dashboard([tm.trace_metrics(t.spans) for t in plain.values()])
    m_safe = tm.dashboard([tm.trace_metrics(spans) for spans in copies.values()])
    keys = ("cost_mean_usd", "latency_p95_s", "escalation_rate", "tool_error_rate", "output_tokens_mean")
    same = all(abs(m_plain[k] - m_safe[k]) < 1e-9 for k in keys)
    print(f"  dashboard metrics identical with and without redaction: {same} ({', '.join(keys)})")

    step(4, "Operational notes")
    print("  * Keep the pseudonym key in a secret store; re-identification = access to the key (audited).\n"
          "  * Rotating the key breaks joins across the rotation date - rotate on a schedule, not ad hoc.\n"
          "  * Regexes miss free-text names ('Marcus Hale, Cobalt Chemical Works'); either don't log message text at\n"
          "    all (log IDs and lengths) or add an NER pass. Here tool inputs are truncated summaries - still review them.\n"
          "  * Retention (PRV-004): raw traces 90 days, then aggregate metrics only - enforce it in the backend.")


if __name__ == "__main__":
    main()
