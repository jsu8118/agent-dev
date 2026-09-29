# Day 2 - Tool engineering at scale

The first course gave Kestrel's support agent eleven tools, and the tool list was a detail: a few definitions at the
top of every request, each easy to tell apart. Since go-live, the Service Desk Copilot has become the front door for
more teams. Field service wants engineer availability and visit booking, quality wants lots, holds and bulletins,
fleet telemetry wants pump readings, fault codes and alerts. The catalog in `advanced/data/tools/catalog.json` now
holds **120 tools in 12 domains**, with near-duplicates that exist because three teams built what they needed. The
tool list has stopped being a detail. It is the first thing in every request, it is what the model chooses from, and
it is what the prompt cache keys on.

Today is about carrying a large, changing toolset without paying for all of it on every request, and without the
model losing its way in it. You will measure what a toolset costs. You will defer definitions behind tool search
and compare its regex and BM25 variants. You will change the toolset in the middle of a conversation without
breaking the cache or preserved thinking, and move fan-out work into code with programmatic tool calling. You will
stream long tool inputs and check them yourself, write tool contracts that hold up as the catalog grows, scope
toolsets by role and tenant, and measure tool selection with evals that can tell a real improvement from noise.

The day builds on three days of the first course. Its Day 2 (tools and the agent loop) taught descriptions, schemas
and errors for a handful of tools. Its Day 3 (context engineering) taught the cache's prefix economics, which decide
most of today's trade-offs. Its Day 6 (evals) taught the discipline that today applies to tool selection. Day 5 of
this course takes the security side further: row filters, injection through tool results, and how far to trust
code a model wrote.

## Learning objectives

By the end of the day you can:

1. Price a toolset: measure it with `count_tokens`, explain where definitions render and what caching does to their
   cost, and choose among loading everything, hand-picked routes, deferred loading with tool search and code
   execution, or a combination of them.
2. Configure tool search (regex and BM25) with `defer_loading`, read its blocks (`server_tool_use`,
   `tool_search_tool_result`, the error variant), and measure discovery with hit@k, MRR and discovery precision.
3. Change the toolset mid-conversation with `tool_addition` / `tool_removal` and inline definitions, and say what
   each change does to the cache, to preserved thinking and to the executor.
4. Move fan-out work into code with programmatic tool calling: the pause/resume protocol, the container, and the
   cases where round trips are still the better choice.
5. Stream long tool inputs with `eager_input_streaming` and take over the validation the API no longer does.
6. Write tool contracts that survive a growing catalog: descriptions that separate near-duplicates, strict schemas
   and enums, one error envelope, and versioned names for deprecation.
7. Derive capability-scoped toolsets from a tenant and role model, and put each check in the toolset or in the
   executor, wherever it belongs.
8. Build a tool-selection eval with intervals, paired comparisons and a held-out set, and read it without fooling
   yourself.

## Agenda (about 7 hours)

| Time | Block | Lab |
|---|---|---|
| 0:00 - 0:45 | What a large toolset costs; four ways to carry one | 01 |
| 0:45 - 1:40 | Tool search internals: regex vs BM25 | 02 |
| 1:40 - 2:20 | The wide agent: 120 tools, nine loaded | 03 |
| 2:20 - 2:35 | Break | |
| 2:35 - 3:25 | Mid-conversation tool changes | 04 |
| 3:25 - 4:10 | Lunch | |
| 4:10 - 5:05 | Programmatic tool calling | 05 |
| 5:05 - 5:45 | Eager input streaming | 06 |
| 5:45 - 6:45 | Tool contracts, scoped toolsets, selection evals | 07 |
| 6:45 - 7:00 | Case study review | |

Exercises (`exercises/README.md`, twelve of them, worked solutions in `solutions/README.md`) are for the evening or
the next morning. Four are hands-on: a tool-removal phase, a selection-eval scorer, fixing discovery misses, and a
streaming validator.

**Running the labs.** `python advanced/day2_tools_at_scale/labs/01_catalog_cost.py` from the repository root
(activate the venv first). Without an API key everything runs against labkit's offline mock. The mechanics are
real: tool search runs over the definitions as documented, cache accounting follows the API's rules, and code
cells run in a local sandbox and pause like the API. The *choices* are a rule-based stand-in: which tool, which
search query and which code. Every lab says so in `[mock]` lines wherever that matters. With `ANTHROPIC_API_KEY`
set, the same scripts call Claude. The simulated total of the seven labs is about $4.12, most of it the two
selection evals in labs 01 and 07 (121 and 130 calls). Lab 04 uses two beta headers,
`mid-conversation-tool-changes-2026-07-01` and `inline-tools-2026-09-15`, through `client.beta.messages.create`.
Tool search, code execution, programmatic tool calling and eager input streaming are generally available and need
no header. The excerpts below are *mock mode*. Live wording, token counts and ids will differ; the mechanics will
not.

---

## 1. What a large toolset costs, and four ways to carry one

**The idea.** Every tool you declare is rendered into the prompt at the very front (the order is tools, then
system, then messages), on every request, and the model chooses from everything it can see. A catalog that grows
from 11 to 120 tools grows three costs at once:

* **tokens** on every request;
* **cache fragility**: the prefix is larger and must stay byte-identical to be read;
* **selection errors**: more candidates, and more of them look alike.

The three costs have different remedies, which is why "just use tool search" is only part of the answer.

**Where the pattern comes from.** This is the *working set* problem from operating systems: keep resident what is
used often, page in the rest on demand, and pay a fault when you guess wrong. It is also the *interface
segregation* problem from API design: a client offered everything uses the wrong thing. Both disciplines settled
on the same answer: measure what is used, keep that small and stable, and make everything else cheap to fetch.

**How it works underneath.** `client.messages.count_tokens` with and without tools shows the size of each request
shape. Lab 01 measures the catalog (*mock mode*):

```
  request shape                     prompt tokens  tool block
  --------------------------------  -------------  ----------
  no tools                                     25           0
  first course: 11 support tools            2,138       2,113
  core only (9 tools)                       1,328       1,303
  route: core + logistics (19)              2,342       2,317
  core + BM25 search, 111 deferred          1,347       1,322
  core + code execution (9 + 1)             1,343       1,318
  all 120 tools loaded                     12,089      12,064
```

