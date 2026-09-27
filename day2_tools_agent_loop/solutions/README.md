# Day 2 solutions — Tool Use & the Agent Loop

Worked answers for all twelve exercises. Each answer explains the reasoning, not just the result, and names the
tempting wrong answers and why they are wrong. The runnable solutions are:

| Exercise | Script | Run it |
|---|---|---|
| H9 | `ex09_shipment_tracking.py` | `python day2_tools_agent_loop/solutions/ex09_shipment_tracking.py` |
| H10 | `ex10_max_tokens.py` | `python day2_tools_agent_loop/solutions/ex10_max_tokens.py` |
| H11 | `ex11_tool_unit_tests.py` | `python day2_tools_agent_loop/solutions/ex11_tool_unit_tests.py` |
| H12 | `ex12_fix_bad_tool.py` | `python day2_tools_agent_loop/solutions/ex12_fix_bad_tool.py` |

All of them run offline in mock mode (the excerpts below are mock-mode output) and unchanged in live mode.

---

## C1. One message, results first

**1. What happens.** The first request fails with a 400 before the model runs:
`messages.N: tool_use ids were found without tool_result blocks immediately after: toolu_… Each tool_use block
must have a corresponding tool_result block in the next message.` The rule: **every `tool_use` in an assistant
turn must be answered in the very next user message.** Sending results one by one leaves the other calls
unanswered. Lab 03 reproduces this exact error.

**2. Why the protocol insists.**

* An assistant turn is **one decision with N outstanding actions**. The model asked for three observations because
  its next step depends on them. There is no representation for "result 2 is still pending": a missing result is
  indistinguishable from a call that silently vanished. Requiring the complete set keeps the transcript
  unambiguous.
* The conversation is a strict alternation: the next user turn is the environment's reply to the whole assistant
  turn. Tool results come **first** because they answer the calls directly above them. Text placed before them
  would read as the user interrupting before the tools returned, which breaks the pairing.
* Operationally, one results message per assistant turn is one state transition, which is easy to log, replay,
  unit-test and resume.

The teammate's goal ("let the model start earlier") also fails on its own terms. The model cannot start until you
send a request, and every extra request re-sends the whole history at full input price (§1.4 of the lesson). If the
tools are slow, run them **concurrently** and send one message (lab 03: 1.0 s → 0.5 s).

**3. The variants.**

| Variant | Accepted? | Why |
|---|---|---|
| (a) text *after* the results, same message | **yes** | results first, then anything; lab 03 shows `[accepted]` |
| (b) results split over two consecutive user messages | **yes, but don't** | the API merges consecutive same-role messages into one turn, so it is valid, but one message is clearer and cannot interleave with anything |
| (c) results in a different order | **yes** | results pair by `tool_use_id`; keep the original order anyway for readable logs |
| (d) results, then an assistant "Thanks!" before the next request | **no** | the conversation now ends with an assistant turn: on Claude Opus 5 that is prefill, rejected with `does not support assistant message prefill`. Never fabricate assistant turns; append only what the API returned |

Tempting wrong answers: *"it's an arbitrary API restriction"* (it is the transcript invariant the model is trained
on); *"results must be in the same order as the calls"* (pairing is by id); *"(b) is an error"* (it is merged, not
rejected; it is still a bad idea).

## C2. The quadratic bill

Request *t* carries *P + (t−1)·d* tokens with *P* = 2,900 and *d* = 800. The total over *T* calls is
*T·P + d·T(T−1)/2*.

**1. Without caching**

| Calls *T* | *T·P* | *d·T(T−1)/2* | Total input | Cost at $5/MTok |
|---:|---:|---:|---:|---:|
| 5 | 14,500 | 8,000 | 22,500 | $0.1125 |
| 10 | 29,000 | 36,000 | 65,000 | $0.3250 |
| 20 | 58,000 | 152,000 | 210,000 | $1.0500 |

Doubling from 10 to 20 calls multiplies the bill by **3.23**. From 10 calls on, the quadratic term is larger than
the fixed-prefix term.

**2. With automatic caching.** The first call writes its prompt at 1.25×: 3,625 token-equivalents. Call *t ≥ 2*
reads the previous prompt (*2,900 + (t−2)·800*) at 0.1× and writes 800 new tokens at 1.25×. That is
1,290 + 80·(t−2) token-equivalents.

