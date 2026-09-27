"""Lab 03 - From free text to reliable structured data.

Objective
    Triage one support ticket into a typed record three ways - prompt-only JSON (fragile),
    structured outputs by hand (so you see the mechanism), and `client.messages.parse` with a
    Pydantic model (what you ship) - and understand why the last two are reliable.

Concepts
    Prompt-only JSON vs structured outputs (constrained decoding); JSON Schema from Pydantic;
    `output_config.format`; closed objects (additionalProperties: false); enums as guard rails;
    stop_reason checks; why assistant prefill is no longer the answer.

Run
    python day1_foundations/labs/03_structured_extraction.py [--ticket T-1101]

What to observe
    * The prompt-only reply may be chatty or fenced: `json.loads` fails and you end up writing
      fragile "tolerant" parsers that still don't guarantee valid enums or types.
    * With `output_config.format` the reply text IS the JSON, and it validates against the schema.
    * `messages.parse()` does the schema conversion, the request, and the validation for you.
"""

# test: expect=parsed_output

import argparse
import json
import re

from _triage import SYSTEM_PROMPT, TicketTriage, load_tickets, ticket_prompt
from labkit import MODEL, get_client, header, step, text_of


def naive_parse(text: str) -> dict:
    return json.loads(text)


def tolerant_parse(text: str) -> dict:
    """What people end up writing when they rely on prompt-only JSON. It still breaks eventually."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(candidate)


def closed_schema(schema: dict) -> dict:
    """Structured outputs require every object to be closed (additionalProperties: false)."""
    if isinstance(schema, dict):
        schema = {k: closed_schema(v) for k, v in schema.items()}
        if schema.get("type") == "object" or "properties" in schema:
            schema["additionalProperties"] = False
    elif isinstance(schema, list):
        schema = [closed_schema(v) for v in schema]
    return schema


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", default="T-1101")
    args = parser.parse_args()
    ticket = next(t for t in load_tickets() if t["ticket_id"] == args.ticket)
    client = get_client()
    header(f"Lab 03 - structured extraction for {ticket['ticket_id']}: {ticket['subject']}")
    print(ticket["body"])

    step(1, "Prompt-only JSON: ask nicely and hope")
    prompt_only = client.messages.create(
        model=MODEL, max_tokens=3000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": ticket_prompt(ticket) + "\nAnswer in JSON with the fields: category, "
                   "priority, product_line, order_id, sentiment, requires_human, language, summary."}],
    )
    raw = text_of(prompt_only)
    print("Raw reply:\n" + raw[:800])
    try:
        naive_parse(raw)
        print("json.loads succeeded this time - nothing guarantees it next time, or that the enums are valid.")
    except json.JSONDecodeError as exc:
        print(f"json.loads failed: {exc}")
        try:
            data = tolerant_parse(raw)
            print("A tolerant regex parser recovered it, but nothing guarantees valid enums or types:")
            print(f"  category={data.get('category')!r} priority={data.get('priority')!r}")
        except (json.JSONDecodeError, ValueError) as exc2:
            print(f"Even the tolerant parser failed: {exc2}")

    step(2, "Structured outputs by hand: output_config.format with a JSON Schema")
    schema = closed_schema(TicketTriage.model_json_schema())
    response = client.messages.with_raw_response.create(
        model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": ticket_prompt(ticket)}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    sent = json.loads(response.http_request.content)
    print("The output_config the API received:")
    print(json.dumps(sent["output_config"], indent=2)[:1500])
    message = response.parse()
    print(f"stop_reason={message.stop_reason}; reply text: {text_of(message)[:300]}")
    if message.stop_reason == "end_turn":
        by_hand = TicketTriage.model_validate_json(text_of(message))
        print(f"Validated with Pydantic -> category={by_hand.category}, priority={by_hand.priority}")

    step(3, "The convenient way: messages.parse with a Pydantic model")
    message = client.messages.parse(
        model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": ticket_prompt(ticket)}],
        output_format=TicketTriage,
    )
    print(f"stop_reason={message.stop_reason}")
    if message.stop_reason != "end_turn":
        print("Not a complete answer (refusal or max_tokens) - never trust parsed_output in that case.")
        return
    result: TicketTriage = message.parsed_output
    print("parsed_output (a validated TicketTriage instance):")
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    print("parse() converted the model to a closed JSON Schema (Literal -> enum, Optional -> anyOf with null), sent it "
          "as output_config.format, and validated the reply into the model.")

    step(4, "Using the result in code")
    route = {"safety_incident": "on-call field service (page now)", "warranty_claim": "warranty desk",
             "billing": "accounts receivable"}.get(result.category, "tier-1 support queue")
    print(f"Route to: {route}; priority {result.priority}; human needed: {result.requires_human}")


if __name__ == "__main__":
    main()