The tool block is a tool-use system prompt plus the JSON of every loaded definition. Deferred definitions travel
in the request but cost nothing until a search surfaces them. From then on they are billed in the conversation tail,
where the API expands them (702 tokens for one search turn that returned five references in lab 01). Caching
changes the arithmetic without changing the principle. A warm 12,064-token prefix is read at 0.1x, so it costs
about what 1,200 uncached tokens would, but only while every byte of it stays the same. Selection is the cost that
caching cannot touch. With every tool loaded, every tool is a candidate for every request.

**The four strategies compared** (lab 01, step 6, with a simulated day of 1,600 requests):

```
  strategy                tool tokens  day (sim.)  cache                  visible tools      use when
  ----------------------  -----------  ----------  ---------------------  -----------------  -------------------------------------------
  all tools loaded             12,064  $51.62      one entry, large       120                small catalogs; every tool used often
  hand-picked per route         2,317  $48.96      one entry per route    ~19                few stable task types and a reliable router
  deferred + tool search        1,322  $44.25      one entry, small       9 + found          large or growing catalogs, mixed tasks
  code execution (PTC)          1,318  lab 05      one entry + container  9 + code-callable  fan-out, loops, big intermediate results
```

| Strategy | What it saves | What it costs | Fails when |
|---|---|---|---|
| All loaded | nothing to build, no search step | every definition on every request; selection among all of them | the catalog passes a few dozen tools, or a byte of it changes per request |
| Hand-picked routes | tokens; a narrower choice | a router, a cache entry per route (lab 01: 88% prefix hits instead of 100%) | the router is wrong: the right tool is simply absent |
| Deferred + search | tokens; the prefix stays small and stable | a search step when nothing loaded fits; discovered definitions in the tail | descriptions do not use the users' words (section 6) |
| Code execution | the *results* of many calls stay out of the context | a container, a code-writing turn, a sandbox to trust | each result needs the model's judgement before the next call |

The strategies compose, and none of them is a default. Anthropic's cost-optimization guidance for the Claude API
puts the break-even of tool search at roughly 10K tokens of schemas. Below that, the search step is overhead
(exercise 2 works through the numbers). Code execution does not shrink the definitions it calls; it shrinks what the
calls return into the context.

**Kestrel example.** The copilot's final design keeps nine core tools loaded (`meta.core` in the catalog) and
defers the other 111 behind BM25 search. It switches phase-specific tools with `tool_addition` in the service-visit
flow and runs the recall fan-out as a code cell. The case study below tells how it got there.

## 2. Tool search: deferred definitions, regex and BM25

**The idea.** Declare every tool, mark most of them `defer_loading: true`, and add a tool search tool. When nothing
loaded fits, the model searches. The API returns *references* to matching tools and expands their definitions into
the conversation. You pay for a definition only once it has been found.

**Where the pattern comes from.** Lazy loading and demand paging. The retrieval part is classic information
retrieval: the regex variant is `grep`, and BM25 is the Okapi ranking function behind a generation of search
engines, which weighs a term up when it is rare across the catalog and saturates repeated terms. In both cases the
retrieval surface is the tool's name, description and argument text, so **the description is now a search
document** as well as an instruction.

**The shapes (GA, no beta).** The labs declare the search tool from a constant in `_day2.py` and defer everything
outside the core:

```python
SEARCH_BM25 = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
...
    if defer:
        out["defer_loading"] = True
```

The regex variant is `{"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}`. The model's
search is a `server_tool_use` whose input is `{"pattern": ..., "limit"?}` for the regex variant and
`{"query": ..., "limit"?}` for BM25. The API runs it and attaches the result *in the same response*. Lab 02, step 1:

```
  server_tool_use  id=srvtoolu_mock_33f1396ecc90b987c022  name=tool_search_tool_regex  input={"pattern": "scan|track|histor"}
...
  server_tool_use  id=srvtoolu_mock_9086fb2a0f5ee3b5a817  name=tool_search_tool_bm25  input={"query": "order carrier scan history tracking number"}
{
  "type": "tool_search_tool_result",
  "tool_use_id": "srvtoolu_mock_9086fb2a0f5ee3b5a817",
  "content": {
    "tool_references": [
      {
        "tool_name": "track_shipment",
        "type": "tool_reference"
      },
```

The rules that trip people up:

* **Never send a `tool_result` for a `srvtoolu_` id.** The API ran the search; you only keep sending the same `tools`
  array.
* **At least one tool stays non-deferred** (the search tool itself may not be deferred), and a deferred definition
  may not carry `cache_control`. Put breakpoints on loaded blocks.
* **A bad search is a 200, not a 400.** An invalid regex comes back as `tool_search_tool_result_error` with
  `error_code: "invalid_tool_input"`, and the model can correct itself in the same response. The SDK's error codes
  are `invalid_tool_input`, `unavailable`, `too_many_requests` and `execution_time_exceeded`.
* **`count_tokens` may reject server tools.** `d2.count_prompt()` then falls back to a `max_tokens=1` request and
  reads the billed input. That is a paid call, so do it once, not per request.
* **Discovery is appended, not swapped.** The expanded definitions sit in the messages after the cached tools and
  system prefix, so a search invalidates nothing. Lab 02 measures it: `prompt before the search: 1,349 tokens; with
  the search turn in the history: 2,160 (+811)`. Every later turn carries those definitions, so a search that returns
  eight tools costs more than one that returns three.

**Regex vs BM25.**

| | regex (`tool_search_tool_regex_20251119`) | BM25 (`tool_search_tool_bm25_20251119`) |
|---|---|---|
| What the model writes | a Python regular expression | natural-language terms |
| Matching | any match in name, description or argument text, unranked | ranked by term rarity and frequency |
| Good at | catalogs with systematic names (prefixes, namespaces), exact identifiers | prose requests, descriptions written in users' words |
| Typical failure | matches inside other words; broad alternations; invalid patterns | common domain words bury the one word that names the tool |
| Lab 02, 32 tasks (*mock*) | hit@1 31% | hit@1 97% |

