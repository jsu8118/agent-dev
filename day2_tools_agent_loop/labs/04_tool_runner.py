"""Lab 04 - The SDK Tool Runner: the same agent in a fraction of the code.

Objective
    Rebuild the lab 02 agent with `@beta_tool` functions and `client.beta.messages.tool_runner(...)`,
    compare the code size, use the runner's hooks (inspect each turn, see tool results before they are
    sent, bound the loop), and put an approval gate in front of a WRITE tool - learning which gate
    placement actually prevents the side effect.

Concepts
    Tool Runner (beta); schemas generated from type hints + docstrings; ToolError -> is_error;
    max_iterations; until_done(); generate_tool_call_response(); append_messages(); gating inside the
    tool function vs overriding the history.

Run
    python day2_tools_agent_loop/labs/04_tool_runner.py

What to observe
    * The runner produces the same trajectory as your manual loop; you write only the tools.
    * `@beta_tool` turns the function signature and docstring into the JSON schema the model sees.
    * Gate A (override the pending call with append_messages) changes what the MODEL sees, but in the SDK
      version this course pins the function still RAN - the step prints what your installed SDK does.
      Gate B (the approval check inside the tool) is the one that reliably prevents the side effect.
"""

# test: expect=Code size
# test: expect=Gate B

from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import dataclass

from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from _order_desk import SYSTEM_PROMPT, OrderDesk
from labkit import MODEL, get_client, header, step, text_of, wrap

lab02 = importlib.import_module("02_agent_loop_from_scratch")

DESK = OrderDesk()


def _call(name: str, **kwargs) -> str:
    """Route a runner tool call to the order desk; expected failures become is_error results."""
    content, is_error = DESK.run(name, kwargs)
    if is_error:
        raise ToolError(content)            # the runner sends this back with is_error: true
    return content


# The docstring becomes the tool description and "Args:" the parameter descriptions. Keep the FIRST LINE a
# complete one-line summary: the docstring parser treats it as the "short description" and inserts a paragraph
# break after it, so a sentence wrapped across the first two lines arrives split in half.
@beta_tool(strict=True)
def get_order_status(order_id: str) -> str:
    """Look up ONE Kestrel sales order: status, dates, lines, shipment and invoice ID.

    Use it whenever a question is about where an order is or when it arrives. The shipment part has the
    carrier, tracking number, ETA, delivered date and any carrier exception. For several orders, call it
    once per order, in parallel.

    Args:
        order_id: Sales order ID, e.g. SO-10234.
    """
    return _call("get_order_status", order_id=order_id)


@beta_tool(strict=True)
def get_invoice(invoice_id: str) -> str:
    """Look up ONE customer invoice: amount, due date, amount paid, balance and status.

    Use it for billing and payment questions; an order's invoice ID is in the get_order_status result.

    Args:
        invoice_id: Invoice ID, e.g. AR-90123.
    """
    return _call("get_invoice", invoice_id=invoice_id)


@beta_tool(strict=True)
def search_policies(query: str) -> str:
    """Search Kestrel's written policies: shipping and delays, returns, refunds, warranty, privacy, safety.

    Use it BEFORE telling anyone what they are entitled to, and quote what it returns.

    Args:
        query: Keywords, e.g. 'late delivery compensation customs'.
    """
    return _call("search_policies", query=query)


# ------------------------------------------------------------------------------ a write tool + approval
@dataclass
class Decision:
    approved: bool
    reviewer: str
    reason: str


def deny_all(tool_name: str, args: dict) -> Decision:
    """Stand-in for a human reviewer (lab 07 builds a real approval flow)."""
    return Decision(False, "ops-lead (simulated)", "logistics cases for customs holds go through the EU desk")


APPROVER = deny_all


@beta_tool(strict=True)
def open_logistics_case(order_id: str, summary: str) -> str:
    """WRITE ACTION. Open a case for Kestrel's logistics team to chase a carrier about a stuck shipment.

    Use only when asked to follow up with the carrier, after looking the order up. Idempotent: returns the
    existing open case for the order instead of opening a second one.

    Args:
        order_id: Sales order ID, e.g. SO-10234.
        summary: What is wrong and what logistics should do (1-2 sentences).
    """
    if GATE_INSIDE_TOOL:
        decision = APPROVER("open_logistics_case", {"order_id": order_id, "summary": summary})
        if not decision.approved:
            raise ToolError(json.dumps({"error": f"DECLINED by {decision.reviewer}: {decision.reason}. The case "
                                                 "was NOT opened. Do not retry; tell the user."}))
    return _call("open_logistics_case", order_id=order_id, summary=summary)


GATE_INSIDE_TOOL = False
READ_TOOLS = [get_order_status, get_invoice, search_policies]


