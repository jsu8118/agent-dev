"""Lab 05 - Tool results are prompt: measure what raw dumps cost.

Objective
    Run the same five-question order-desk session twice - once with a tool that returns raw table rows
    (SELECT * from every related table, pretty-printed), once with the curated compact JSON of
    _order_desk.py - and measure context growth (count_tokens after every question), billed input tokens
    and cost.  Then measure the fixed overhead of tool DEFINITIONS with count_tokens.

Concepts
    Every tool result stays in the history and is re-sent on every later request; context cost of result
    design; data minimisation (PRV-004 "minimum necessary"); tool-definition token overhead; count_tokens.

Run
    python day2_tools_agent_loop/labs/05_tool_result_design.py

What to observe
    * The raw-row variant answers the same questions, but every look-up leaves several times more tokens in
      the history - and the gap compounds, because later requests re-send earlier results.
    * The raw rows also leak fields nobody asked for (phone numbers, credit limits): a privacy problem, not
      just a cost problem.
    * Tool definitions are a fixed tax on EVERY request of the conversation (a tool-use system prompt plus
      each schema); eleven tools cost more than three.
"""

# test: expect=Context size after each question
# test: expect=Tool-definition overhead

from __future__ import annotations

import json

from _order_desk import INVOICE_ID, ORDER_ID, SYSTEM_PROMPT, TOOLS, OrderDesk, ToolError
from kestrel.support_tools import TOOLS as SUPPORT_TOOLS
from labkit import MODEL, cost_usd, get_client, header, is_mock, step, text_of

SESSION = [
    "Where is SO-10303?",
    "And SO-10300?",
    "What's the status of SO-10285?",
    "Has SO-10283 been delivered, and when?",
    "Which of these orders are still in transit?",
]
MONTHLY_TICKETS = 1_900


class RawRowsDesk(OrderDesk):
    """The tempting first version: return every row of every related table, pretty-printed."""

    def get_order_status(self, order_id: str) -> dict:
        oid = self._normalise(order_id)
        if not ORDER_ID.match(oid) or INVOICE_ID.match(oid):
            raise ToolError(f"'{order_id}' is not a valid order ID (SO-12345).")
        orders = self._query("SELECT * FROM orders WHERE order_id = ?", (oid,))
        if not orders:
            raise ToolError(f"Order {oid} not found.")
        return {
            "orders": orders,
            "order_lines": self._query("SELECT * FROM order_lines l JOIN products p USING (sku) WHERE order_id = ?",
                                       (oid,)),
            "shipments": self._query("SELECT * FROM shipments WHERE order_id = ?", (oid,)),
            "invoices": self._query("SELECT * FROM invoices WHERE order_id = ?", (oid,)),
            "customers": self._query("SELECT * FROM customers WHERE customer_id = ?", (orders[0]["customer_id"],)),
        }

    def run(self, name: str, tool_input: dict) -> tuple[str, bool]:
        content, is_error = super().run(name, tool_input)
        return (json.dumps(json.loads(content), indent=2), is_error)      # pretty-printed, as dumps often are


