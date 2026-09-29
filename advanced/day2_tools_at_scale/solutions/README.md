# Day 2 solutions - worked answers

Runnable solutions: `ex02_ex03_cost_math.py` (exercises 2-3), `ex04_near_duplicates.py`, `ex05_core_set.py`,
`ex06_keystone_scopes.py`, `ex08_tool_removal_phase.py`, `ex09_selection_scorer.py`, `ex10_discovery_fixes.py` and
`ex11_stream_validator.py`. The numbers below are printed by those scripts in mock mode. The arithmetic is exact;
token counts and the model's choices are the mock's, so rerun the scripts live before quoting them for Claude.

---

## 1. What survives a change to the toolset?

Three rules decide every row.

* **The cache is a prefix match** over tools, then system, then messages. Anything that changes bytes in the tools
  array, including its order, invalidates every entry. Anything appended after the cached prefix invalidates
  nothing. A deferred tool that nothing has referenced is not rendered.
* **Preserved thinking compares the prefix a block was produced in**: the system prompt, the tools as a *name-keyed
  set* of full definitions (order ignored, and deferred tools that nothing has named yet ignored), and every earlier
  message. Appending is fine; editing is not.
* **The tool-change blocks are a beta surface with placement rules**, and the API enforces them with 400s.

| Change | Accepted | Cache read | Earlier thinking | Why |
|---|:---:|:---:|:---:|---|
| a. search discovers, model calls a found tool | yes | yes | yes | Discovery appends. The search blocks and the expanded definitions sit in the tail, and the docs say the schemas are "appended to the request, not swapped in". Lab 02: +811 tokens in the tail, nothing invalidated. |
| b. append a regular tool to `tools` | yes | no | no | Tools render first, so a new loaded definition changes position 0. Every entry is unreadable (one full rewrite at 1.25x), and every thinking block was produced under another tool set (`tool_set_changed`). Depending on model and binding settings, the request is rejected or the blocks are dropped. |
| c. deferred + `tool_addition`, with the header | yes | yes | yes | The cache-preserving path. The deferred entry is outside the prefix and outside the compared set until named, and the addition is appended history. Lab 04, variant B. |
| d. (c) without the header | no (400) | - | - | `tool_addition/tool_removal blocks require the anthropic-beta header 'mid-conversation-tool-changes-2026-07-01'` (lab 04, step 6). The deferred entry alone would have been harmless; the block needs the header. |
| e. `tools` sorted by name | yes | no | yes | Reordering changes the bytes at position 0: one full miss, then stable again. The thinking check compares a set, so blocks still verify. Choose the order once and never let it depend on a registry, a dict or a worker (the case study). |
| f. typo fixed in `get_invoice`'s description | yes | no | no | Same name, new bytes: the cache is gone and every earlier block fails (`tool_schema_changed`). A "harmless" wording fix is the most expensive edit you can make to a live conversation. Freeze definitions per conversation and ship text changes to new conversations. |
| g. `tool_removal`, then a user message after it | no (400) | - | - | A removal must sit immediately before an assistant message, or last in `messages` (lab 04, step 6). Append it after the newest user message instead; then all three answers are yes. |
| h. inline `tool_definition` with the fix | yes | yes | yes | Under `inline-tools-2026-09-15` a `tool_addition` can carry a full definition, and a same-name definition replaces the old one from that message on. Nothing already sent moves. Keep the header on every later request, since the definition stays in the history. |

Tempting wrong answers:

* "(a) must break the cache because the model sees new tools." It does not: the definitions are appended.
* "(e) is harmless because nothing changed." Bytes changed. The cache compares bytes, and the thinking check
  compares sets, which is why the answers differ.
* "(c) works without the header because the tool is already declared." It does not: the block needs the header,
  not the declaration.

## 2. Load everything, or defer and search?

Units are input tokens at list price. At $5 per million, one unit is $0.000005.

**a.** Loaded, warm prefix: the extra 10,742 tokens are read on each of the 4 requests at 0.1x.
`4 x 10,742 x 0.1 = 4,296.8` units per conversation.

