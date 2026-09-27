"""Lab 07 - Errors, retries and refusals: the failure modes you must handle on day one.

Objective
    Trigger each class of failure deliberately and handle it correctly: invalid requests (400),
    unknown models (404), rate limits (429) and overload (529) with SDK retries, your own
    backoff for what the SDK doesn't retry, timeouts, and safety refusals with server-side
    fallbacks.

Concepts
    Typed exceptions (BadRequestError, NotFoundError, RateLimitError, OverloadedError,
    APIConnectionError/APITimeoutError); what the SDK retries automatically (408/409/429/5xx,
    connection errors; max_retries=2 by default); request ids for support tickets;
    stop_reason="refusal" + stop_details; `fallbacks="default"` (beta) on Claude Opus 5.

Run
    python day1_foundations/labs/07_errors_retries_refusals.py

What to observe
    * 400/404 are bugs in YOUR request: don't retry them, fix them.
    * 429/529 are transient: the SDK retries with exponential backoff (honouring retry-after).
    * A refusal is an HTTP 200: only stop_reason tells you. Fallbacks turn many refusals into answers.
    * Rate-limit, overload and refusal scenarios are SIMULATED with labkit's mock API even when you
      run live (we can't politely force the real API to fail); the SDK code paths are the real ones.
"""

# test: expect=OverloadedError

import random
import time

import anthropic

from labkit import MODEL, fallback_kwargs, get_client, header, mock_api, show_message, step, text_of

HELLO = [{"role": "user", "content": "Say hello to the Kestrel support team in five words."}]


def call_with_backoff(client, *, attempts: int = 4, base: float = 0.2, **params):
    """Application-level retry for errors the SDK has already given up on (e.g. sustained overload)."""
    for attempt in range(1, attempts + 1):
        try:
            return client.messages.create(**params)
        except (anthropic.RateLimitError, anthropic.OverloadedError, anthropic.InternalServerError) as exc:
            if attempt == attempts:
                raise
            delay = base * 2 ** (attempt - 1) * (1 + random.random())   # exponential backoff + jitter
            print(f"  attempt {attempt} failed with {type(exc).__name__} (request_id={exc.request_id}); "
                  f"sleeping {delay:.2f}s")
            time.sleep(delay)
    raise RuntimeError("unreachable")


def main() -> None:
    live = get_client()
    simulated = get_client(mode="mock")        # fault injection needs the mock, even in live mode
    header("Lab 07 - errors, retries and refusals")

    step(1, "400 - an invalid request (a parameter this model no longer accepts)")
    try:
        live.messages.create(model=MODEL, max_tokens=2000, messages=HELLO, extra_body={"temperature": 0.2})
    except anthropic.BadRequestError as exc:
        print(f"BadRequestError status={exc.status_code} request_id={exc.request_id}\n  {exc.message[:200]}")
        print("  -> Fix the request. Retrying a 400 can never succeed.")

    step(2, "404 - an unknown or retired model id")
    try:
        live.messages.create(model="claude-3-opus-20240229", max_tokens=2000, messages=HELLO)
    except anthropic.NotFoundError as exc:
        print(f"NotFoundError: {exc.message[:160]}")
        print("  -> Keep model ids in configuration and validate them at startup (client.models.retrieve).")

    step(3, "429 then 529 - transient errors the SDK retries for you (simulated)")
    mock_api().inject_faults(429, 529)
    start = time.perf_counter()
    ok = simulated.messages.create(model=MODEL, max_tokens=2000, messages=HELLO)
    print(f"Succeeded after the SDK's automatic retries in {time.perf_counter() - start:.2f}s: {text_of(ok)[:80]!r}")

    step(4, "When the SDK gives up - add your own backoff (simulated)")
    mock_api().inject_faults(529, 529, 529, 529)
    no_retry_client = simulated.with_options(max_retries=1)
    try:
        no_retry_client.messages.create(model=MODEL, max_tokens=2000, messages=HELLO)
    except anthropic.OverloadedError as exc:
        print(f"OverloadedError after SDK retries: status={exc.status_code} (529 is its own class, not a 5xx subclass)")
    mock_api().inject_faults(529, 529, 529)
    message = call_with_backoff(no_retry_client, model=MODEL, max_tokens=2000, messages=HELLO)
    print(f"Application-level backoff succeeded: {text_of(message)[:80]!r}")

    step(5, "Timeouts")
    print("Configure per client or per request: get_client(timeout=30) or client.with_options(timeout=5.0). "
          "The SDK retries timeouts too, so worst-case wall-clock ~ timeout x (max_retries + 1). For long "
          "generations prefer streaming over raising the timeout.")

    step(6, "Refusals: an HTTP 200 whose stop_reason is 'refusal' (simulated)")
    prompt = [{"role": "user", "content": "[simulate:refusal] Review the firewall rules for our plant network."}]
    refused = simulated.messages.create(model=MODEL, max_tokens=2000, messages=prompt)
    print(f"stop_reason={refused.stop_reason} content={refused.content} stop_details={refused.stop_details}")
    print("  -> Branch on stop_reason BEFORE reading content; content may be empty or partial.")

    step(7, "Server-side fallbacks: let the API retry a declined request on another model")
    kwargs = fallback_kwargs(MODEL)
    print(f"fallback kwargs for {MODEL}: {kwargs}")
    rescued = simulated.beta.messages.create(model=MODEL, max_tokens=2000, messages=prompt, **kwargs)
    show_message(rescued)
    served_by_fallback = any(it.type == "fallback_message" for it in (rescued.usage.iterations or []))
    print(f"served by fallback model: {served_by_fallback} (response.model={rescued.model})")
    print("Production code on Claude Opus 5 should opt in by default and log when a fallback served the answer.")


if __name__ == "__main__":
    main()
