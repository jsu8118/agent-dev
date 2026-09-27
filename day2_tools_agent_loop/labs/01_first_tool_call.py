"""Lab 01 - Your first tool call, done by hand.

Objective
    Give Claude ONE tool (order status from Kestrel's read-only ops DB), and walk through a complete
    tool-use round trip manually: request -> tool_use block -> YOUR code runs the function ->
    tool_result -> final answer.  Print every content block and the JSON shape of each request and
    response, so nothing about the protocol is hidden.

Concepts
    Tool definition (name, description, input_schema, strict); the model never executes anything;
    tool_use / tool_result blocks and their ids; stop_reason "tool_use" vs "end_turn"; statelessness
    (request 2 re-sends the whole history, including the tool definitions).

Run
    python day2_tools_agent_loop/labs/01_first_tool_call.py

What to observe
    * Response 1 ends with stop_reason="tool_use" and contains a tool_use block: a name, an id, and
      input that matches your schema.  Nothing has been executed yet.
    * Your code runs the SQL query.  The result goes back as a tool_result block whose tool_use_id
      matches the tool_use id, inside a USER message.
    * Request 2 carries the user question, the assistant turn (verbatim, thinking block included) and the
      tool result: input tokens grow because the API keeps no state between calls.
"""

# test: expect=stop_reason=tool_use
# test: expect=stop_reason=end_turn

from __future__ import annotations

import json

from labkit import MODEL, get_client, header, show_message, step, text_of, usage_summary
from labkit.data import ops_db

SYSTEM = ("<day2_order_desk>\nYou are the order-desk assistant of Kestrel Pumps & Controls. Answer staff questions "
          "about orders using the tools; never guess. Today is 2026-09-15.")

# The tool definition is the model's ONLY documentation of your function: say what it returns and WHEN to use it.
ORDER_TOOL = {
    "name": "get_order_status",
    "description": ("Look up one Kestrel sales order: status, promised date, and shipment details (carrier, tracking "
                    "number, ship date, ETA, delivered date). Use it whenever someone asks where an order is or when "
                    "it will arrive. Order IDs look like SO-10234."),
    "strict": True,                               # the API guarantees the input matches the schema
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "Sales order ID, e.g. SO-10234"}},
        "required": ["order_id"],
        "additionalProperties": False,            # required by strict mode: no invented extra arguments
    },
}


def get_order_status(order_id: str) -> dict:
    """The actual function. Claude never runs this - your process does, with your credentials."""
    db = ops_db()                                 # read-only connection to the ERP extract
    order = db.execute("SELECT order_id, status, promised_date FROM orders WHERE order_id = ?",
                       (order_id.strip().upper(),)).fetchone()
    if order is None:
        return {"error": f"Order {order_id} not found."}
    ship = db.execute("SELECT carrier, tracking_number, ship_date, eta_date, delivered_date FROM shipments "
                      "WHERE order_id = ?", (order["order_id"],)).fetchone()
    return {**dict(order), "shipment": dict(ship) if ship else None}


def block_json(block) -> dict:
    """A content block as the JSON the API sends/receives (thinking signatures shortened for display)."""
    data = block.to_dict() if hasattr(block, "to_dict") else dict(block)
    if data.get("type") == "thinking":
        data["signature"] = data["signature"][:20] + "...(opaque, must be sent back unchanged)"
    return data


def request_shape(request: dict) -> str:
    """Compact view of a request's conversation: role -> block types."""
    lines = []
    for m in request["messages"]:
        content = m["content"]
        kinds = ["text"] if isinstance(content, str) else [
            (b.get("type") if isinstance(b, dict) else b.type) for b in content]
        lines.append(f"  {m['role']:<9} {kinds}")
    return "\n".join(lines)


def main() -> None:
    client = get_client()
    header(f"Lab 01 - one tool, one round trip, by hand ({MODEL})")

    step(1, "Request 1: the question plus the tool definition")
    question = ("Jorge Medina at GreenValley Irrigation asks for the tracking number of SO-10303 so he can plan the "
                "unloading crew. What should I tell him?")
    request1 = {"model": MODEL, "max_tokens": 4000, "system": SYSTEM, "tools": [ORDER_TOOL],
                "messages": [{"role": "user", "content": question}]}
    print(json.dumps(request1, indent=2))
    response1 = client.messages.create(**request1)

    step(2, "Response 1: Claude asks US to run the tool")
    show_message(response1)
    print("\nRaw content blocks (JSON):")
    print(json.dumps([block_json(b) for b in response1.content], indent=2))
    tool_uses = [b for b in response1.content if b.type == "tool_use"]
    if response1.stop_reason != "tool_use" or not tool_uses:
        print("Claude answered without the tool this time; nothing to execute. (Re-run to see a tool call.)")
        return

    step(3, "Run the tool locally and build the tool_result block")
    results = []
    for call in tool_uses:
        output = get_order_status(**call.input)   # input already matches the schema (strict: true)
        print(f"executed {call.name}({call.input}) -> {output}")
        results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(output),
                        "is_error": "error" in output})
    print("\ntool_result block(s) we will send:")
    print(json.dumps(results, indent=2))

    step(4, "Request 2: re-send EVERYTHING - the API has no memory of request 1")
    request2 = {**request1, "messages": [
        *request1["messages"],
        {"role": "assistant", "content": response1.content},   # verbatim: thinking + text + tool_use blocks
        {"role": "user", "content": results},                  # every tool_result for that turn, in ONE message
    ]}
    print("Conversation shape (role -> content block types):")
    print(request_shape(request2))
    response2 = client.messages.create(**request2)

    step(5, "Response 2: the final answer")
    show_message(response2)
    print(f"\nAnswer for the staff member:\n  {text_of(response2)}")

    step(6, "What the round trip cost")
    print(f"request 1: {usage_summary(response1)}")
    print(f"request 2: {usage_summary(response2)}")
    growth = response2.usage.input_tokens - response1.usage.input_tokens
    print(f"Request 2 re-sent request 1 (system + tool definition + question) plus the assistant turn and the tool "
          f"result: +{growth} input tokens. Every extra agent turn repeats this.")


if __name__ == "__main__":
    main()