Deferred: a discovery in the response to request 1 is written with request 2 and read on requests 3 and 4:
`702 x (1.25 + 2 x 0.1) = 1,017.9` units. A second discovery (response to request 2) is written with request 3 and
read on request 4: `702 x (1.25 + 0.1) = 947.7`. Two discoveries therefore cost 1,965.6. At the stated mix:
`0.3 x 0 + 0.5 x 1,017.9 + 0.2 x 1,965.6 = 902.1` units per conversation.

Per day, with 400 conversations: loaded **$8.59**, deferred **$1.80**, a saving of **$6.79** (79%).

**b.** The average discovery costs `1,965.6 / 2 = 982.8` units, so break-even is `4,296.8 / 982.8 = 4.4`
discoveries per conversation. That is more discoveries than a conversation has requests. For this catalog, deferring
wins at any realistic discovery rate.

**c.** Cold start: the loaded strategy pays the write on the first request.
`10,742 x 1.25 + 3 x 10,742 x 0.1 = 16,650.1` units per conversation. The deferred strategy stays at 902.1, because
the core prefix is cold under both strategies and cancels out. The ratio grows from 4.8x to 18x. Thin traffic
punishes big prefixes.

**d.** The 20-tool catalog has `11 x 97 = 1,067` extra tokens: `4 x 1,067 x 0.1 = 426.8` units loaded against 902.1
deferred. Break-even is `426.8 / 982.8 = 0.43` discoveries per conversation. Loading wins unless fewer than about
four conversations in ten need a discovery. This is the "roughly 10K tokens of schemas" rule in numbers: below it,
the search step costs more than it saves.

**e.** The calculation leaves out five things:

* **Selection accuracy.** Lab 01, step 5: the fewer tools loaded, the fewer wrong first calls. A wrong call costs a
  turn, and sometimes a side effect.
* **The latency of the search step** inside the response.
* **Discovery precision.** Every returned definition is paid for on every later turn (lab 03: 7 of 30 returned
  definitions were called), so a `limit` that returns eight costs more than one that returns three.
* **Cache fragmentation** if you choose routes instead (lab 01, step 3).
* **The search's own output tokens**, which are small.

Lab 01's day simulation, which models the cache request by request, gives $51.62 for all loaded against $44.25 for
deferred. That difference of $7.37 is the same order as (a)'s $6.79.

## 3. A code cell, or direct calls?

**a.** Each later request re-reads **122** tokens per unit under direct calls against **25** under the code cell
(121.5 and 25.1).

**b.** With caching, every token the model reads is written once and read six times, a factor of
`1.25 + 6 x 0.1 = 1.85`:

* direct: `n x (121.5 x 1.85 + 27 x 5) = 359.9 n` units;
* code: `600 x 5 + n x 25.1 x 1.85 = 3,000 + 46.4 n` units;
* break-even: `3,000 / (359.9 - 46.4) = 9.6` units.

Without caching the factor is `1 + 6 = 7`: direct is `985.8 n`, code is `3,000 + 175.6 n`, and break-even is
`3,000 / (985.8 - 175.6) = 3.7` units.

**c.** At $5 per million:

| units | direct, cached | code, cached | direct, uncached | code, uncached |
|---:|---:|---:|---:|---:|
| 11 | $0.0198 | $0.0176 | $0.0542 | $0.0247 |
| 110 | $0.1979 | $0.0405 | $0.5422 | $0.1116 |

The fixed cost of writing the cell (600 output tokens, worth 3,000 input units) is what small jobs cannot amortise,
and the per-unit difference is what large ones save. Caching narrows the gap: a cached re-read costs 0.1x, so direct
calls are cheaper than an uncached estimate suggests.

**d.** The code path reads less because code-called `tool_use` and `tool_result` blocks go to the running cell, not
to the model, and cost no tokens: the response that only re-pauses the cell shows `out=0`, and the request after it
is no larger than the one before. It costs more because it writes more: 1,750 output tokens against 1,007, most of
them the cell, at five times the input price. That is the fixed cost in (b).