| Calls | Token-equivalents | Cost | Saving vs no cache |
|---:|---:|---:|---:|
| 10 | 3,625 + (9×1,290 + 80×36) = **18,115** | $0.0906 | 72% |
| 20 | 3,625 + (19×1,290 + 80×171) = **41,815** | $0.2091 | 80% |

**3. "Caching fixes the quadratic growth" is wrong.** Caching **reprices** the re-sent tokens (0.1× instead of
1×); it does not stop re-sending them. The quadratic coefficient drops from 800/2 per turn² to 80/2, so the curve
is still superlinear: 10 → 20 calls costs ×2.31 with caching. The saving grows with length (58% at 5 calls, 80% at
20), which is why caching matters most for long loops. It does not change the growth law.

**4. Levers.** Fewer turns (the growth rate): parallel tool calls for independent look-ups; **task-shaped tools**
that return what the step needs in one call (the reference `get_order` returns order, shipment and invoice reference
together); programmatic tool calling for long chains whose intermediate results the model need not see. Smaller
per-turn increment *d*: curated results (lab 05: 4.4× smaller per look-up, 1.64× less input per session),
pagination and caps, and, for very long sessions, context editing or compaction. A cheaper model for sub-steps
changes the price, not the growth.

Tempting wrong answer: *"output tokens dominate"*. In agent loops input usually dominates **uncached** spend. Once
caching is on, output (thinking included) often becomes the largest line, so measure both.

## C3. A truncated tool call

**1. Never execute it.** `stop_reason: "max_tokens"` means generation stopped at your cap, possibly in the middle of
the tool input. A truncated input usually still parses. Here RMA-7004's refund due is $663.00, and `66` is exactly
what you get if generation stops after two digits. When streaming, the SDK's tolerant partial-JSON parser returns
exactly such a well-formed object, so "it parsed" proves nothing about completeness. The stop reason is the
signal. The right move is to **drop the turn** (do not append it), retry the
same request with a larger `max_tokens` on a bounded ladder, and hand over to a person if it still truncates.
(Answering the truncated call with an `is_error` "your call was cut off" result also keeps the transcript valid,
but it spends a turn and leaves a misleading call in the history. Retrying bigger is cleaner.)

**2. `refusal`.** Check it **before** touching content. A refusal can cut a `tool_use` mid-input, and in any case
the model declined, so run no tools. Use a server-side fallback (`fallbacks="default"` on Opus 5 via
`labkit.fallback_kwargs`) or hand over with a safe reply. The reference agent escalates and replies with a neutral
holding message.

**3. Text-only truncation.** Never send half an email. Either retry with a bigger budget (the default in
exercise 10) or return it flagged as truncated to a human reviewer. Lab 02's run 3 shows the flagged path. To
*continue* a long text instead of regenerating it, append the partial assistant turn and a user message asking to
continue. That is valid on Opus 5, whereas prefill (ending on an assistant turn) is not.

**4. The third retry.** 8,000 → 16,000 → **32,000**. A non-streaming `messages.create` with `max_tokens=32000` is
refused **client-side** by the SDK with `ValueError: Streaming is required for operations that may take longer than
10 minutes`. The SDK estimates 3,600 s × max_tokens / 128,000, and above ~21,333 tokens that exceeds 600 s. Cap
non-streaming calls at 16,000, or switch to `client.messages.stream(...).get_final_message()` above the threshold.
Setting an explicit client timeout disables the guard, but long idle connections can still drop, so stream
instead. Kestrel's reference agent (`kestrel/support_agent.py`) has exactly this doubling-to-32,000 path. It has
been reported for a fix, and exercise 10 shows the replacement.

## C4. Runner or manual loop?

1. **Order-desk assistant: Tool Runner.** Three read tools and no special stop-reason policy. The runner removes
   ~56 lines of loop code (lab 04: 67 vs 11), validates arguments with pydantic, and turns `ToolError` into
   `is_error`. Use `max_iterations` as the turn guard and log via the per-turn hook.
