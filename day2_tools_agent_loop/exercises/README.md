# Day 2 exercises — Tool Use & the Agent Loop

Twelve exercises: five concept checks (C), three design scenarios (D) and four hands-on coding tasks (H).
Timings are for a working engineer who has done labs 01–08. Every exercise has a worked answer in
`../solutions/README.md`, and the coding ones also have runnable solutions in `../solutions/*.py`. Try each one
before you look.

Hands-on tasks run offline in mock mode. In live mode each costs a few cents. Scratch databases you create must be
named with a `day2_` prefix (`labkit.data.scratch_db("day2_<something>.db")`).

---

## C1. One message, results first (10 min)

An assistant turn contains three `tool_use` blocks. A teammate's loop sends each `tool_result` to the API in its
own request as soon as that tool finishes ("so the model can start thinking earlier").

1. What happens on the first of those requests? Quote the rule, not just "it fails".
2. Why does the protocol insist that all results for one assistant turn arrive together, in the next user message,
   before any text?
3. Which of these variants does the API accept? (a) text *after* the three results in the same user message;
   (b) the three results split over two consecutive user messages; (c) the results in a different order from the
   `tool_use` blocks; (d) a user message with the results and then an assistant message reading "Thanks!" before
   the next request.

## C2. The quadratic bill (15 min)