The lab also does not share this exercise's assumption of six later requests. Its conversation ends after the
triage, so the per-unit saving that later turns would collect never arrives. With no later requests the break-even
moves to 11.7 units (line d of the script's output), just above the lab's 11 units.

Live, measure three things. First, `input_tokens`, `cache_*` and `output_tokens` per request of the code path,
resumes included. Second, the output tokens of the cell Claude actually writes, which set the fixed cost. Third, the
prompt size of the first request after the triage, which decides the rest of the conversation's cost.

## 4. Near-duplicate tools

**a.** In each family, name the identifier the tool takes (the strongest signal a model has is the shape of the ID
in the request), use the users' verb, and add the negative that names the sibling.

* **Shipments.**
  * `get_shipment`: "One shipment's booking record by shipment ID (SH-...): the carrier we booked, the promised ETA,
    the recorded exception. Takes a shipment ID, not a tracking number; for where it is *now*, use track_shipment."
  * `track_shipment`: "Live carrier scans for a tracking number: where it is now, whether it has moved, out for
    delivery, delayed. If you only have an order ID, call list_shipments_for_order first."
  * `list_shipments_for_order`: "How many shipments an order has, with their IDs, carriers and tracking numbers;
    the first step when all you have is an order ID."
* **Money.**
  * `issue_refund`: "Money leaves Kestrel to the original payment method, for an order; irreversible; approval over
    $2,500."
  * `issue_credit_note`: "Reduces what the customer owes on an invoice; no money moves; needs approval; compute
    percentages from the invoice amount."
  * `apply_late_fee_waiver`: "Cancels only the late-payment fees of an overdue invoice; any other reduction is
    issue_credit_note."
* **Pickups.**
  * `schedule_pickup`: "A carrier pickup at a warehouse or site by location, for outbound goods; not for returns."
  * `schedule_return_pickup`: "Pickup of RMA units at the customer's site, by RMA ID, after create_rma; anything
    else is schedule_pickup."

**b.** The script's set C makes two edits. It removes "or as goodwill" from `apply_late_fee_waiver` (the phrase that
pulled the 5% goodwill request to it) and adds "any other reduction of what the customer owes is issue_credit_note".
It also adds "(compute percentages from the invoice amount)" to `issue_credit_note`. Result on the 30 tasks:

```
  set A: 27/30  confusions: [('get_order_status_history', 'list_shipments_for_order'), ('issue_refund', 'get_order'), ('escalate_to_human', None)]
  set B: 29/30  confusions: [('get_invoice', 'apply_late_fee_waiver')]
  set C: 30/30  confusions: none
  tasks set B got right that set C breaks: none
```

Two caveats. First, the stand-in is lexical: removing a word is what moves it, while a model is also helped by the
sentence that names the sibling. Second, B and C were written against these same 30 tasks. Score C on a held-out
set (exercise 9) and live before shipping it.

**c.** *Merge* two tools when they take the same identifier, have the same kind of effect and differ only by a
parameter the model can set from the request. For example, `get_shipment` and `track_shipment` could become one
`get_shipment(shipment_id | tracking_number, live_events: bool)`. The confusion disappears, at the price of a bigger
schema.

*Keep tools separate* when they differ in risk, approvals, owning system or latency. `issue_refund` (irreversible,
money out) and `issue_credit_note` (no money moves) must stay apart so that scopes and approvals can differ.

*Split* a tool when its description needs if-then rules, when its parameters are mutually exclusive modes, or when
its risk depends on a parameter (a read mode and a write mode). Scopes attach to tools, not to parameters.

**d.**

* *The executor* catches wrong IDs (a tracking number passed to `get_shipment` returns `not_found` with a hint), calls
  out of scope (`not_available`) and policy gates (`approval_required`).
* *A strict schema* catches the shape: types, required fields and enum members.
* *Only an eval* catches a well-formed call to the wrong tool, which succeeds: a credit note when the user wanted a
  refund, a fee waiver for a goodwill credit, the booking record when the user wanted live tracking. That is why
  selection is measured, not assumed.

