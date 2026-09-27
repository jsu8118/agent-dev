"""Lab 06 - Adaptive thinking and the effort dial.

Objective
    See what thinking costs, how `effort` trades quality for tokens and latency, and how a too-small
    max_tokens truncates the answer because thinking and text share the same budget.

Concepts
    Adaptive thinking (on by default on Claude Opus 5); `output_config.effort`
    (low | medium | high | xhigh | max); thinking display (omitted vs summarized); thinking tokens
    are billed as output tokens; max_tokens = hard cap on thinking + text; why temperature and
    budget_tokens are gone on current models (and what replaces them).

Run
    python day1_foundations/labs/06_thinking_and_effort.py [--model claude-opus-5]

What to observe
    * Output tokens (and cost) rise with effort; on easy tasks the answer often doesn't change.
    * With a tiny max_tokens you get stop_reason="max_tokens" and a cut-off (or empty) answer.
    * In mock mode token counts are simulated from the effort level; run live for real numbers.
"""

# test: expect=effort sweep

import argparse
import time

from labkit import MODEL, get_client, get_spec, header, show_message, step, supports_effort, text_of, usage_summary

POLICY_SYSTEM = ("<day1_policy_math>\nYou apply Kestrel's returns policy RET-002: non-defective returns are accepted "
                 "within 30 calendar days of delivery, unused and in original packaging, with a 15% restocking fee on "
                 "the returned line value. Answer with the eligibility decision and the refund amount, showing the "
                 "arithmetic in one short paragraph.")
QUESTION = ("A key-tier customer returns 4 of the 10 MS-250 seal kits they bought at $559.36 each. The kits were "
            "delivered 18 days ago and are unopened. Are they eligible, and what is the refund?")


def ask(client, model: str, *, effort: str | None = None, max_tokens: int = 4000, thinking: dict | None = None):
    kwargs = {}
    if effort:
        kwargs["output_config"] = {"effort": effort}
    if thinking:
        kwargs["thinking"] = thinking
    start = time.perf_counter()
    message = client.messages.create(model=model, max_tokens=max_tokens, system=POLICY_SYSTEM,
                                     messages=[{"role": "user", "content": QUESTION}], **kwargs)
    return message, time.perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    client = get_client()
    spec = get_spec(args.model)
    header(f"Lab 06 - thinking & effort on {spec.display_name}")
    print(f"thinking when omitted: {spec.thinking_default}; effort levels: {spec.effort_levels or 'not supported'}; "
          f"default effort: {spec.default_effort}")

    step(1, "See the reasoning: display='summarized'")
    thinking = {"type": "adaptive", "display": "summarized"} if spec.supports_adaptive else None
    message, _ = ask(client, args.model, thinking=thinking)
    show_message(message)

    step(2, "The effort sweep")
    levels = [lvl for lvl in ("low", "medium", "high", "xhigh", "max") if supports_effort(args.model, lvl)]
    if not levels:
        print(f"{args.model} does not accept `effort`; its thinking is controlled with budget_tokens instead.")
    print(f"{'effort':<8} {'out_tokens':>10} {'seconds':>8}  answer")
    for level in levels:
        message, seconds = ask(client, args.model, effort=level)
        answer = " ".join(text_of(message).split())
        print(f"{level:<8} {message.usage.output_tokens:>10} {seconds:>8.2f}  {answer[:90]}")
    print("Read the table as a cost/latency curve: pick the lowest effort that keeps your eval scores (Day 6).")

    step(3, "max_tokens caps thinking + text together")
    message, _ = ask(client, args.model, effort=levels[-1] if levels else None, max_tokens=60)
    print(f"max_tokens=60 -> stop_reason={message.stop_reason}; blocks={[b.type for b in message.content]}; "
          f"text={text_of(message)!r}")
    print("A truncated answer is not an answer: check stop_reason before using content, and size max_tokens for "
          "thinking plus the reply (>= 2,000 even for short replies on thinking models).")

    step(4, "Parameters that no longer exist on current models")
    for label, extra in [("temperature=0", {"extra_body": {"temperature": 0}}),
                         ("budget_tokens", {"thinking": {"type": "enabled", "budget_tokens": 2048}})]:
        try:
            client.messages.create(model=args.model, max_tokens=4000,
                                   messages=[{"role": "user", "content": "hi"}], **extra)
            print(f"{label}: accepted by {args.model}")
        except Exception as exc:  # anthropic.BadRequestError
            print(f"{label}: rejected by {args.model} -> {type(exc).__name__}: {str(exc)[:140]}")
    print("Consistency now comes from structure (structured outputs, clear rubrics, evals), not sampling knobs.")
    print("\nLast call cost:", usage_summary(message))


if __name__ == "__main__":
    main()