def run_with_runner(client, question: str, *, tools=READ_TOOLS, max_iterations: int = 8, verbose: bool = True):
    """The whole lab 02 loop, as far as your code is concerned."""
    runner = client.beta.messages.tool_runner(
        model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT, tools=tools, max_iterations=max_iterations,
        messages=[{"role": "user", "content": question}])
    for turn, message in enumerate(runner, start=1):
        if verbose:
            calls = [f"{b.name}({json.dumps(b.input)})" for b in message.content if b.type == "tool_use"]
            print(f"  turn {turn}: stop_reason={message.stop_reason} in={message.usage.input_tokens} "
                  f"out={message.usage.output_tokens} {'; '.join(calls)}")
    return runner.until_done()


def code_size(func) -> int:
    lines = inspect.getsource(func).splitlines()
    return sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))


def main() -> None:
    global GATE_INSIDE_TOOL
    client = get_client()
    header(f"Lab 04 - the Tool Runner ({MODEL})")

    step(1, "What @beta_tool generated from the function signature and docstring")
    print(json.dumps(get_order_status.to_dict(), indent=2))
    print("Note the auto-generated 'title' keys: harmless, but they cost tokens in every request (lab 05).")

    step(2, "The lab 02 question, run by the Tool Runner")
    question = ("Siobhan Byrne at Aurora Pharma says order SO-10279 was promised for 8 September, tracking has shown "
                "'exception' for days and nobody from Kestrel has called them. What happened, is she entitled to "
                "compensation under our shipping policy, and has the invoice for this order been paid?")
    final = run_with_runner(client, question)
    print("Final answer:\n" + wrap(text_of(final)))

    step(3, "Code size: manual loop vs runner")
    manual, runner_lines = code_size(lab02.run_agent) + code_size(lab02.next_action), code_size(run_with_runner)
    print(f"Code size - manual loop (run_agent + next_action): {manual} lines; with the runner: {runner_lines} lines.")
    print("What the manual loop still gives you: your own max_tokens retry policy, refusal handling, per-turn "
          "budgets in dollars, custom transports. The runner stops (without running tools) on max_tokens and "
          "refusal - check final.stop_reason yourself.")

    step(4, "Hook: look at tool results before they are sent back")
    DESK.calls.clear()
    runner = client.beta.messages.tool_runner(
        model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT, tools=READ_TOOLS, max_iterations=5,
        messages=[{"role": "user", "content": "Where are SO-10300 and SO-10303?"}])
    for message in runner:
        response = runner.generate_tool_call_response()      # runs the tools now; the result is cached
        if response is not None:
            for block in response["content"]:
                print(f"  about to send: tool_result is_error={block.get('is_error', False)} "
                      f"{str(block['content'])[:90]}...")
    print(f"  tools executed: {len(DESK.calls)} (each exactly once - the runner reuses the cached response)")

    step(5, "Approval gates for a WRITE tool - two placements")
    write_question = ("SO-10279 is stuck in customs. Please open a logistics case so someone chases the carrier, "
                      "and tell me what you did.")
    tools = [*READ_TOOLS, open_logistics_case]

    print("Gate A - inspect the pending call in the loop body and override it with append_messages():")
    DESK.cases.clear()
    GATE_INSIDE_TOOL = False
    runner = client.beta.messages.tool_runner(model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT, tools=tools,
                                              max_iterations=6,
                                              messages=[{"role": "user", "content": write_question}])
    for message in runner:
        pending = [b for b in message.content if b.type == "tool_use"]
        if any(b.name == "open_logistics_case" for b in pending):
            decision = APPROVER("open_logistics_case", {})
            denial = {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                 "content": f"DECLINED by {decision.reviewer}: {decision.reason}. Not performed."}
                for b in pending]}
            runner.append_messages(message, denial)          # the model will see the denial...
    print("  model's answer:\n" + wrap(text_of(runner.until_done())))
    ran = bool(DESK.cases)
    print(f"  ...but did the function run? {'YES - a case was opened: ' + str(list(DESK.cases)) if ran else 'no'}")
    if ran:
        print("  In this SDK version the runner still computes the tool response for the turn, so the side effect "
              "happened while the model was told it was declined. Don't use history overrides as a safety gate.")

    print("\nGate B - the approval check lives INSIDE the tool function:")
    DESK.cases.clear()
    GATE_INSIDE_TOOL = True
    final = run_with_runner(client, write_question, tools=tools)
    print("  model's answer:\n" + wrap(text_of(final)))
    print(f"  did the function's side effect happen? {'yes' if DESK.cases else 'no - nothing was opened'}")

    step(6, "max_iterations bounds the runner like your max-turn guard")
    short = run_with_runner(client, question, max_iterations=1, verbose=False)
    print(f"  with max_iterations=1 the runner stopped after one model call: stop_reason={short.stop_reason!r} "
          f"(unanswered tool calls: {sum(1 for b in short.content if b.type == 'tool_use')}) - your code must "
          "notice that the run did not finish.")


if __name__ == "__main__":
    main()