Lab 02 shows both failure modes in the stand-in's own queries. For the regex variant, `'fault' matched in 'default'`
put `list_orders` first for a fault-code question. For BM25, the words "seal", "lot" and "recall" pulled five other
quality tools ahead of `get_bulletin`. Exercise 10 fixes both. It takes one description rewrite for BM25 and 22
patterns quoted from the catalog's own wording for regex, which says what regex search rewards: knowing how the
tool author wrote the description.

**What to measure.** With one relevant tool per task, hit@k is recall@k, and precision@5 is at most 0.20. Track
**MRR** (how high the right tool ranks) and **discovery precision**, the share of returned definitions the model
went on to call. In lab 03, 7 of the 30 definitions the searches returned were called (23%). The other 23 were paid
for on every later turn of their conversations.

**Kestrel example.** The wide agent of lab 03 declares 121 tools and loads 10 (nine core tools and the search tool),
and its first request costs 1,482 prompt tokens against 12,224 with everything loaded. On "where is the shipment for
SO-10290", it searches, finds `track_shipment`, looks up the order's shipments with a core tool, and tracks the
parcel. The same task on a core-only agent ends `partial`. Of the other four tasks, the core-only agent escalates
all four.

## 3. Changing the toolset in the middle of a conversation

**The idea.** Sometimes your application, not the model, decides which tools exist. A service-visit conversation
moves from diagnosis to scheduling to customer communication, a capability is revoked, or a tool ships while a
conversation is running. Re-sending a different `tools` array does it at the price of the whole cache, and on models
with preserved thinking, of every earlier thinking block. The tool-change blocks do it by *appending* to the
conversation.

**Where the pattern comes from.** The append-only log of Day 1: never rewrite what you sent, append a record of the
change. Databases call it a schema migration applied as an event, not an edit of history.

**The shapes (beta `mid-conversation-tool-changes-2026-07-01`).** A `role: "system"` message whose content blocks add
or remove tools by reference. Lab 04 prints the one it sends at the first phase change:

```
  {"role": "system", "content": [
      {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "get_fault_codes"}},
      {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "decode_fault_code"}},
      {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "get_pump_telemetry"}},
      {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "create_service_ticket"}},
      {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "get_engineer_availability"}},
      {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "schedule_field_visit"}}
  ]}
```

The rules, each of which lab 04 breaks on purpose to show the 400:

* A tool you add must already be declared in `tools` with `defer_loading: true`. Adding a loaded tool is a 400.
* A `tool_removal` must sit immediately before an assistant message, or last in `messages`. The lab appends the
  system message after the user turn it applies to.
* The beta header must be on the request. The blocks are plain dicts in Python; SDK typings lag them.
* **Inline definitions** (`{"type": "tool_definition", "definition": {...}}` inside a `tool_addition`) need
  `inline-tools-2026-09-15`. That header also covers changes by reference, so it replaces the older one. It is a
  Claude API feature, while the by-reference header also works on Amazon Bedrock and Google Cloud. The mid-
  conversation system messages page lists the supported models, and Claude Sonnet 5 is not among them.

When a beta graduates, expect the header to stop being required while the block shapes stay. Build these messages
in one helper so the change is one line, and check the release notes then.

**What each way of changing tools does.**

| | Re-send `tools` | `tool_addition` / `tool_removal` | inline `tool_definition` | Tool search |
|---|---|---|---|---|
| Who decides | you | you | you | the model |
| Header | none | `mid-conversation-tool-changes-2026-07-01` | `inline-tools-2026-09-15` | none (GA) |
| Declared up front | no | yes, deferred | no | yes, deferred |
| Cached prefix | invalidated from position 0 | kept | kept | kept |
| Earlier thinking blocks | invalidated | kept | kept | kept |
| Change a same-name definition | yes, at full price | no: use a new name | yes: the new definition replaces the old | no |
| Withdraw a tool | implicitly | `tool_removal` | `tool_removal` | cannot |

The preserved-thinking column follows from how the check compares the prefix. Tools are compared as a name-keyed
set of full definitions, so reordering the array is harmless to thinking (it is not harmless to the cache), and a
deferred tool that nothing has named yet is outside the comparison. Adding a loaded tool, removing one, or editing a
description in place changes the set, and every block minted before the change stops verifying. Day 3 covers the
binding controls.

**Cost.** Lab 04 runs the same ten requests both ways:

```
  variant                          requests  prompt  cache read  cache write  uncached    out     cost
  -------------------------------  --------  ------  ----------  -----------  --------  -----  -------
  A: re-send tools per phase             10  28,410      18,155       10,255         0  2,539  $0.1366
  B: tool_addition / tool_removal        10  35,886      31,234        4,652         0  2,539  $0.1082
```

B's prompts are *larger*: every phase tool is declared, and the change messages accumulate. Yet B costs 79% of A,
because each of A's phase changes rewrites the whole prefix at 1.25x, while B's cache reads only grow. The gap widens
with longer conversations and larger system prompts.

**Removal is two changes.** The `tool_removal` block changes what the model sees. The executor's allowed set
changes what can run. Lab 04 gates the executor per phase, and exercise 8 proves that a removed tool is refused even
when the model calls it anyway, whether from a stale plan or an injected instruction.

**Tool changes or tool search?** Search is for *discovery*: the model finds what it needs. Tool changes are for
*control*: the application says what exists now. Kestrel's service-visit flow uses changes, because the phases are
the application's state and not something the model should find its own way around.

## 4. Programmatic tool calling: fan-out in code

**The idea.** With direct tool use, each call is a round trip: Claude calls, the result enters its context, Claude
reads it and calls again. Programmatic tool calling (PTC) lets Claude write one Python cell that calls your tools as
async functions. The container pauses when the code awaits a client tool, you run the call and resume, and the result
goes back *to the running code*. Only what the cell prints enters Claude's context.

**Where the pattern comes from.** Moving the computation to the data, as stored procedures and map-reduce do.
Instead of shipping every row to the client (the model) and letting it filter, ship the filter.

