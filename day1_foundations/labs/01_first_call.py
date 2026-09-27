"""Lab 01 - Anatomy of a Messages API call.

Objective
    Make one request to Claude and understand every field of the request and the response:
    content blocks, stop_reason, usage (and what it costs), the request id - and prove to
    yourself that the API is stateless.

Concepts
    POST /v1/messages; model / max_tokens / system / messages; content blocks
    (thinking, text); stop_reason; usage -> dollars; statelessness.

Run
    python day1_foundations/labs/01_first_call.py

What to observe
    * The response `content` is a LIST of typed blocks. On Claude Opus 5 the first block is
      usually `thinking` (thinking is on by default) - code that does `content[0].text` breaks.
    * `usage.output_tokens` includes thinking tokens: you pay for them.
    * The second call knows nothing about the first: every request must carry its own context.
"""

# test: expect=stop_reason

import json

from labkit import MODEL, get_client, header, show_message, step, text_of, usage_summary

SYSTEM = "You are a concise assistant for field engineers at Kestrel Pumps & Controls."


def main() -> None:
    client = get_client()
    header(f"Lab 01 - one request, one response ({MODEL})")

    step(1, "Send a request")
    request = {
        "model": MODEL,
        "max_tokens": 2000,         # a hard cap on thinking + answer tokens together
        "system": SYSTEM,           # instructions that frame the whole conversation
        "messages": [{"role": "user", "content": "In two sentences: what is cavitation in a centrifugal pump?"}],
    }
    print("Request body (what the SDK sends as JSON):")
    print(json.dumps(request, indent=2))
    message = client.messages.create(**request)

    step(2, "Inspect the response object")
    print(f"id={message.id}  request_id={message._request_id}  model={message.model}")
    print(f"content block types: {[block.type for block in message.content]}")
    show_message(message)

    step(3, "Get the text safely")
    # Never index content[0].text: filter by block type instead.
    answer = text_of(message)
    print(f"Answer ({len(answer)} chars): {answer[:300]}")

    step(4, "Usage and cost")
    usage = message.usage
    print(f"input_tokens={usage.input_tokens}  output_tokens={usage.output_tokens} "
          "(output includes any thinking tokens)")
    print("Cost of this call:", usage_summary(message))

    step(5, "The API is stateless")
    followup = client.messages.create(
        model=MODEL, max_tokens=2000, system=SYSTEM,
        messages=[{"role": "user", "content": "What did I just ask you about?"}],
    )
    print("Asked 'What did I just ask you about?' in a NEW request without history:")
    print("  ->", text_of(followup)[:300])
    print("Claude cannot know: nothing from the first request is stored server-side. Lab 02 shows how to carry "
          "context yourself.")

    step(6, "The raw JSON of the response (useful for logging and debugging)")
    raw = json.loads(message.to_json())
    for block in raw["content"]:
        if block["type"] == "thinking":
            block["signature"] = block["signature"][:24] + "..."   # opaque; must be passed back unchanged
    print(json.dumps(raw, indent=2)[:2500])


if __name__ == "__main__":
    main()