2. **Customer-facing support agent: manual loop.** It needs a policy on *every* turn: drop and retry a truncated
   turn, hand over after the ladder, escalate and reply safely on refusal, and stop when the dollar budget is
   spent. The runner stops on `max_tokens`/`refusal` without running tools, and you could check `stop_reason` in
   the loop body, but you would re-implement the loop's decisions inside its iterator. The reference agent uses a
   manual loop for these reasons.
3. **Hours-long approvals: neither loop may block.** Persist and resume. The refund tool files a pending approval
   and returns a reference; the run ends with a holding reply. When the decision arrives, append it to the stored
   transcript as a new user turn and run again (exercise D8). The runner has no "suspend for two days"; a manual
   loop makes the resume point explicit, which is why teams usually pick it here.
4. **`pause_turn` with server tools: either, if you verify.** The API pauses long server-side loops (default 10
   iterations) and expects the turn to be re-sent unchanged. The course's SDK (anthropic 1.8.0) maps `pause_turn`
   to "resume" inside the runner. Older versions returned the paused turn as the final message, silently
   truncating the answer. Check your installed version's behaviour, and cap continuations either way. In a manual
   loop it is a three-line branch.

**The colleague's gate is dangerous, for three reasons:**

1. **The refund still executes.** In anthropic 1.8.0 the runner computes the tool response for the turn even when
   you have appended your own messages. Lab 04 measures it: the model is told "Declined" while the side effect
   happens. That is the worst combination, because the transcript and the ledger disagree.
2. **It only answers the refund calls.** If the same turn also asked for `get_rma` (parallel calls), those
   `tool_use` blocks go unanswered and the next request fails with the missing-`tool_result` 400.
3. **"Declined." instructs nothing.** The model may retry, split the amount, or tell the customer the refund was
   issued.

