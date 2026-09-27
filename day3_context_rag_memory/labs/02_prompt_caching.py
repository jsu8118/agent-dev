"""Lab 02 - Prompt caching: pay once for the manuals, read them at a tenth of the price.

Objective
    Put Kestrel's pump manuals in a cached system prompt, ask several questions and watch cache writes turn into
    cache reads; break the cache with a timestamp (a classic silent invalidator) and fix it; compute the savings and
    the break-even point for the 5-minute and 1-hour TTLs; then run a multi-turn chat with automatic caching and
    inject a mid-conversation system message without losing the cached prefix.

Concepts
    prefix match (tools -> system -> messages), cache_control breakpoints (max 4), usage.cache_creation_input_tokens /
    cache_read_input_tokens, silent invalidators, write 1.25x (5 min) / 2x (1 h) vs read 0.1x, break-even,
    automatic (top-level) caching, the "explicit static prefix + automatic tail" combination, mid-conversation
    {"role": "system"} messages on Claude Opus 5, caching vs batching.

Run
    python day3_context_rag_memory/labs/02_prompt_caching.py

What to observe
    * Request 1 writes the manual library to the cache; requests 2-4 read it (cache_read ~ the whole library).
    * With a timestamp in the system prompt every request writes and none reads - 25% MORE than not caching.
    * Moving the timestamp after the breakpoint (into the user turn) restores reads immediately.
    * In the chat, cache_read grows turn by turn while cache_write stays about one turn's worth.
    * A {"role": "system"} message keeps the cached conversation; editing the top-level system prompt re-bills it.
"""
# test: expect=silent invalidator
# test: expect=Break-even

from __future__ import annotations

import datetime as dt
import hashlib

from labkit import MODEL, get_client, header, is_mock, step, text_of, wrap
from labkit.pricing import _get

import _day3 as d3

SYSTEM_HEAD = """\
<day3_manual_library>
You are Kestrel's field-service assistant. Technicians ask you short technical questions on site. Answer ONLY from \
the manuals below. After each fact, cite the manual code and section in brackets, e.g. [IOM-KP250 §6]. If the \
manuals don't cover it, say so. Keep answers under 120 words.
</day3_manual_library>

"""
QUESTIONS = [
    "How often do I regrease the KP-250 bearings, and with how much grease?",
    "What are the vibration limits for a KP-400 on a steel-frame (flexible) foundation?",
    "The KC-1 shows F05 on hot afternoons. What should I check first?",
    "What torque do the KP-250 baseplate foundation bolts need?",
]


def manual_system() -> list[dict]:
    """The stable prefix: identical bytes on every request, so every request after the first can read it."""
    library = d3.library_text(d3.load_docs(("manual",)))
    return [{"type": "text", "text": SYSTEM_HEAD + library, "cache_control": {"type": "ephemeral"}}]


def usage_row(label: str, response) -> list:
    u = response.usage
    return [label, u.input_tokens, u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0,
            u.output_tokens, d3.money(d3.response_cost(response))]


HEADERS = ["request", "uncached in", "cache write", "cache read", "out", "cost"]


def uncached_cost(responses: list) -> float:
    """What the same token volumes would have cost with no caching at all."""
    p_in, p_out = d3.input_price(MODEL), d3.output_price(MODEL)
    return sum(d3.prompt_size(r.usage) * p_in + r.usage.output_tokens * p_out for r in responses)


def ask(client, system: list[dict], question: str):
    return client.messages.create(model=MODEL, max_tokens=2000, system=system,
                                  messages=[{"role": "user", "content": question}])


