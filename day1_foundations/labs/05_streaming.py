"""Lab 05 - Streaming responses.

Objective
    Stream a drafted customer reply token by token, measure time-to-first-token (TTFT) vs total
    time, look at the raw event types, and still get the complete Message at the end.

Concepts
    Server-sent events (message_start, content_block_start/delta/stop, message_delta,
    message_stop); `client.messages.stream()` + `text_stream` + `get_final_message()`;
    thinking deltas (with display="summarized"); why long or large-max_tokens requests
    should stream (the SDK refuses very large non-streaming requests to avoid HTTP timeouts).

Run
    python day1_foundations/labs/05_streaming.py [--ticket T-1101]

What to observe
    * Text arrives in chunks; TTFT is what users feel as latency.
    * On Claude Opus 5 thinking may precede the text: with display "omitted" (the default) you see
      a pause; "summarized" streams a readable summary of the reasoning.
    * The final message carries usage just like a non-streaming call.
"""

# test: expect=Event counts

import argparse
import time
from collections import Counter

from _triage import load_tickets
from labkit import MODEL, get_client, header, step

SYSTEM = ("<day1_reply_drafter>\nYou draft email replies for Kestrel Pumps & Controls customer support. Be warm, "
          "specific and brief (under 150 words). Never promise dates or compensation you have not been given.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", default="T-1101")
    args = parser.parse_args()
    ticket = next(t for t in load_tickets() if t["ticket_id"] == args.ticket)
    client = get_client()
    header("Lab 05 - streaming a drafted reply")
    prompt = f"<ticket>\nFrom: {ticket['from_email']}\nSubject: {ticket['subject']}\n\n{ticket['body']}\n</ticket>\n\n" \
             "Draft a holding reply while we investigate."

    step(1, "Stream text as it is generated")
    start = time.perf_counter()
    first_token_at = None
    with client.messages.stream(model=MODEL, max_tokens=4000, system=SYSTEM,
                                messages=[{"role": "user", "content": prompt}]) as stream:
        for text in stream.text_stream:
            if first_token_at is None:
                first_token_at = time.perf_counter() - start
            print(text, end="", flush=True)
        final = stream.get_final_message()
    total = time.perf_counter() - start
    print(f"\n\nTTFT={first_token_at or 0:.2f}s total={total:.2f}s stop_reason={final.stop_reason} "
          f"output_tokens={final.usage.output_tokens}")

    step(2, "The raw event stream (with summarized thinking)")
    counts: Counter = Counter()
    with client.messages.stream(model=MODEL, max_tokens=4000, system=SYSTEM,
                                thinking={"type": "adaptive", "display": "summarized"},
                                messages=[{"role": "user", "content": prompt}]) as stream:
        for event in stream:
            counts[event.type] += 1
            if event.type == "content_block_start":
                print(f"\n[{event.content_block.type} block starts]")
            elif event.type == "content_block_delta" and event.delta.type == "thinking_delta":
                print(event.delta.thinking, end="", flush=True)
            elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                print(event.delta.text, end="", flush=True)
        final = stream.get_final_message()
    print("\n\nEvent counts:", dict(counts))
    print("Final content blocks:", [b.type for b in final.content])
    print("Tip: the SDK also emits convenience events ('text', 'thinking', 'input_json') alongside the raw ones.")


if __name__ == "__main__":
    main()