**Fix:** put the gate **inside the tool function**. Check approval (or the approval limit) first, and on decline
raise `ToolError` with an instructive message ("DECLINED by … The refund was NOT issued. Do not retry or split it;
tell the customer it is pending approval with reference …"). The function body never runs, the runner answers
every call, and you audit the decision (lab 04 Gate B, lab 07).

## C5. Migrating forced tool choice

**1. What breaks.** On Claude Opus 5.5 (and Fable 5.1 / Mythos 5.1), `tool_choice` `{"type": "any"}` or
`{"type": "tool", ...}` returns `400 invalid_request_error: tool_choice: type "tool" and "any" are not supported for
this model.`. This applies to Messages, `count_tokens` (so pre-flight token estimates fail too) and Batches (each
item errors). Both services break on their first request.

**2. Replacements.**

* **Triage pipeline → structured outputs.** The forced tool existed only to get JSON. Use
  `client.messages.parse(output_format=TriageModel)` (or `output_config.format`). That is a *stronger* guarantee:
  the whole response is schema-valid JSON, with no loop and no tool bookkeeping (lab 08 step 7).
* **Order desk → `auto` + instruction + `strict` + verification.** Say "Use the get_order_status tool to answer" in
  the prompt (or append a `role: "system"` message naming the tool for that turn, a documented pattern when the
  application requires the call), keep `strict: true` so arguments stay schema-valid, check that a `tool_use` came
  back, and retry once with a firmer instruction if not (lab 08 step 6).

**3. Guarantees.** Lost: *that a call happens* (`auto` may answer directly). Kept: *schema-valid arguments*, via
`strict`. Compensate with the verify-and-retry check, an eval that measures the tool-call rate on real tickets
(Day 6), and a clear trigger in the tool description. `disable_parallel_tool_use` with `auto` still limits the
response to *at most* one call; the "exactly one" combination with forcing is gone.

**4. "Disable thinking so forcing works" is wrong twice.** Opus 5.5's thinking cannot be disabled at all:
`thinking: {"type": "disabled"}` is itself a 400 on that model. And the restriction is model-specific, not a
consequence of thinking: Opus 5 thinks by default and still accepts forcing. (Amazon Bedrock has a separate rule
requiring thinking disabled *alongside* forced tool choice on models that support forcing. That is a platform
detail and does not help here.)

---

## D6. Changing delivery addresses

**Tool definition**

```json
{
  "name": "update_delivery_address",
  "description": "WRITE (reversible until the order ships). Change the delivery address of ONE of the requester's orders that has not shipped yet. Use it only when the customer explicitly asks to change where an order is delivered and has given the complete new address. Do NOT use it for orders that have shipped (offer a carrier redirect with the tracking number instead) or to change the billing address. Changes to another country or within 2 business days of the promised date are routed to the order desk; the result says whether the change was applied or is pending.",
  "strict": true,
  "input_schema": {
    "type": "object",
    "properties": {
      "order_id": {"type": "string", "description": "Order ID, e.g. SO-10312"},
      "address": {
        "type": "object",
        "properties": {
          "street": {"type": "string", "description": "Street and number, e.g. 4410 Industrial Pkwy"},
          "unit": {"type": "string", "description": "Unit/suite, or an empty string"},
          "city": {"type": "string"},
          "postal_code": {"type": "string"},
          "country": {"type": "string", "description": "ISO 3166-1 alpha-2 code, e.g. US"}
        },
        "required": ["street", "unit", "city", "postal_code", "country"],
        "additionalProperties": false
      },
      "customer_request_quote": {"type": "string", "description": "The customer's sentence requesting the change, quoted verbatim (kept in the audit record)"}
    },
    "required": ["order_id", "address", "customer_request_quote"],
    "additionalProperties": false
  }
}
```

A structured address, not one free-text line: the tool can validate each part and name exactly what is missing.

**Risk class and approval.** A **reversible write** until the order ships. Redirecting goods is also a classic
fraud pattern, so the approval rule is risk-based and enforced in code:

| Condition (checked by the tool) | Decision |
|---|---|
| order belongs to the sender (identity from the channel), status `pending`/`confirmed`/`in_production`, same country, promised date more than 2 business days away, order value under $25,000 | **auto-apply**, audit, confirmation to the account's **email on file** (not only the sender) |
| different country, promised date within 2 business days, or value ≥ $25,000 | **pending**: routed to the order desk for approval (customs/tax, warehouse may already have picked it) |
| `shipped` / `delivered` / `cancelled` | **refuse** with instructions |

**Errors (for the model).**

* shipped: *"SO-… shipped on … with … (tracking …); the address can no longer be changed. Offer to request a carrier
  redirect; do not promise it."*
* cancelled: *"SO-… is cancelled; nothing to change."*
* not yours: one uniform message for "does not exist" and "not on this account", so the tool cannot be used to
  enumerate order numbers.
* invalid address: *"postal_code '4410' is not a valid US ZIP code; ask the customer for the full address."*

**Idempotency and audit.** The same order plus the same normalised address returns the existing `change_id` rather
than a new one. Use one open change per order with an optimistic version check, so two conflicting changes cannot
silently overwrite each other. The audit record holds ticket, sender, old → new address, decision (auto-policy or
the named approver), the quoted request, and a timestamp.

**Result and reply.** The tool returns
`{"change_id": "ADR-8001", "status": "applied" | "pending_approval", "new_address": {...}, "note": ...}`. Applied:
confirm, restate the new address and the reference, and mention the confirmation email. Pending: "passed to our
order desk, who will confirm by [SLA]". Never claim a pending change is done. Lab 07 implements a simplified version
of this tool behind an approval gate.

**The argument you must not add:** anything that names the requester or grants authority (`customer_id`,
`requester_email`, `authorized_by`, `skip_approval`). Identity comes from the mail gateway and authority from
policy code. Any argument the model can fill, a malicious email can fill too.

## D7. "Just give it run_sql"

| Dimension | 11 task tools (reference) | `run_sql(query)` on a read-write connection |
|---|---|---|
| Prompt injection | worst case: the model calls a tool it was allowed to call, with checked arguments | worst case: arbitrary SQL with the connection's privileges (`UPDATE refunds …`, `SELECT * FROM customers`) |
| Identity | from the channel, enforced per tool | unenforceable: the model writes the `WHERE` clause |
| Policy (refund limits, eligibility) | enforced in code (`kestrel.policy`) | an `INSERT INTO refunds` bypasses it; policy lives only in the prompt |
| Gating & audit | per action, typed arguments, easy approval UI | an opaque string; you would have to parse SQL to know what it does |
| Context cost | curated results | `SELECT *` and joins; row caps must be bolted on |
| Reliability | one call per task step | schema knowledge in the prompt (DDL tokens), join mistakes, more turns |
| Maintenance | a tool per capability, unit-tested | "never add a tool" means business rules drift into prompts and schema changes silently break them |

For a customer-facing agent that can move money, the proposal fails on security alone. Least privilege is
impossible when the privilege is "any SQL".

**When SQL *is* right at Kestrel:** an **internal analyst assistant** ("late shipments by carrier last quarter").
Give it a read-only replica, a read-only transaction, an allowlist of curated views (`v_shipments`, `v_orders`
without PII columns), row-level security, a statement timeout and a row limit enforced by the wrapper, query logging,
and staff users only. Even then, results should come back aggregated or capped. For heavy analysis consider the code
execution tool over an exported file, which keeps the raw rows out of the context. This follows Anthropic's rule of
thumb: start broad where the blast radius is contained, and promote actions to dedicated tools when they need
gating, audit or rendering.

## D8. Refund approvals that take hours

**1. At request time.** The agent calls `get_rma`, then `issue_refund`. The tool computes the approver level from
the **refund due** (not the requested amount; see H11), refuses, and files the approval request itself (or the agent
calls `escalate_to_human`, as in the reference). It returns
`{"status": "pending_approval", "approval_id": "APR-…", "amount_usd": 9188.50, "approver": "Support Manager",
"sla": "1 business day"}`. The agent tells the customer the amount, that it needs approval from that role, the
reference, and the expected timeline, and makes no promise that it will be approved. The run ends. Nothing blocks.

**2. State and resume.** Three records hold the state:

* the **approval record** (id, RMA, exact payload, payload hash, approver role, status, requested/decided at, by,
  reason);
* the **conversation transcript**, keyed by ticket;
* the **chat message**, which carries only the `approval_id`. Button clicks go to your service, which verifies the
  chat platform's request signature, maps the clicking user to an approver role, and records the decision.

On **approve**, *the service* executes the refund deterministically from the stored payload. The model is not asked
to re-decide. The service then appends a user turn to the transcript such as *"[approval APR-… approved by Finance
Director at …; refund RF-… issued for $9,188.50]"* and runs the agent (or a template) to write the customer update.
On **decline**, it appends the reason and the agent explains the outcome and the next step (for example a partial
credit offer made by a person).