**The shapes (GA).** A `code_execution_20260120` server tool, and client tools that allow code as a caller
(`allowed_callers: ["code_execution_20260120"]`; `["direct"]` is the default). Lab 05 builds them like this:

```python
    return [dict(d2.CODE_EXECUTION)] + [d2.api_tool(d2.entry(n), allowed_callers=[d2.CODE_CALLER]) for n in TOOL_NAMES]
```

The protocol, as lab 05's transcript shows it:

```
  <- response 1: stop_reason=tool_use  in=1,108 out=1,013
       server_tool_use code_execution id=srvtoolu_mock_cffe21384ebb51dd0540  (27 lines of Python)
...
       tool_use get_pump_telemetry(serial_number=KP100-2608-0001) [from code]  caller={type: code_execution_20260120, tool_id: srvtoolu_mock_cffe21384ebb51dd0540}
...
  -> request 2: + assistant turn + user message with 11 tool_result blocks (and nothing else), container=container_mock_000040
  <- response 2: stop_reason=tool_use  in=1,915 out=0
```

* The paused response carries `tool_use` blocks with a `caller` naming the cell. `asyncio.gather` in the cell turns
  eleven calls into one pause.
* Code-called `tool_use` and `tool_result` blocks go to the running cell, not to the model, so they cost no tokens.
  A response that only re-pauses the cell, like response 2 above, is not model work: `out=0`.
* You answer with a user message that contains **only** `tool_result` blocks, and pass
  `container=response.container.id`. Without the container it is a 400, and with extra text in the message it is
  a 400 too (lab 05, step 7).
* When the cell finishes, a `code_execution_tool_result` (`stdout`, `stderr`, `return_code`) arrives and Claude
  continues.
* The container is state. Variables survive between cells, so lab 05's second question reuses `units` from the
  first cell (`same container: True`), until `container.expires_at`.

**Three ways to run the same triage** (lab 05, step 5):

```
  approach                   requests  model turns  tool calls  tool output in context  billed input    out     cost
  -------------------------  --------  -----------  ----------  ----------------------  ------------  -----  -------
  direct, one call per turn        16           16          15                   1,337        30,355  3,191  $0.1116
  direct, parallel calls            3            3          15                   1,337         6,363  1,007  $0.0384
  code cell (programmatic)          3            2          15                     276         4,938  1,750  $0.0684
```

Read the columns with care. The code path *reads* less: 4,938 billed input tokens against 6,363 for parallel direct
calls. It *writes* more: 1,750 output tokens against 1,007, most of them the cell itself, at five times the input
price. For one 11-unit question that ends the conversation, parallel direct calls are cheaper ($0.0384 against
$0.0684). What the code path changes for the rest of the conversation is **tool output in context**: 276 tokens
instead of 1,337, carried by every later turn. Code pays off as the fan-out grows, as the results grow, and as the
conversation continues after them. Exercise 3 puts the break-even near ten units with caching, near four without,
and near twelve when nothing follows the triage.

**When code wins, and when round trips do.**

| Use a code cell when | Use direct calls when |
|---|---|
| calls are many, independent or mechanically chained | each result needs judgement before the next step |
| results matter in aggregate (joins, filters, sums, top-k) | a call has side effects that need a human's approval |
| intermediate results are large and mostly discarded | the tool must stay `strict: true` (PTC does not accept strict tools) |
| the same job repeats with different inputs | one or two calls do the job |

**A preview of Day 5.** A cell is code the model wrote. It runs in a sandbox and calls *your* tools with arguments it
computed. Gate every call in the executor exactly as you gate direct calls: approvals, scopes and row filters. Tool
results flow into code the model wrote, so untrusted text in them can steer that code. And a container is state:
never share one across users or tenants.

## 5. Eager input streaming: long tool inputs, validated by you

**The idea.** By default, when you stream, the API buffers and validates each tool-input parameter before it emits
any `input_json_delta` for it. For `{"order_id": "SO-10303"}` that is invisible. For a 2,000-character bulletin body
it is a silent gap, and for a 20K-token file it is minutes. Setting `eager_input_streaming: true` on a client tool
turns the buffering off for that tool. The fragments stream as they are generated and the first one arrives at once.
The events are the same (`content_block_start`, then `input_json_delta` many times, then `content_block_stop`); only
the timing and the validation guarantee change. It is a field on the tool, with no beta header. It is ignored on
non-streaming requests and not allowed on server tools.

**Where the pattern comes from.** This is the trade every streaming parser makes: latency against the guarantee that
what you hold is complete and well-formed. Eager mode moves the guarantee from the server to your client.

**What you give up, and what you must do instead.** The accumulated input can be cut at `max_tokens` mid-parameter,
or be invalid JSON the model emitted. The SDKs accumulate it with a *tolerant* partial-JSON parser, so a broken input
often comes back as a silently truncated object and not as an exception. Lab 06 replays one streamed draft cut in
four ways:

```
  what arrived                        chars  tolerant parse (what the SDK hands you)  schema                                     strict json.loads
  ----------------------------------  -----  ---------------------------------------  -----------------------------------------  --------------------------------------
  the full stream                     2,044  6 keys, 5 actions                        schema-valid                               ok
  cut inside `body`                     555  4 keys, 0 actions                        invalid: 'body' is a required property     error: Unterminated string starting at
  cut after the last complete action  1,983  6 keys, 4 actions                        schema-valid                               error: Expecting ',' delimiter
  cut inside `actions`, mid-string    1,995  6 keys, 4 actions                        schema-valid                               error: Unterminated string starting at
  a missing comma between two fields  2,043  5 keys, 0 actions                        invalid: 'actions' is a required property  error: Expecting ',' delimiter
```

Two cut streams parse into **schema-valid** objects that silently lost an action. That is why the order of checks
matters:

1. **The stop reason first.** `max_tokens` with a `tool_use` in the turn means it was truncated: drop the turn, never
   run it, and retry with a larger budget (lab 06, step 5: 600 tokens, then 2,400). `refusal` can cut a `tool_use`
   mid-input, so run nothing.
