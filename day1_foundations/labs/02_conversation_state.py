"""Lab 02 - Conversation state is YOUR job (and it costs tokens).

Objective
    Build a minimal conversation manager, see how the growing history is re-sent on every
    turn, and measure how input tokens (and cost) grow with the number of turns.

Concepts
    Multi-turn = resending history; role alternation; appending the FULL assistant content
    (including thinking blocks); per-turn token growth; why long chats and agent loops get
    expensive; count_tokens for estimating before you send.

Run
    python day1_foundations/labs/02_conversation_state.py

What to observe
    * Turn N's input tokens ~ the sum of all previous turns: cost grows roughly quadratically
      with conversation length unless you cache (Day 3) or trim context.
    * With history, Claude "remembers"; without it, it does not.
"""

# test: expect=Input tokens per turn

from labkit import MODEL, get_client, header, step, text_of

SYSTEM = "<day1_chat>\nYou are a friendly assistant for Kestrel Pumps & Controls customers. Keep answers short."


class Conversation:
    """The smallest useful conversation manager."""

    def __init__(self, client, model: str = MODEL, system: str = SYSTEM) -> None:
        self.client, self.model, self.system = client, model, system
        self.messages: list[dict] = []
        self.input_tokens_per_turn: list[int] = []

    def send(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        response = self.client.messages.create(model=self.model, max_tokens=2000, system=self.system,
                                               messages=self.messages)
        # Append the complete content list, not just the text: thinking blocks (and later, tool_use
        # blocks) must travel back unchanged or the next request can be rejected.
        self.messages.append({"role": "assistant", "content": response.content})
        self.input_tokens_per_turn.append(response.usage.input_tokens)
        return text_of(response)


def main() -> None:
    client = get_client()
    header("Lab 02 - carrying conversation state")

    step(1, "A conversation WITH history")
    chat = Conversation(client)
    for line in ["Hi, my name is Dana and I work at the Granite Bay north pump station.",
                 "We run two KP-400 pumps there.",
                 "What's my name?",
                 "Which site do I work at?"]:
        print(f"user> {line}")
        print(f"assistant> {chat.send(line)}")

    step(2, "The same question WITHOUT history")
    fresh = Conversation(client)
    question = "What's my name?"
    print(f"user> {question}")
    print(f"assistant> {fresh.send(question)}")

    step(3, "Input tokens per turn")
    print("Input tokens per turn:", chat.input_tokens_per_turn)
    for turn, tokens in enumerate(chat.input_tokens_per_turn, start=1):
        print(f"  turn {turn}: {'#' * max(1, tokens // 10)} {tokens}")
    total = sum(chat.input_tokens_per_turn)
    print(f"Total input tokens billed for {len(chat.input_tokens_per_turn)} turns: {total}. "
          "Each turn re-sends everything before it.")

    step(4, "Estimate before you send: count_tokens")
    estimate = client.messages.count_tokens(model=MODEL, system=SYSTEM,
                                            messages=chat.messages + [{"role": "user", "content": "Thanks!"}])
    print(f"A 5th turn would send ~{estimate.input_tokens} input tokens (count_tokens is free; it never runs the model).")

    step(5, "What the history looks like on the wire")
    for i, m in enumerate(chat.messages):
        content = m["content"]
        kinds = "text" if isinstance(content, str) else ", ".join(getattr(b, "type", "?") for b in content)
        print(f"  messages[{i}] role={m['role']:<9} blocks: {kinds}")


if __name__ == "__main__":
    main()