**3. Exactly the approved refund, exactly once.** Bind the approval to a hash of (RMA, amount, currency, payee =
original payment method), and have execution refuse if the payload differs. Use an idempotency key on the payment
call (`approval_id`). Enforce the RMA state machine `received → refund_pending → refunded` with a database
constraint. Make the button handler idempotent, because double clicks and retries happen.

**4. The customer writes while it is pending.** `get_rma` shows `refund_pending` with the approval reference, and
the agent reports the status. Creating an approval is idempotent (the existing pending request comes back), so a
second email cannot create a second approval. If the SLA is breached, the service escalates the approval to a
deputy. That is a scheduler's job, not the model's.

**5. Anti-splitting guards.** The approval level follows the **refund due** for the RMA. Partial refunds always
need a human. There is one refund per RMA (the state machine). Add a per-customer daily refund cap, and alert on
multiple refunds for the same order. All of these live in code, because RET-002 prohibits splitting, and a prompt
cannot guarantee it.

---

## H9. `get_shipment_tracking`

**Solution:** `ex09_shipment_tracking.py`. Design decisions:

* **A new, narrow tool** instead of more fields on `get_order_status`. Tracking questions are the largest ticket
  category, and a tracking-shaped result answers them in one call. The description tells the model to *prefer* it
  for "where / when / late / stuck" questions, and names the ID format.
* **Lateness computed in code.** `business_days_late` between the promised date and the delivered date (or the
  ETA). The model should not count business days, and policy SHP-003 §4 turns on exactly this number (more than 5
  business days late).
* **"Not shipped yet" is an answer, not an error.** The first draft raised `ToolError` for unshipped orders. The
  model then saw an *error* for a perfectly valid question. Return
  `{"status": "confirmed", "shipment": null, "note": "Not shipped yet …"}` instead. Reserve `is_error` for calls
  the model must correct.
* Tracking links use `.example` domains. The result reuses `get_order_status`'s validation and instructive errors
  by calling it. `tool_names` is extended in the subclass, never by dispatching to arbitrary attributes.

Mock-mode output:

