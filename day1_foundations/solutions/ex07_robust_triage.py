"""Solution - Exercise 7: a triage call that never lies.

Every path returns either a *validated* result or an explicit, conservative "route to a human" result,
together with a record of what happened.  Each path is exercised deterministically with the mock:
    * max_tokens truncation  -> a deliberately tiny first budget
    * refusal                -> the "[simulate:refusal]" marker
    * transient 529s          -> mock_api().inject_faults(...)
"""

# test: expect=all paths verified

import sys
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _triage import SYSTEM_PROMPT, TicketTriage, load_tickets, ticket_prompt  # noqa: E402
from labkit import MODEL, fallback_kwargs, get_client, header, mock_api, step  # noqa: E402

CONSERVATIVE = TicketTriage(category="other", priority="P2", product_line="none", order_id=None,
                            sentiment="neutral", requires_human=True, language="en",
                            summary="Automatic triage unavailable; routed to a human for manual triage.")


@dataclass
class TriageOutcome:
    result: TicketTriage
    path: list[str] = field(default_factory=list)
    automated: bool = True


def _call(client, ticket: dict, max_tokens: int, *, with_fallbacks: bool = False):
    params = dict(model=MODEL, max_tokens=max_tokens, output_format=TicketTriage,
                  system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                  messages=[{"role": "user", "content": ticket_prompt(ticket)}])
    if with_fallbacks:
        return client.beta.messages.parse(**params, **fallback_kwargs(MODEL))
    return client.messages.parse(**params)


def robust_triage(client, ticket: dict, *, max_tokens: int = 4000) -> TriageOutcome:
    outcome = TriageOutcome(result=CONSERVATIVE)
    try:
        message = _call(client, ticket, max_tokens)
    except (anthropic.RateLimitError, anthropic.OverloadedError, anthropic.InternalServerError,
            anthropic.APIConnectionError) as exc:          # the SDK already retried; don't loop forever
        outcome.path.append(f"transient failure after SDK retries: {type(exc).__name__}")
        outcome.automated = False
        return outcome
    outcome.path.append(f"first call: stop_reason={message.stop_reason}")

    if message.stop_reason == "max_tokens":
        message = _call(client, ticket, max(max_tokens * 4, 8000))
        outcome.path.append(f"retried with larger max_tokens: stop_reason={message.stop_reason}")

    if message.stop_reason == "refusal":
        message = _call(client, ticket, max(max_tokens, 4000), with_fallbacks=True)
        served = any(it.type == "fallback_message" for it in (message.usage.iterations or []))
        outcome.path.append(f"retried with server-side fallbacks: stop_reason={message.stop_reason}, "
                            f"served_by_fallback={served} ({message.model})")

    if message.stop_reason != "end_turn" or message.parsed_output is None:
        outcome.path.append("no trustworthy result -> conservative human routing")
        outcome.automated = False
        return outcome
    outcome.result = message.parsed_output
    return outcome


def main() -> None:
    client = get_client()
    simulated = get_client(mode="mock")
    header("Exercise 7 - robust triage")
    ticket = next(t for t in load_tickets() if t["ticket_id"] == "T-1102")

    step(1, "Happy path")
    o = robust_triage(client, ticket)
    print(o.path, "->", o.result.category, o.result.priority)
    assert o.automated

    step(2, "max_tokens truncation (tiny first budget)")
    o = robust_triage(simulated, ticket, max_tokens=50)
    print(o.path, "->", o.result.category)
    assert any("larger max_tokens" in p for p in o.path) and o.automated

    step(3, "Refusal rescued by server-side fallbacks")
    refused_ticket = {**ticket, "body": "[simulate:refusal] " + ticket["body"]}
    o = robust_triage(simulated, refused_ticket)
    print(o.path, "->", o.result.category)
    assert any("fallbacks" in p for p in o.path) and o.automated

    step(4, "Sustained overload beyond the SDK's retries")
    mock_api().inject_faults(529, 529, 529)
    o = robust_triage(simulated.with_options(max_retries=2), ticket)
    print(o.path, "->", o.result.summary)
    assert not o.automated and o.result.requires_human

    print("\nall paths verified")


if __name__ == "__main__":
    main()