def step_warm_cache(client) -> tuple[list, int]:
    system = manual_system()
    responses, rows = [], []
    for i, q in enumerate(QUESTIONS, 1):
        r = ask(client, system, q)
        responses.append(r)
        rows.append(usage_row(f"Q{i}", r))
    d3.table(rows, HEADERS)
    print("\nQ1 answer:\n" + wrap(text_of(responses[0])))
    actual, baseline = sum(d3.response_cost(r) for r in responses), uncached_cost(responses)
    p_in = d3.input_price(MODEL)
    input_actual = sum(d3.response_cost(r) - r.usage.output_tokens * d3.output_price(MODEL) for r in responses)
    input_plain = sum(d3.prompt_size(r.usage) for r in responses) * p_in
    print(f"\nTotal {d3.money(actual)} vs {d3.money(baseline)} without caching ({1 - actual / baseline:.0%} cheaper); "
          f"input side alone {d3.money(input_actual)} vs {d3.money(input_plain)} ({1 - input_actual / input_plain:.0%} "
          "cheaper). Output tokens are never cached.")
    prefix = responses[0].usage.cache_creation_input_tokens or 0
    return responses, prefix


def first_difference(a: str, b: str) -> int:
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


def step_invalidator(client) -> None:
    base = manual_system()[0]["text"]
    rows, bodies = [], []
    for i, q in enumerate(QUESTIONS[:3], 1):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        text = f"Current time: {stamp}\n" + base          # looks harmless; changes the first bytes every time
        bodies.append(text)
        r = ask(client, [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}], q)
        rows.append(usage_row(f"broken Q{i}", r))
    d3.table(rows, HEADERS)
    pos = first_difference(bodies[0], bodies[1])
    print(f"\nDiagnosis: sha256 of the system prompt differs between requests "
          f"({hashlib.sha256(bodies[0].encode()).hexdigest()[:10]} vs {hashlib.sha256(bodies[1].encode()).hexdigest()[:10]}); "
          f"first differing character at position {pos}: {bodies[0][max(0, pos - 20):pos + 12]!r}")
    print("Every request pays the 1.25x write and nothing is ever read: a broken cache costs MORE than no cache.")

    rows = []
    for i, q in enumerate(QUESTIONS[:3], 1):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        r = client.messages.create(model=MODEL, max_tokens=2000, system=manual_system(),
                                   messages=[{"role": "user", "content": f"(sent at {stamp})\n{q}"}])
        rows.append(usage_row(f"fixed Q{i}", r))
    print("\nFix: keep the system prompt byte-identical; put volatile values after the last breakpoint (user turn).")
    d3.table(rows, HEADERS)