2. **Strict parse, then the schema.** Parse the raw fragments with `json.loads`, validate against the tool's JSON
   Schema, then run your content checks. Lab 06 checks that the body names every requested lot, which no schema can
   express.
3. **Answer a bad input with an error result**, built with the JSON library so that quotes are escaped:
   `{"type": "tool_result", "tool_use_id": ..., "is_error": true, "content": "{\"INVALID_JSON\": \"<raw>\"}"}`. The
   model sends the input again (lab 06, step 6).
4. **When the SDK raises** `ValueError` while streaming (JSON it could not parse at all), there is no `tool_use` id to
   answer. Re-issue the request, cap the retries, and let typed API errors (rate limits, authentication) propagate.

Python's `@beta_tool(eager_input_streaming=True)` passes the flag through, and typed tool-runner helpers validate
before they call your function. With raw JSON-schema tools you validate yourself. Availability differs by platform:
the Claude API, Vertex AI, Microsoft Foundry and Claude Platform on AWS accept it for current models, while on Amazon
Bedrock only the newer serving stack does. A proxy in front of the API may reject the field.

**When to use it.** Stream eagerly when a parameter is long and a person or a pipeline is waiting on it: bulletins,
reports, file bodies, code. Leave it off for short parameters, where buffering costs nothing and gives you validation
for free, and wherever your client cannot handle invalid JSON.

**Kestrel example.** Quality drafts technical safety bulletins for recall RC-2026-03 into a `draft_bulletin` tool (800
to 3,000 characters of body, at least three actions). The bulletin screen renders the body as it streams, the
validator above decides whether the draft is saved, and a person reviews it before anything is sent.

## 6. Tool contracts: names, descriptions, schemas, errors, versions

**The idea.** At 120 tools, a tool's contract is what keeps it findable, choosable and safe to change. The contract
is its name, its description, its input schema, the errors it returns, and the promise that it will not change
under a running conversation.

**Descriptions are the selection surface and the retrieval surface.** Lab 07 gives the same twelve tools two
description sets: set A as the catalog wrote them, and set B rewritten in users' words. B says what each tool takes
(the ID shape), what it returns, and which sibling to use instead. The same descriptions decide tool search:

```
  descriptions  hit@1  hit@3  hit@5   MRR
  ------------  -----  -----  -----  ----
  A               67%    83%    83%  0.74
  B               93%    97%    97%  0.95
```

A description that separates near-duplicates names the identifier, the verb in the users' words, and the negative.
For example: "Takes a shipment ID, not a tracking number"; "No cash goes back to the customer"; "any other
reduction of what the customer owes is issue_credit_note". Exercise 4 works through the three families of
near-duplicates in the catalog.

**Schemas guarantee shape, not meaning.** `strict: true` makes the API guarantee that inputs match the schema, but
it requires `additionalProperties: false` on every object (a 400 otherwise, lab 07 step 4) and does not combine with
programmatic calling. Enums close a set (a currency, a priority, a queue). `input_examples` teaches formats and
computed values at a price: `+41 tokens on every request that loads the tool` in lab 07. None of these know that
AR-90257 exists or that 5% of 24,564.00 is 1,228.20. The executor still validates meaning.

**One error envelope.** Every failure has the same shape, marked `is_error: true`:
`{"error": {"code", "message", "next_action"}}`. The code is for your dashboards and retries, the message tells the
model what failed and why (without leaking data), and `next_action` says what to do instead. Lab 07's step 7 prints
four of them:

```
  retired tool      get_shipment: code='retired'
      message='get_shipment has been retired; use get_shipment_v2 with the same arguments.'
      next_action='get_shipment_v2'
```

A negative answer ("not shipped yet") is data, not an error.