```text
--- Step 2: Is SO-10290 late, and by how many business days? ---
  tool calls: get_shipment_tracking(SO-10290)
  SO-10290 shipped on 2026-09-03 with BlueRiver Logistics (tracking BRL4803670335), promised for 2026-09-09, but
  the carrier reports an exception: Highway 16 closed due to wildfire; ... Expected arrival 2026-09-17 is 6
  business day(s) after the promised date.
--- Step 3: When will SO-10312 arrive? ---
  tool calls: get_shipment_tracking(SO-10312)
  SO-10312 is confirmed and has not shipped yet; the promised date is 2026-09-18.
```

Note that 6 business days late would qualify for compensation *if* the delay were within Kestrel's control. A
wildfire road closure is not (SHP-003 §4), so a good agent searches the policy before offering anything.

## H10. Robust max_tokens handling

**Solution:** `ex10_max_tokens.py`. The core is `create_with_ladder()`:

```python
for budget in ladder:
    streamed = budget > NON_STREAMING_MAX          # ~21,333: the SDK's 10-minute estimate
    if streamed:
        with client.messages.stream(max_tokens=budget, **params) as stream:
            response = stream.get_final_message()
    else:
        response = client.messages.create(max_tokens=budget, **params)
    record(attempt)
    if response.stop_reason == "refusal" or response.stop_reason not in TRUNCATED:
        return response                             # usable (or a refusal, which the loop handles)
raise Truncated(attempts)                           # the loop hands over
```

Choices and why:

* **Drop, don't append, truncated turns.** They may hold a truncated tool input (C3). A text-only truncation is
  retried too, because a half-written customer email must never be sent.
* **Streaming above the threshold** instead of a custom timeout. The loop cannot tell the difference, because
  `get_final_message()` returns the same `Message`.