class Session:
    """One staff member, one conversation, several questions - the history keeps every tool result."""

    def __init__(self, client, desk: OrderDesk) -> None:
        self.client, self.desk = client, desk
        self.messages: list[dict] = []
        self.billed_input = self.output = self.api_calls = 0
        self.cost = 0.0

    def ask(self, question: str, max_turns: int = 6) -> str:
        # A deliberately minimal loop (it only runs tools on stop_reason == "tool_use"); lab 02 has the full
        # stop-reason policy you would use in production.
        self.messages.append({"role": "user", "content": question})
        for _ in range(max_turns):
            response = self.client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT, tools=TOOLS,
                                                   messages=self.messages)
            u = response.usage
            self.api_calls += 1
            self.billed_input += u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
            self.output += u.output_tokens
            self.cost += cost_usd(u, response.model)
            self.messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                return text_of(response)
            results = []
            for b in calls:
                content, is_error = self.desk.run(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
        return "(stopped: max turns)"

    def context_tokens(self) -> int:
        """What the NEXT request would carry (count_tokens is free of charge and runs no model)."""
        probe = self.messages + [{"role": "user", "content": "(next question)"}]
        return self.client.messages.count_tokens(model=MODEL, system=SYSTEM_PROMPT, tools=TOOLS,
                                                 messages=probe).input_tokens


def result_tokens(client, desk: OrderDesk, order_id: str) -> tuple[int, str]:
    content, _ = desk.run("get_order_status", {"order_id": order_id})
    base = client.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": "x"}]).input_tokens
    with_result = client.messages.count_tokens(model=MODEL,
                                               messages=[{"role": "user", "content": "x\n" + content}]).input_tokens
    return with_result - base, content


def main() -> None:
    client = get_client()
    header(f"Lab 05 - tool result design: raw rows vs curated JSON ({MODEL})")
    if is_mock():
        print("(mock mode: token counts come from the mock's deterministic estimator - the ratios are what matter)")

    step(1, "One look-up, two result designs")
    raw_tokens, raw = result_tokens(client, RawRowsDesk(), "SO-10303")
    compact_tokens, compact = result_tokens(client, OrderDesk(), "SO-10303")
    print(f"raw rows:     {len(raw):>5} chars, ~{raw_tokens:,} tokens")
    print(f"curated JSON: {len(compact):>5} chars, ~{compact_tokens:,} tokens  "
          f"(x{raw_tokens / max(compact_tokens, 1):.1f} smaller)")
    print("curated JSON:\n  " + compact)
    leaked = sorted(set(json.loads(raw)["customers"][0]) - {"customer_id", "name"})
    print(f"fields the raw version also sends for every look-up (customers table): {', '.join(leaked)}")

    step(2, "The same five-question session, twice")
    sessions = {"raw rows": Session(client, RawRowsDesk()), "curated": Session(client, OrderDesk())}
    sizes: dict[str, list[int]] = {name: [] for name in sessions}
    answers: dict[str, list[str]] = {name: [] for name in sessions}
    for question in SESSION:
        for name, session in sessions.items():
            answers[name].append(session.ask(question))
            sizes[name].append(session.context_tokens())
    print("Context size after each question (tokens the next request must carry):")
    print(f"  {'#':>2} {'question':<46} {'raw rows':>9} {'curated':>9} {'ratio':>6}")
    for i, question in enumerate(SESSION):
        r, c = sizes["raw rows"][i], sizes["curated"][i]
        print(f"  {i + 1:>2} {question:<46} {r:>9,} {c:>9,} {r / c:>6.2f}")
    print(f"\nLast answer (raw rows): {answers['raw rows'][-1]}")
    print(f"Last answer (curated):  {answers['curated'][-1]}")
    print("Same facts from both designs: the extra tokens bought nothing (compare all five answers yourself).")

    step(3, "What the whole session cost")
    print(f"  {'variant':<10} {'API calls':>9} {'billed input':>13} {'output':>7} {'cost':>9}")
    for name, s in sessions.items():
        print(f"  {name:<10} {s.api_calls:>9} {s.billed_input:>13,} {s.output:>7,} ${s.cost:>8.4f}")
    raw_s, cur_s = sessions["raw rows"], sessions["curated"]
    extra = raw_s.cost - cur_s.cost
    print(f"Raw rows cost {raw_s.billed_input / cur_s.billed_input:.2f}x the input tokens "
          f"(+${extra:.4f} per session). At {MONTHLY_TICKETS:,} tickets a month that is ~${extra * MONTHLY_TICKETS:,.0f} "
          "a month for nothing - before caching, which cuts the price of re-sent tokens but not their number.")

    step(4, "Tool-definition overhead (count_tokens, no model call)")
    question = [{"role": "user", "content": "Where is SO-10303?"}]
    bare = client.messages.count_tokens(model=MODEL, messages=question).input_tokens
    three = client.messages.count_tokens(model=MODEL, messages=question, tools=TOOLS).input_tokens
    eleven = client.messages.count_tokens(model=MODEL, messages=question, tools=SUPPORT_TOOLS).input_tokens
    print(f"  no tools:                          {bare:>6,} tokens")
    print(f"  3 order-desk tools:                {three:>6,} tokens (+{three - bare:,})")
    print(f"  11 reference support-agent tools:  {eleven:>6,} tokens (+{eleven - bare:,})")
    print("Every request in the conversation pays this again (cache it - Day 3), and every extra tool is one more "
          "option the model must weigh. Adding tools is not free; neither is making them verbose.")


if __name__ == "__main__":
    main()