def step_economics(client, prefix: int) -> None:
    p = d3.input_price(MODEL)
    print(f"Prefix measured in step 1: {prefix:,} tokens. Input-side cost for n requests sharing it "
          f"(write 1.25x or 2x once, then read 0.1x):")
    rows = []
    for n in range(1, 7):
        none, m5, h1 = n * prefix * p, (1.25 + 0.1 * (n - 1)) * prefix * p, (2.0 + 0.1 * (n - 1)) * prefix * p
        rows.append([n, d3.money(none), d3.money(m5), d3.money(h1),
                     ", ".join(name for name, c in (("5-min", m5), ("1-hour", h1)) if c < none) or "-"])
    d3.table(rows, ["n", "no cache", "5-min TTL", "1-hour TTL", "cheaper than no cache"])
    print("Break-even: 2 requests for the 5-minute TTL (1.25 + 0.1 < 2), 3 for the 1-hour TTL (2 + 0.2 < 3).")

    gap_n = 6
    m5_gap = gap_n * 1.25 * prefix * p                  # every request arrives after the 5-min entry expired
    h1_gap = (2.0 + 0.1 * (gap_n - 1)) * prefix * p
    print(f"\nTraffic with gaps: one question every 12 minutes for an hour ({gap_n} requests):")
    d3.table([["no cache", d3.money(gap_n * prefix * p)], ["5-minute TTL (expires between requests)", d3.money(m5_gap)],
              ["1-hour TTL (each read refreshes it)", d3.money(h1_gap)]], ["strategy", "input cost"])

    policies = d3.library_text(d3.load_docs(("policy",)))
    r = client.messages.create(
        model=MODEL, max_tokens=2000,
        system=[{"type": "text", "text": SYSTEM_HEAD + policies, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[{"role": "user", "content": "Is impeller pitting from cavitation covered under warranty?"}])
    breakdown = _get(r.usage, "cache_creation", None)
    print(f"\nA 1-hour write on the policy library: cache_creation_input_tokens={r.usage.cache_creation_input_tokens:,} "
          f"(ephemeral_1h_input_tokens={_get(breakdown, 'ephemeral_1h_input_tokens'):,}), billed at 2x: "
          f"{d3.money(d3.response_cost(r))}.")

    n_jobs = 1000
    sync_cached = (1.25 * prefix + (n_jobs - 1) * 0.1 * prefix) * p
    batch_plain = n_jobs * prefix * p * 0.5
    batch_cached = 0.5 * sync_cached
    print(f"\nCaching vs batching for {n_jobs:,} overnight questions over the same {prefix:,}-token prefix (input only):")
    d3.table([["synchronous, cached", d3.money(sync_cached)], ["Batches API, no cache_control", d3.money(batch_plain)],
              ["Batches API + cache_control (hits are best-effort)", d3.money(batch_cached) + " or more"]],
             ["strategy", "input cost"])


def step_chat(client) -> list[dict]:
    """Automatic caching: one top-level cache_control that moves with the tail, plus the explicit system breakpoint."""
    system = manual_system()
    turns = ["I'm about to service a KP-250 at Harbor Foods. How often should its bearings be regreased?",
             "And on the KP-400 split-case pumps?",
             "What torque for the KP-250 baseplate foundation bolts?",
             "One more: the KC-1 on that skid showed F05 yesterday afternoon. First thing to check?"]
    messages: list[dict] = []
    rows, replies = [], []
    for i, text in enumerate(turns, 1):
        messages.append({"role": "user", "content": text})
        r = client.messages.create(model=MODEL, max_tokens=2000, system=system, messages=messages,
                                   cache_control={"type": "ephemeral"})
        messages.append({"role": "assistant", "content": r.content})   # full content: thinking blocks included
        replies.append(r)
        u = r.usage
        prompt = d3.prompt_size(u)
        rows.append([f"turn {i}", prompt, u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0,
                     u.input_tokens, f"{(u.cache_read_input_tokens or 0) / prompt:.0%}"])
    d3.table(rows, ["request", "prompt", "cache read", "cache write", "uncached", "read share"])
    print("Healthy-loop signature: reads cover the whole prior prefix; writes are about one turn's worth.")
    print("Turn 2 ('And on the KP-400...?') answer:\n" + wrap(text_of(replies[1])))
    return messages


def step_mid_conversation_system(client, history: list[dict]) -> None:
    question = "Remind me of the cavitation checks."
    instruction = "The technician is now on the plant floor wearing gloves: reply in at most three short bullet points."
    system = manual_system()

    via_message = client.messages.create(
        model=MODEL, max_tokens=2000, system=system, cache_control={"type": "ephemeral"},
        messages=history + [{"role": "user", "content": question}, {"role": "system", "content": instruction}])
    edited = [{**system[0], "text": system[0]["text"] + "\n" + instruction}]
    via_edit = client.messages.create(model=MODEL, max_tokens=2000, system=edited, cache_control={"type": "ephemeral"},
                                      messages=history + [{"role": "user", "content": question}])
    d3.table([usage_row('{"role": "system"} message', via_message), usage_row("edited top-level system", via_edit)],
             HEADERS)
    print("Answer with the system message:\n" + wrap(text_of(via_message)))


def main() -> None:
    client = get_client()
    header("Lab 02 - Prompt caching")
    if is_mock():
        print("(mock mode: the cache is simulated - same prefix rules, TTLs, minimums and prices as the real API)")

    step(1, "Four questions against a cached manual library")
    _, prefix = step_warm_cache(client)

    step(2, "A silent invalidator: a timestamp at the top of the system prompt")
    step_invalidator(client)

    step(3, "Break-even, TTL choice, and caching vs batching")
    step_economics(client, prefix)

    step(4, "Multi-turn chat with automatic caching (top-level cache_control)")
    history = step_chat(client)

    step(5, "Changing instructions mid-conversation without losing the cache")
    step_mid_conversation_system(client, history)


if __name__ == "__main__":
    main()