* **A bounded ladder, not open-ended doubling.** Doubling eventually crosses the streaming threshold (the reference
  agent's bug) and has no natural end.
* **Sticky budgets.** The next turn starts at the budget that last succeeded, so a conversation does not pay for
  the same truncated attempts on every turn.
* **Every attempt recorded.** Truncation is a cost and quality signal that belongs in traces (Day 6).

Mock-mode output of the starved ladder `(100, 180, 24000)`:

```text
  attempt: max_tokens=   100 streamed=False stop_reason=max_tokens  output_tokens=100
  attempt: max_tokens=   180 streamed=False stop_reason=max_tokens  output_tokens=180
  attempt: max_tokens=24,000 streamed=True stop_reason=tool_use    output_tokens=190
  attempt: max_tokens=24,000 streamed=True stop_reason=end_turn    output_tokens=225
```

Step 4 of the script shows the SDK refusing a non-streaming `max_tokens=24000` client-side. Anthropic's cost
guidance makes the complementary point: `max_tokens` is a backstop, not a tuning knob. For long agentic coding
runs it recommends generous caps (64K, streamed), because capped runs that fail cost money without producing
results.

## H11. Unit tests for tools

**Solution:** `ex11_tool_unit_tests.py`, which runs 13 tests in milliseconds with no model and no key. The three
layers:

1. **Schema contract** for every tool in both toolsets: strict, closed objects, `required ⊆ properties`, typed
   properties, and a description of at least 80 characters. The "does the description say *when*?" check is a
   heuristic, so it **warns** instead of failing. It flags `issue_refund`, whose description states a precondition
   but no trigger.
2. **Behaviour and error contract:** the happy path; a **context budget** (one order under 1,200 characters of
   JSON); **data minimisation** (no phone numbers or credit limits in results); instructive errors for the wrong
   kind of ID, malformed IDs and unknown IDs ("do not guess"); unknown tools and missing arguments come back as
   JSON `is_error`, never as exceptions; policy search cites its source; the write is idempotent.
3. **Policy guarantees of the reference toolset:** refund above the agent limit refused with the next step named;
   a refund happens once; `create_rma` is idempotent; identity comes from the channel (a stranger gets nothing,
   and order ID + PO verifies).

**What the "get around the limit" test finds.** It asks for a **$2,400 refund on RMA-7001**, whose refund due is
$9,188.50 (Support Manager territory). `issue_refund` picks the approver from the *requested* amount, so $2,400 goes
through as agent-approved, and the RMA is marked `refunded`, closing it with $6,788.50 unpaid. This is a policy
bypass (RET-002 §6) that no prompt instruction can close. The test records it as an explicit **skip with a "KNOWN
GAP" message**. That way the suite stays green today and turns into a normal passing test once the tool is fixed.
The fix in `kestrel/support_tools.py` would be:

```python
due = rma["refund_due_usd"]
approver = policy.refund_approver(due)            # the approval level follows what is DUE ...
if approver != "agent":
    raise ToolError("... must be approved by the ... call escalate_to_human ...")
if abs(amount - due) > 0.005:                     # ... and partial refunds are a human decision
    raise ToolError("Partial refunds need a person: call escalate_to_human(queue='support_manager') ...")
```

The lesson generalises: **test that your guarantees hold against a caller who is trying to get around them**,
because the caller is a language model reading untrusted email.

## H12. Critique and fix a badly designed tool

**Problems in the definition** (the linter in `ex12_fix_bad_tool.py` reports 15 findings that cover all eleven, some of them once per argument):

1. `db` is not a verb_noun name and says nothing about what the tool does.
2. The description is 36 characters, says nothing about what it returns, and gives no trigger.
3. `q` is an abbreviation with no description: order ID, invoice ID or email?
4. `cust` lets the **model** choose whose data to read. That is identity from arguments, and an injection vector.
5. `mode` is a closed set written as free text ("order, invoice, … or refund"). It should be an enum, or better,
   separate tools.
6. `amt` is an abbreviation with no unit or description.
7. `all_columns` is a flag whose only purpose is raw dumps.
8. There is no `required` list, so every argument is optional, including the ones the code needs.
9. It is not `strict`, so arguments are not guaranteed to match the schema.
10. The schema is open (no `additionalProperties: false`).
11. Reads and an **irreversible write** (refund) share one tool, so the write cannot be gated, audited or
    rate-limited separately.

**Problems in the implementation:**

12. `SELECT *` joined with `customers`: every look-up ships contact names, e-mails, phone numbers and credit limits
    (a PRV-004 violation and a context cost).
13. It returns a Python `repr`, not JSON (single quotes, `None`), which is harder for the model to read reliably.
14. `"ERROR"` teaches the model nothing: which ID failed, why, what next?
15. `orders_for_customer` is unbounded: 4,237 characters for one customer today, and it grows forever. There is no
    `limit`, filter or pagination.
16. `refund` has no RMA check, no limit, no approval, no idempotency and no audit. The success string ("Refund of
    9188.5 issued") will be relayed to the customer.
17. No format validation (`SO-12345`) and no ownership check.
18. An unknown `mode` falls through to `"ERROR"` instead of an instructive message.

**The fix** (implemented in `CustomerDesk`):

* `get_order_status(order_id)` and `get_invoice(invoice_id)`: strict, documented, curated JSON, instructive errors.
  **Ownership is checked before anything is read**, with one message for "does not exist" and "not yours", so the
  tools cannot be used to enumerate order numbers. (The reference toolset still distinguishes the two cases. That
  was reported as a small hardening item.)
* `list_my_orders(status?, limit?)`: no customer argument (identity from the channel), a `status` enum, a limit
  capped at 10, newest first, and a note on how to get details.
* **Refunds are not in this toolset.** They belong to the RMA workflow's `issue_refund`, with the limit enforced in
  code and an approval path (D8). A read-mostly assistant should not carry a money-moving capability "because the
  old tool had it".

Mock-mode output:

```text
`db`: 15 problems found by the linter
`get_order_status`: 0 problems
`get_invoice`: 0 problems
`list_my_orders`: 0 problems
definitions: bad = 443 tokens (1 vague tool); fixed = 797 tokens (3 documented tools) - clarity costs a few
hundred tokens once per request, and caching makes them cheap
jorge.medina@greenvalley-coop.example: 'Where is SO-10300?' -> tools: get_order_status(is_error)
  I can't share any details about SO-10300 from this email address. ...
```

The trade-off is worth stating plainly: the fixed definitions cost **more** tokens per request (797 vs 443 in mock
mode). They are paid once per request, cache well, and buy correct tool choice, smaller results, and a surface you
can gate and test. The raw `db` result alone costs more than the extra definition tokens on the first look-up.