A Kestrel agent's fixed prefix (tool definitions + system prompt + customer email) is 2,900 tokens. Each turn
appends about 800 tokens (the assistant's tool call plus the tool result). Input costs $5 per million tokens on
Claude Opus 5.

1. Compute the total input tokens and input cost for runs of 5, 10 and 20 model calls, without caching.
2. Redo 10 and 20 calls with automatic prompt caching. The first call writes its prompt at 1.25×; each later call
   reads the previous prompt at 0.1× and writes the new 800 tokens at 1.25×.
3. A colleague says "caching fixes the quadratic growth". Right or wrong, and why?
4. Name two levers from today that reduce the *growth rate* itself, and one that reduces the *per-turn increment*.

## C3. A truncated tool call (10 min)

A response arrives with `stop_reason: "max_tokens"`. Its last block is
`tool_use {"name": "issue_refund", "input": {"rma_id": "RMA-7004", "amount_usd": 66}}`. The SDK parsed it without
complaint.

1. Should the loop execute it? What should it do instead, and why is "the SDK parsed it" no reassurance?
2. What changes if the stop reason is `refusal` and the content holds a `tool_use` block?
3. What if a `max_tokens` response holds only text — the customer reply cut mid-sentence?
4. Your loop doubles `max_tokens` on each retry, starting at 8,000. What happens on the third retry with the
   non-streaming `messages.create`, and how do you avoid it?

## C4. Runner or manual loop? (15 min)

For each case choose the Tool Runner or a manual loop, and justify the choice in one or two sentences:

1. The internal order-desk assistant from labs 02–04 (three read tools).
2. The customer-facing support agent. It must hand over when a turn is truncated, meter dollars per ticket against
   the $0.40 target, and escalate on a refusal.
3. An agent whose refund tool needs a Finance Director approval that can take hours.
4. A research helper that uses the `web_search` server tool and sometimes returns `pause_turn`.

Then review this approval gate a colleague wrote with the runner. What is wrong with it, and how do you fix it?

```python
for message in runner:
    pending = [b for b in message.content if b.type == "tool_use" and b.name == "issue_refund"]
    if pending and not approver.approve(pending):
        runner.append_messages(message, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content": "Declined."}
            for b in pending]})
```

## C5. Migrating forced tool choice (10 min)

Two Kestrel services run on Claude Opus 5. The **triage pipeline** calls
`tool_choice={"type": "tool", "name": "record_triage"}` only to obtain structured fields. The **order desk** forces
`get_order_status` on the first turn of every conversation. The platform team wants to move both to Claude Opus 5.5.

1. What breaks, and where exactly? Consider Messages, `count_tokens` and Batches.
2. What do you replace each use with?
3. Which guarantee is lost, which is kept, and how do you compensate for the lost one?
4. A colleague suggests sending `thinking: {"type": "disabled"}` "so forcing works again". Evaluate.

---

## D6. Kestrel wants the agent to change delivery addresses (25 min)

Customers regularly ask to change the delivery address of an order (ticket T-1005). SHP-003 allows changes
**until the order ships**. Today the reference agent escalates these to the order desk. Design a tool that lets the
agent do it:

* name, description (what + when + when not), and a strict JSON schema;
* risk class and approval rule (who approves what, automatically or by a person);
* preconditions and the errors that explain them to the model, including shipped, cancelled, not yours, and
  missing address parts;
* idempotency and the audit record;
* what the tool returns, and what the agent tells the customer on success and on decline;
* the one argument you must *not* add, and why.

## D7. "Just give it run_sql" (20 min)

The data team proposes replacing the eleven reference support tools with one tool, `run_sql(query: str)`, on a
read-write connection to Atlas ERP. "Claude writes great SQL, and we'd never have to add a tool again." Evaluate the
proposal against: security (prompt injection, identity, least privilege), policy enforcement, gating and audit,
context cost, reliability, and maintenance. Is there a situation at Kestrel where a SQL tool *is* the right design?
What would it look like?

## D8. Refund approvals that take hours (20 min)

Refunds above $2,500 need the Support Manager, and above $10,000 the Finance Director. Approvals take between 10
minutes and 2 business days. The Finance Director wants to approve from a chat message with Approve/Decline buttons.
Design the flow end to end:

1. What the agent does and says when a refund needs approval.
2. What state is stored where, and how the conversation resumes after the decision (approved and declined).
3. How to make sure the refund that executes is exactly the one that was approved, and executes once.
4. What happens if the customer writes again while the approval is pending.
5. Which guards stop the agent from splitting the refund to get under the limit.

---

## H9. Add a `get_shipment_tracking` tool (30 min)

Tracking questions are the largest ticket category. Add a read tool, `get_shipment_tracking(order_id)`, to the lab
02 agent:

* It returns the carrier, tracking number and a tracking link (use `.example` domains), the ship date, the ETA or
  delivered date, any carrier exception, and **how many business days the (expected) arrival is after the promised
  date**, computed in code.
* Write a description that makes the model prefer it over `get_order_status` for "where is it / is it late"
  questions.
* Decide what the tool returns for an order that has not shipped yet.
* Run your agent on: "Fiona at Coastal Shipyards says tracking for SO-10300 hasn't updated since the 10th — is it
  stuck in customs?", "Is SO-10290 late, and by how many business days?" and "When will SO-10312 arrive?".

Tip: `run_agent()` in lab 02 takes `tools=` and `system=`; subclass `OrderDesk` and extend its `tool_names`.

## H10. Robust max_tokens handling (30 min)

Replace the doubling retry in a manual loop with a **budget ladder**, for example `(8000, 16000, 32000)`:

* never execute or append a truncated turn;
* when the budget exceeds what the SDK accepts without streaming, send the request with
  `client.messages.stream(...)` and use `get_final_message()`;
* when the whole ladder is exhausted, hand over instead of acting on a fragment;
* record every attempt (budget, streamed or not, stop reason, output tokens).

Demonstrate three cases: a normal run, a starved ladder whose last rung is streamed, and an exhausted ladder. Bonus:
make the ladder "sticky" so later turns start at the budget that last worked.

## H11. Unit tests for tools (30 min)

Write fast, model-free tests (`unittest` or `pytest`) for the order desk (`labs/_order_desk.py`) and for the policy
guarantees of `kestrel/support_tools.py`. Cover at least:

* the schema contract of every tool (strict, closed, required ⊆ properties, a description that says when to use it);
* the happy path, **context budget** (result size) and data minimisation;
* instructive errors: wrong kind of ID, malformed ID, unknown ID, unknown tool, missing arguments;
* idempotency of a write;
* the refund limit, "refund happens once", and identity from the channel;
* one test that tries to *get around* the refund limit. What does it find?

## H12. Critique and fix a badly designed tool (30 min)

`exercises/bad_tool_definition.py` contains a tool called `db` that someone wrote for the customer-facing bot. Run
it and see what it returns.

1. List every problem with the **definition** and with the **implementation**. There are more than ten.
2. Design the replacement toolset: names, descriptions, strict schemas, result shapes and errors.
3. Decide where the `refund` capability belongs, and who may approve it.
4. Implement enough of your design to answer "Where is SO-10303?" from `jorge.medina@greenvalley-coop.example`, and
   show that "Where is SO-10300?" from the same sender (another customer's order) discloses nothing.
5. Bonus: write a linter for tool definitions that you could run in CI.