**Never edit a live definition.** A description edit changes the bytes at position 0 (the cache) and the tool set
the thinking blocks were bound to (preserved thinking), and it changes behaviour for conversations that are halfway
through. Ship changes under a **new name**, `get_shipment_v2`, then follow four steps: *announce* (v2 declared,
v1's description marked deprecated), *sunset* (v1 answers with the `retired` error that names its successor),
*measure* (calls by name), and *remove* (only when v1's count has stayed at zero). Lab 07's step 6 runs the first
three steps, and exercise 7 designs the whole plan. Under `inline-tools-2026-09-15`, a same-name definition can be
replaced by appending it. Without that beta, a new name is the only append-only way to change a tool.

## 7. Capability-scoped toolsets

**The idea.** Different users of the same copilot may do different things. `advanced/data/security/tenants.json`
gives each role a set of domains, a maximum risk class, explicitly denied tools and, for partners and contractors,
a row filter (`customer_id in tenant.customers`). Three tools are denied to every agent. A session's toolset is
derived from the role, never from what the user asks for.

**Where the pattern comes from.** Capability-based security: a process can only use the capabilities it was handed.
Least privilege: hand out the minimum. The same idea appears as IAM policies and OAuth scopes.

**The toolset and the executor are two halves.** The toolset decides what the model can see and *find*. A deferred
tool is one search away, so a tool the role may not use must not be declared at all, not even deferred. The executor
decides what *runs*. It refuses anything outside the scope with `not_available`, applies the row filter and the
approval limits, and holds even when the model tries something it was never offered. Lab 07's scope table (*mock*
token counts):

```
  role                 tools  domains  max risk      denied  row filter                       prompt (core+search)
  -------------------  -----  -------  ------------  ------  -------------------------------  --------------------
  support_agent           85  9        write              0  -                                               1,240
  support_manager        117  all      irreversible       0  -                                               1,327
  quality_lead            47  5        write              0  -                                                 847
  partner_admin           58  6        write              0  customer_id in tenant.customers                   880
  partner_user            30  4        read               0  customer_id in tenant.customers                   641
  contractor_lead         40  4        write              2  customer_id in tenant.customers                   605
  contractor_engineer     25  3        read               3  customer_id in tenant.customers                   605
```

| Control | What it stops | What it does not stop |
|---|---|---|
| A prompt instruction ("do not use X") | most honest mistakes | a confused or manipulated model: it is guidance, not a control |
| Not declaring the tool | the model seeing or finding it | a call made anyway, from stale history or an injection |
| The executor's scope gate | every out-of-scope call, whatever the model saw | an in-scope tool used on the wrong rows |
| The row filter in the executor | reads and writes of other tenants' rows | a permitted row used for the wrong purpose (Day 5) |

**Kestrel example.** The same request, "List the contacts at C-1016 with their roles and emails.", goes to two
roles. A support agent's session finds and calls `list_contacts`. A contractor engineer's session searches, finds
nothing that fits (the tool is not declared), and says so. When a contractor session calls `list_contacts` anyway, it
gets `not_available`. Exercise 6 designs Keystone's toolsets and the invariants you test before any session starts.

## 8. Tool-selection evals

**The idea.** "The new descriptions are better" is a claim about a distribution of requests, so measure it. The
basic unit is **first-call accuracy**: give the model a request and the toolset, and check whether the first tool it
calls is the expected one.

**Where the pattern comes from.** Classification metrics (accuracy, per-class precision and recall, confusion
matrices), paired experimental designs, and the held-out sets of machine learning. The first course's Day 6 built
the harness; today applies it to tool choice.

**How to build one that tells you something.**

* **Label the first step, not the goal.** "Reduce invoice AR-90257 by 5% as a goodwill credit" needs the invoice
  amount before it needs a credit note, so the expected first call is `get_invoice`. Lab 07 labels the task that
  way for exactly this reason.
* **Report an interval.** Lab 07's 27/30 is 90% with a 95% Wilson interval of 74%-97%, and 29/30 is 97% with 83%-99%.
  The intervals overlap.
* **Compare paired results, task by task.** Both sets ran on the same thirty tasks: `B fixes 3 task(s) A got wrong and
  breaks 1 A got right`. An exact McNemar test on those four discordant pairs gives p = 0.625 (exercise 9). Thirty
  tasks cannot separate 90% from 97%. That takes about 115 tasks if B only fixes, and about 225 if it also breaks a
  few.
* **Look per tool.** Recall shows which tools get missed; precision shows which tools soak up wrong calls. Near-
  duplicates show up as pairs in the confusion matrix.
* **Keep a held-out set.** Set B was written after reading A's failures on these same tasks, so its score is
  optimistic. On ten held-out tasks the scores are 5/10 and 7/10 (exercise 9), and that is the pair of numbers to
  believe.
* **Run it live.** The mock picks tools lexically. It rewards descriptions written in users' words, as models do, but
  it does not reason. Every lab that scores selection says so.

**Kestrel example.** Every description change and every change to the loaded set goes through the eval before it
ships: the 30 tuning tasks, a held-out set that nobody tunes on, and the discovery metrics of section 2 for the
deferred tools. Day 6 grows the set from production traces.

---

## Case study: the copilot that grew from 11 to 120 tools

**Requirements.** Within one quarter, three teams asked for the copilot. Field service wanted tickets, engineer
availability and visit booking. Quality wanted lots, holds, test results and bulletins for recall RC-2026-03. Fleet
telemetry wanted pump readings, vibration trends, fault codes and alerts. The platform team set three constraints:
the same copilot and entry points, cost within a quarter of the 11-tool copilot's, and no loss of first-call accuracy
on the support tasks the copilot already served.

**The first attempt.** Every team registered its tools as a plugin, and each worker process assembled the `tools`
array from the plugin registry at start-up. All 120 tools went on every request. Two things went wrong.

*The bill doubled.* Lab 01 simulates the copilot's day with the attempt's two properties:

```
  the copilot's day                                     prefix hits  per day  vs before
  ----------------------------------------------------  -----------  -------  ---------
  11 tools, fixed order (before)                               100%   $43.43      1.00x
  120 tools, fixed order                                       100%   $51.62      1.19x
  120 tools, order per worker (8 workers, as deployed)          82%   $83.31      1.92x
```

Loading everything was the smaller problem, at +19% while the cache held. The registry was the larger one. Its
iteration order differed per process, so a request could read only what the same worker had written, and a
conversation whose next turn landed on another worker re-wrote its whole history at 1.25x. Nothing errored. The
evidence was `cache_read_input_tokens` falling on the dashboards after the deploy, and nobody was watching it.

*Selection got worse where it hurt.* Support staff reported the copilot answering "where is my pallet right now?"
with the booking record: it called `get_shipment` (carrier, promised ETA) when the user needed `track_shipment` (live
scans). With 120 tools, several shipment and order tools shared every logistics request's words, and the catalog's
descriptions ("Return a shipment: carrier, tracking number, ship date, ETA...") did not say which one answers
"where is it now". This is a production symptom with a live model. The labs' lexical stand-in does not reproduce this
particular confusion. What lab 01's step 5 shows is the direction: 27/30 correct with 12 tools loaded, 24/30 with 120.

**What changed.**

1. **One order, frozen per conversation.** The tools array is built from the catalog in catalog order, stored with
   the conversation (Day 1) and re-sent byte for byte. A deploy check sends two identical requests and fails if the
   second reads nothing from the cache.
2. **Nine loaded, 111 deferred.** The core tools stay loaded, and BM25 tool search reaches the rest. The first
   request fell from 12,224 to 1,482 prompt tokens (lab 03). Discovery precision is tracked, because returned
   definitions are paid for on every later turn.
3. **Phases by tool changes.** The service-visit flow adds and removes tools with `tool_addition` / `tool_removal`.
   Lab 04's version costs 79% of re-sending tools per phase and invalidates nothing. The executor enforces each
   phase.
4. **Fan-out in code.** The recall triage runs as one cell: 276 tokens of tool output in context instead of 1,337
   (lab 05). The triage rule is data in the prompt, and the cell does the arithmetic.
5. **Bulletins streamed and validated.** `draft_bulletin` streams eagerly, and a client-side validator decides
   whether a draft is saved (lab 06).
6. **Contracts rewritten and measured.** Descriptions were rewritten in users' words, and the eval moved from 27/30
   to 29/30 on the tuning set. The team also kept a held-out set, because the paired test cannot yet tell the two
   apart (lab 07, exercise 9). `get_shipment` is being retired behind `get_shipment_v2`.
7. **Scoped by role.** Partner and contractor sessions declare only what their role may use, and the executor checks
   the scope and the row filter on every call (lab 07; Day 5).

**What it teaches.** Tokens, cache fragility and selection are three costs with three different levers, and each
lever needs its own measurement: `count_tokens`, `cache_read_input_tokens` after every deploy, and a selection eval
with a held-out set.

---

## Lab walkthrough

### Lab 01 - `01_catalog_cost.py`: what a large toolset costs

Loads the catalog, measures six request shapes with `count_tokens`, prices a working day of traffic under each
strategy with the cache simulated, replays the first attempt of the case study, and runs lab 07's thirty selection
tasks with 12, 30, 60 and 120 tools loaded.

```bash
python advanced/day2_tools_at_scale/labs/01_catalog_cost.py
```

What to observe: the question alone is 25 tokens and the tool block is everything else. Deferred definitions cost
nothing until found. With a warm cache, loading everything costs +19% a day, while a changing tool order costs 1.92x.
Selection accuracy falls as tools are added:

```
  tools loaded  first call correct  accuracy
  ------------  ------------------  --------
            12  27/30                    90%
            30  26/30                    87%
            60  25/30                    83%
           120  24/30                    80%
```

The `[mock]` note under that table matters. The direction matches what the tool search documentation reports for
Claude, but the size of the drop is the mock's. The eval itself runs cached: each toolset's prefix is written once
and read by the other 29 tasks.

### Lab 02 - `02_tool_search_regex_vs_bm25.py`: tool search, regex vs BM25

Sends one request through each search variant and prints the blocks. It then scores both variants on 32 labelled
tasks whose expected tools are all deferred, explains every regex miss by the text it matched, sends an invalid
regex, and measures what an expanded definition costs.

```bash
python advanced/day2_tools_at_scale/labs/02_tool_search_regex_vs_bm25.py
```

What to observe: the `srvtoolu_` id and the `tool_search_tool_result` in the same response, the score table, and
the invalid pattern that comes back as a 200:

```
  variant  hit@1  hit@3  hit@5 (recall@5)  precision@5   MRR  refs returned
  -------  -----  -----  ----------------  -----------  ----  -------------
  regex      31%    59%               75%         0.15  0.45            4.1
  bm25       97%    97%               97%         0.19  0.97            4.9
```

```
    "error_code": "invalid_tool_input",
    "error_message": "Invalid regular expression pattern: missing ), unterminated subpattern at position 0",
    "type": "tool_search_tool_result_error"
```

The two variants name their input differently, `pattern` for regex and `query` for BM25. The labs read either
through `d2.search_input`.

### Lab 03 - `03_wide_agent.py`: the wide agent, 120 tools and nine loaded

Runs five tasks that need deferred tools (a stuck shipment, a fault code plus warranty, units by seal lot, a visit
booking, a late-fee waiver) on the wide agent (search, nine core tools, 111 deferred) and on a core-only agent,
turn by turn, with the cache accounting of every request.

```bash
python advanced/day2_tools_at_scale/labs/03_wide_agent.py
```

What to observe: the wide agent's first turn searches and calls in the same response, and the core-only agent
escalates what it cannot do:

```
[wide] W1: Where is the shipment for SO-10290? The customer says the carrier has not scanned it since Friday.
    turn 1  tool_use  prompt  1,482 = read      0 + write 1,482 + uncached    0   out  387
            search 'carrier scanned' -> track_shipment, create_shipment, get_shipment, update_delivery_window, schedule_return_pickup
            call   list_shipments_for_order(order_id=SO-10290)
    turn 2  tool_use  prompt  2,346 = read  1,482 + write   864 + uncached    0   out  187
            call   track_shipment(tracking_number=BRL4803670335)
```

```
  task  wide  turns  searches  discovered  prompt tok     cost  core only  turns  prompt tok     cost
  ----  ----  -----  --------  ----------  ----------  -------  ---------  -----  ----------  -------
  W1    done      3         1           5       6,451  $0.0408  partial        2       2,934  $0.0211
  W2    done      2         1           6       4,617  $0.0330  escalated      3       4,490  $0.0207
  W3    done      2         1           5       4,236  $0.0266  escalated      2       2,870  $0.0137
  W4    done      3         1           8       7,492  $0.0365  escalated      2       2,934  $0.0151
  W5    done      2         1           6       4,001  $0.0253  escalated      3       4,466  $0.0204
```

The wide agent costs more per task and finishes every task. That comparison is the one to make: cost per
*completed* task, not cost per request. Step 5 shows the prefix of 1,452 tokens written once and read by every other
task's first turn, because the tools array is byte-identical across conversations.

### Lab 04 - `04_mid_conversation_tool_changes.py`: mid-conversation tool changes

Runs a four-phase service-visit conversation (diagnose, schedule, communicate, diagnose again) two ways. Variant A
re-sends a different `tools` array per phase. Variant B declares everything once and switches with `tool_addition` /
`tool_removal`. It then adds a tool by value (`get_recall_status`, which "shipped" mid-conversation) and breaks the
four placement and header rules on purpose.

```bash
python advanced/day2_tools_at_scale/labs/04_mid_conversation_tool_changes.py
```

What to observe: in variant A, cache reads fall at every phase change (the `<- phase change` rows): to 0 when the
phase's tools array is new, and to 1,652 when the conversation returns to the diagnose tools, whose tools-and-system
prefix from request 1 is still cached; the conversation after it is written again. In variant B they keep growing
(`cache read` 2,379 at the first phase change, 4,652 by request 11). The four 400s end with the accepted request:

```
  [400] tool_removal followed by another user message
        messages.1: a tool_removal must sit immediately before an assistant message, or last in messages
  [400] inline tool_definition under the by-reference beta only
        messages.1.content.0.tool.type: inline tool definitions require the anthropic-beta header 'inline-tools-2026-09-15'
  [accepted] the same request with the inline-tools beta
```

The mock's error texts are phrased after the API's. Match on the rule, not the exact string.

### Lab 05 - `05_programmatic_tool_calling.py`: programmatic tool calling

Triages the eleven units of recall RC-2026-03 three ways: direct calls one per turn, direct calls in parallel, and
one code cell. It prints the code path's pause/resume transcript, asks a second question in the same container,
breaks the protocol's rules, and closes with when code wins.

```bash
python advanced/day2_tools_at_scale/labs/05_programmatic_tool_calling.py
```

What to observe: the parallel path and the code path print the same triage (3 P1, 3 P2, 5 P3). The code path pauses
twice, once for the eleven look-ups and once for the four vibration trends that only hot units need. The second cell
reuses the first cell's variables:

```
  same container: True (container_mock_000040); the cell used `units` from the first cell.
```

Then read the three-ways table (section 4). The code path reads less and writes more, and what it changes for every
later turn is "tool output in context".

### Lab 06 - `06_eager_input_streaming.py`: eager input streaming

Streams a bulletin draft into `draft_bulletin` (`eager_input_streaming: True`) and prints the fragments as they
arrive. It runs the client's validation duties, replays the stream cut in four ways through the SDK's tolerant
parser, provokes a real `max_tokens` truncation, and injects a malformed input to exercise the `INVALID_JSON`
error result.

```bash
python advanced/day2_tools_at_scale/labs/06_eager_input_streaming.py
```

What to observe: the replay table (section 5), then the truncation and the rejection:

```
  attempt 1: max_tokens=600 stop_reason=max_tokens tool_use blocks=1 input keys=[]
     -> truncated tool input: NOT executed, NOT appended; retry the turn with a larger budget
  attempt 2: max_tokens=2,400 stop_reason=tool_use tool_use blocks=1 input keys=['bulletin_id', 'title', 'audience', 'lots', 'body', 'actions']
```

```
  validation: not valid JSON (Expecting ',' delimiter at char 1728)
  tool_result sent back: is_error=True, content={"INVALID_JSON": "{\"bulletin_id\": \"TSB-2026-09\", \"title\": \"TSB-...
  the model sent the draft again: 2,044 characters, validation: schema-valid, every lot named
```

The mock empties a truncated tool input. Live, the SDK hands you whatever its tolerant parser made of the fragments,
often a schema-valid object, which is why the stop reason decides. Step 7 (time to the first fragment, with and
without the flag) runs only live, because the mock has no generation latency.

### Lab 07 - `07_tool_contracts_and_selection_eval.py`: tool contracts and the selection eval

Compares two description sets for twelve tools, as selection (a 30-task eval with Wilson intervals and a paired
comparison) and as retrieval (tool search over the whole catalog). It then shows strict schemas, enums and
`input_examples`, derives capability-scoped toolsets for every role in `tenants.json`, runs a deprecation behind a
versioned name, and prints the error contract.

```bash
python advanced/day2_tools_at_scale/labs/07_tool_contracts_and_selection_eval.py
```

What to observe:

```
  set  correct  accuracy  95% CI (Wilson)  prompt tokens (12 tools)
  ---  -------  --------  ---------------  ------------------------
  A    27/30         90%          74%-97%                     1,686
  B    29/30         97%          83%-99%                     2,311
```

B is longer (625 more tokens for twelve tools) and better on these tasks, but not significantly so on thirty
(exercise 9). The deprecation step shows the model recovering from a retired name by itself:

```
  2. sunset: an old integration still asks for v1 by name -> calls ['get_shipment(shipment_id=SH-50237)', 'get_shipment_v2(shipment_id=SH-50237)']
     the retired name answered with is_error=True and the model retried with the name the error gave:
```

---

## Key takeaways

1. A toolset has three costs: tokens, cache fragility and selection errors. Measure each: `count_tokens`,
   `cache_read_input_tokens` after every deploy, and a selection eval.
2. Build the `tools` array in a fixed order, freeze it per conversation, and re-send it byte for byte. The most
   expensive tool bug of the case study was an iteration order.
3. Defer what is rarely used and load what is used in more than a few percent of conversations. Tool search earns
   its search step once the schemas reach roughly 10K tokens; below that, loading everything is usually cheaper.
4. Descriptions are both the selection surface and the retrieval surface. Write them in users' words, name the ID
   each tool takes, and say which sibling to use instead.
5. Prefer BM25 for prose requests. Regex rewards knowing the catalog's wording, and a regex match is a filter, not a
   ranking.
6. Change the toolset mid-conversation by appending (`tool_addition` / `tool_removal`, inline definitions under their
   betas), never by re-sending `tools`. The cache and preserved thinking both survive an append.
7. A removal is two changes, what the model sees and what the executor allows. Only the second is a guarantee.
8. Move fan-out into code when results matter in aggregate. Keep round trips for judgement, approvals and strict
   tools, and gate code-called tools exactly like direct ones.
9. With eager input streaming, the stop reason outranks a successful parse. Validate strictly, then the schema, then
   the content, and answer failures with an `INVALID_JSON` error result.
10. Never edit a live definition. Version the name, retire the old one with an error that names its successor, and
    remove it when usage stays at zero.
11. Scope toolsets by role from data. Do not declare what a role may not use, not even as deferred, and enforce scope
    and row filters in the executor.
12. A selection eval needs intervals, paired comparisons and a held-out set. Thirty tasks cannot separate 90% from
    97%.

## Further reading

* Tool use overview - https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
* Implementing tool use (descriptions, `input_examples`, tool choice) - https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use
* Tool search tool - https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
* Programmatic tool calling - https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling
* Code execution tool - https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool
* Mid-conversation system messages and tool changes - https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages
* Preserved thinking - https://platform.claude.com/docs/en/build-with-claude/preserved-thinking
* Prompt caching - https://platform.claude.com/docs/en/build-with-claude/prompt-caching
* Token counting - https://platform.claude.com/docs/en/build-with-claude/token-counting
* Streaming (including tool input deltas) - https://platform.claude.com/docs/en/build-with-claude/streaming
* Structured outputs and strict tools - https://platform.claude.com/docs/en/build-with-claude/structured-outputs
* Handling stop reasons - https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