## 5. Which tools are always loaded?

The rule is to load a tool when its expected discovery cost is higher than the cost of carrying its definition.
Carrying it costs `requests x tokens x 0.1` per conversation; deferring it costs `share x cost of one discovery`.
From `ex05_core_set.py`:

```
  create_rma: +145 tokens on every request -> 4.54 requests x 145 x 0.1 = 65.8 units per conversation loaded
    deferred: 24% of conversations x 980 units per discovery = 238.8 units; break-even share 6.7%
  track_shipment: +90 tokens on every request -> 4.54 requests x 90 x 0.1 = 40.8 units per conversation loaded
    deferred: 11% of conversations x 980 units per discovery = 112.2 units; break-even share 4.2%
```

On cost alone, anything used in more than about 5% of conversations should be loaded. The candidate sets, priced on
the same 480 traces:

```
  always loaded                          tools  prompt tokens  need a discovery  discoveries / conv  units / conv  input $ / day
  -------------------------------------  -----  -------------  ----------------  ------------------  ------------  -------------
  catalog core (9)                           9          1,327               36%                0.36           918          $1.84
  core + create_rma + track_shipment        11          1,562                0%                0.00           708          $1.42
  calls in >= 20% of traffic + escalate      7          1,146               36%                0.36           857          $1.71
  knowledge search + escalate only           2            633              100%                2.73         2,977          $5.95
```

**Recommended design.**

1. **Loaded whatever the traffic says:** `escalate_to_human`, because the way out to a person must never depend on a
   search (the same invariant as exercise 6), `search_knowledge_base`, the lookup behind most answers, and the tool
   search tool itself.
2. **Loaded by usage above the break-even share, on the copilot's own traffic:** from these traces, `get_customer`,
   `get_order`, `check_warranty`, `list_shipments_for_order`, `create_rma`, `check_return_eligibility`,
   `get_invoice` and `track_shipment`. That is the "core + create_rma + track_shipment" row, and in this proxy
   traffic nobody needs a discovery.
3. **Near-duplicates decide together.** If `track_shipment` is loaded while `get_shipment` stays deferred, the model
   will use the visible one for booking-record questions too. Either load both (about 97 tokens more) or make the
   loaded description name its deferred sibling ("for the booking record, search for get_shipment").
4. **`get_build_record` leaves the core for now.** It has 0% of support traffic, but the copilot also serves quality
   users whom these traces do not cover. Keep it deferred until the copilot's own traffic puts it above the
   break-even share. The cost of guessing wrong is one search.
5. **Write tools.** Loading `create_rma` changes how readily the model reaches for it, not what it is allowed to do:
   the executor still gates it. Make its description state the precondition ("after check_return_eligibility says
   eligible"). Deferral is not access control, because a deferred tool is one search away (exercise 6).

**What to monitor after launch:** the share of conversations with a discovery; the tools discovered most often
(candidates for loading); loaded tools that are rarely called (candidates for deferral); first-call accuracy on the
selection eval whenever the loaded set changes; and `cache_read_input_tokens`. Revisit the set monthly, since the
traffic mix moves as teams onboard.

**Rejected alternatives.**

* *Traffic top 20% plus escalate* (7 tools): a smaller prefix, but 36% of conversations search, and it drops
  `get_invoice` and `check_return_eligibility`, which are both above break-even.
* *Search and escalate only*: every conversation searches, at $5.95 a day against $1.42, and every request depends
  on search quality.
* *Everything loaded*: 12,064 tokens a request and the selection drop of lab 01.

The dollar differences between the first three rows are small. The decision rests on selection quality, which is
why an eval accompanies every change to the set.

## 6. Keystone's scoped toolsets

**Derivation** (`ex06_keystone_scopes.py`), from domains, maximum risk, the role's denied tools and the three tools
denied to every agent:

* `contractor_lead`: field service, products, fleet and knowledge; up to `write`; `get_customer` and `list_contacts`
  denied. That is 40 catalog tools.
* `contractor_engineer`: field service, products and knowledge; `read` only; `list_customer_sites` denied as well.
  That is 25 catalog tools.

The denied tools sit in the `customers` domain, which neither role has. They are excluded twice on purpose, so the
explicit denial survives a later domain grant.

**Harness grants.** `escalate_to_human` is in the `communications` domain, which neither role is granted, and it is a
`write`. Yet a person must always be reachable, so the harness adds it to every session as a documented, tested
exception (41 and 26 tools).

**Loaded vs deferred.** The script loads `check_warranty`, `get_service_ticket`, `search_knowledge_base` and
`escalate_to_human`, the four things a technician on a mobile app asks most (is it covered, what is on my ticket,
how do I fix it, get me a person), and defers the rest behind BM25 search. Both roles' first request is 837 tokens in
mock mode.

**One judgement the data does not make for you.** The derivation gives `contractor_lead` the tool
`set_alert_threshold` (fleet domain, `write`). It needs a `quality_lead`'s approval, and alert thresholds are
Kestrel's monitoring policy, not a contractor's decision. Deny it explicitly (a change request to `tenants.json`)
rather than rely on the approval step.

**Invariants tested before any session starts** (all hold in the script's output):

```
  [ok] no denied tool is declared (not even deferred)
  [ok] no tool above the role's max risk (grants excepted)
  [ok] every domain is granted to the role
  [ok] a person is always reachable (escalate_to_human declared)
  [ok] no approval-required tool for a read-only role
```

Add two executor tests: an out-of-scope call returns `not_available` (the script shows it for `list_contacts`), and
an out-of-tenant row is refused. The **row filter** (`customer_id in ['C-1016', 'C-1019']`) cannot live in the
toolset, because `check_warranty` is in scope for any serial number. It runs in the executor on every call, reads
and writes alike (Day 5).

**"Who is the site contact at C-1016? I need to call them."** Neither Keystone role declares `list_contacts`,
`get_customer` or `list_customer_sites`, so a search cannot return them. The model should say it cannot share
customer contact details, and offer to escalate or to note the request on the service ticket, where Kestrel's
dispatcher can act on it. Two things must never happen:

* **The data arrives through a side door.** Audit every declared tool's *output* for personal data. The catalog's
  `meta.pii` flag marks the tools that return it, and none are in these scopes.
* **A call succeeds because the model asked anyway.** The executor answers `not_available` (lab 07, step 5).

## 7. Retiring `get_shipment`

1. **Build v2 under a new name.** Never edit `get_shipment` in place. That would invalidate the cache and the thinking
   blocks of every live conversation (exercise 1f) and change behaviour halfway through them.
2. **Announce (new conversations).** Declare both tools. Make v1's description start with "DEPRECATED: use
   get_shipment_v2 (same arguments)" and give v2 the full description. Update the evals' expected labels, the
   prompts, the runbooks and the dashboards in the same change. Live conversations keep the tools array they started
   with and continue on v1 untouched. In lab 07, `the 3 shipment-record tasks went to {'get_shipment_v2': 3}`.
3. **Offer v2 to live conversations** where it matters, by appending: v2 declared deferred plus a `tool_addition`,
   and a `tool_removal` for v1. The cache and thinking survive, and the executor keeps accepting v1 until the sunset.
4. **Sunset.** The executor answers v1 with the retired envelope, and the model retries with the successor (lab 07,
   step 6):
   ```
     {"error": {"code": "retired", "message": "get_shipment has been retired; use get_shipment_v2 with the same arguments.", "next_action": "get_shipment_v2"}}
   ```
   Integrations that call v1 by name get the same error, plus a date and a changelog entry.
5. **Measure before removing.** Watch calls by name per day (the lab prints `{'get_shipment': 1, 'get_shipment_v2':
   1}` for its session) and the number of live conversations whose frozen tools array still contains v1. Rerun the
   selection eval with v2 in place.
6. **Remove** v1 from the tools array of *new* conversations once its call count has stayed at zero for longer than
   a conversation lives, including the longest durable-run resume (Day 1). Conversations that started before keep
   their frozen arrays until they end.

**What the cache and thinking see.** Step 2 changes the tools array for new conversations only: a new prefix, written
once. Steps 3 and 4 are appends (a system message, a tool result), so nothing is invalidated. The one thing that
would hurt, editing v1's text in place, is the step the plan never takes.

## 8. A close-out phase that removes tools

The solution runs lab 04's variant B with the fifth phase added:

```
  11 close-out    read  4,652 write   491  close_service_ticket; create_task
  12 close-out    read  5,143 write   193  (answer)
  13 close-out    read  5,336 write    83  (answer)
```
```
  property 1 - cache reads never fall after the first request: PASS
  property 3 - the Teams post was not made with a removed tool: PASS
```

Property 2 also passes: `ops.run("post_teams_message", ...)` returns `not_available`. The phase change at request 11
wrote only the new system message and turn (491 tokens) and read everything before it. The last answer says the
Teams post is not possible with the tools available now.

**Placement.** The system message with the removals and additions goes *after* the phase's first user message, so it
is last in `messages` when sent, immediately before the assistant turn it governs. Put it before the user message and
the API answers 400: "a tool_removal must sit immediately before an assistant message, or last in messages". The
additions in the same message are fine, because `close_service_ticket` and `create_task` were declared deferred from
the first request.

**Why the executor gate.** The `tool_removal` changes what the model sees; the allowed set changes what can run. A
stale plan, a retried turn or an injected instruction can still produce a call to a removed tool, and only the second
half stops it.

## 9. A selection-eval scorer

```
  set  correct  accuracy   95% CI
  ---  -------  --------  -------
  A    27/30         90%  74%-97%
  B    29/30         97%  83%-99%
  McNemar exact test on the 30-task set: B fixes 3, breaks 1; two-sided p = 0.625
```

**The formulas.** Wilson: `centre = (p + z²/2n) / (1 + z²/n)` and `half = z * sqrt(p(1-p)/n + z²/4n²) / (1 + z²/n)`
with z = 1.96. For 27/30 (p = 0.9) that gives 74%-97%. McNemar exact: under "no difference", each discordant pair is
a fair coin, so `p = 2 x P(X <= min(fixes, breaks))` with X ~ Binomial(fixes + breaks, 0.5), which is
`2 x (1 + 4) / 16 = 0.625`.

**Per tool.** A's misses are recall failures: `get_order_status_history` 0.50, `issue_refund` 0.67 and
`escalate_to_human` 0.00. B's one miss shows twice, as `get_invoice` recall 0.67 and `apply_late_fee_waiver` precision
0.67. The tool that soaks up wrong calls is the one whose description needs a negative sentence.

**Held-out.** The script's ten tasks were written from how staff phrase requests: "Put $310 back on GreenValley's
card", "Take the late-payment penalty off AR-90250".

```
  A    5/10          50%  24%-76%
  B    7/10          70%  40%-89%
  McNemar exact test on the held-out set: B fixes 3, breaks 1; two-sided p = 0.625
```

Both sets score lower on held-out tasks than on the thirty they were tuned against. That is the expected shape: the
tuning tasks flatter whoever read them. B still leads, but on ten tasks, 5/10 and 7/10 have intervals that overlap
almost completely.

**How many tasks?** The script enumerates the exact test's power:

```
  B fixes 7% of tasks and breaks 0% (90% -> 97%): about 115 tasks for 80% power (exact McNemar, two-sided 0.05)
  B fixes 10% of tasks and breaks 3% (90% -> 97%): about 225 tasks for 80% power (exact McNemar, two-sided 0.05)
```

So plan for hundreds of tasks, mined from real traffic (Day 6). Keep a held-out slice you never tune on, and retire it
into the tuning set once a decision has used it.

## 10. Fixing the discovery misses

**BM25.** "Get bulletin TSB-2026-09 about the seal lot recall." returned `list_units_by_lot`, `get_runbook`,
`get_lot_summary`, `link_incident_to_lot` and `open_quality_hold`. The words "seal", "lot" and "recall" appear across
the quality tools, and `get_bulletin`'s description did not use the words people use when they ask for a bulletin.
The fix adds "recall or safety notice", "the affected lots and SKUs" and the ID shape to that description:

```
  BM25 hit@1 before 31/32, after 32/32; tasks that lost first place: none
```

The last half of that line is the part to check: a description change is measured on the whole set, not only on the
task it targets.

**Regex.** With the stand-in's own queries, 10 of 32 tasks ranked the expected tool first. The 22 rewritten patterns
all put it first. They have three things in common: they quote the tool's own description (`\bevery serial number\b`,
`\bcontacts at a customer\b`, `\bbill of materials\b`), they use word boundaries, and they are multi-word phrases,
not alternations of stems. They are written in the tool author's vocabulary, not the user's. Regex search works when
the model can predict the catalog's wording (systematic names, namespaced prefixes, exact identifiers) and fails for
prose, where BM25's ranking of rare terms is the right tool.

## 11. A streaming validator

The order of checks, from `ex11_stream_validator.py`:

```python
    if sdk_raised:
        return "reissue", "the SDK raised while streaming: no tool_use id to answer, send the request again (capped)"
    if stop_reason == "refusal":
        return "stop", "a refusal can cut a tool_use mid-input: run nothing"
    if stop_reason == "max_tokens":
        return "retry_bigger", "truncated turn: drop it, never execute it, retry with a larger max_tokens"
```

Then come `json.loads` on the raw fragments (invalid JSON gets `invalid_json`), the JSON Schema (also
`invalid_json`), and the content check that every requested lot is named in the body (`invalid_json`). Otherwise the
call runs. All eight cases:

```
  cut after an action at max_tokens    -> retry_bigger  ok: truncated turn: drop it, never execute it, retry with a larger max_tokens
  body never names a requested lot     -> invalid_json  ok: content: the body never names PS-2608-B
```

**Which cases pass the schema and still must not run?** Two of them. "Cut after an action at max_tokens" is the first:
a tolerant parse of it is schema-valid (lab 06's replay table), and only the stop reason says it lost an action,
which is why the stop reason is checked before any parsing. "Body never names a requested lot" is the second: it is
strict JSON and schema-valid, and only a content check knows that a bulletin about PS-2608-B must name it.
`reissue` comes first because there is no `tool_use` block to answer at all.

## 12. Who do you trust in a code cell?

a. **False.** Code-called tools are client tools. Every call comes back to you as a `tool_use` (with a `caller`) and
   runs in your executor, so gate it exactly as a direct call: scope, approvals, row filters and argument
   validation. Validation matters more here, because the arguments were computed by code the model wrote.

b. **False.** The result returns to the running code, which can print it (stdout reaches the model) or branch on it
   (choose the next calls, their arguments, what to print). Untrusted text in a tool result can steer code the model
   wrote. Treat tool results as data from outside, which is Day 5's subject.

c. **True.** `allowed_callers` lists who may call the tool. `["direct"]` is the default, and a list with only the code
   execution version makes the tool reachable only from code. Pick one caller per tool unless you have a reason
   for both, because it tells the model how the tool is meant to be used.

d. **False.** Programmatic tool calling does not accept `strict: true` tools. Keep strict tools direct. A money-moving
   tool that needs an approval belongs in a direct call anyway, where the approval can pause the run (Day 1).

e. **False.** A container is state: variables, files, and anything an earlier cell loaded. Sharing one across users
   leaks data between them. Containers also expire (`expires_at`). One container per conversation, never shared
   across users or tenants.

f. **False.** One cell can issue any number of calls (lab 05's cell made 15 across two pauses). Count calls in the
   executor, not model turns, and enforce per-conversation limits there (Day 4's budgets).

g. **False.** The cell runs inside the API call. By the time you receive the response, the code has already run up to
   its first `await` of your tool, and the `server_tool_use` block is a record, not a proposal. What you control is
   what your tools do when called: refuse, gate, or answer with an error. Design the tools so that a cell you would
   not have approved cannot do harm through them.
