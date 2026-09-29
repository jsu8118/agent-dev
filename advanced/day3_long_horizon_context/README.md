# Day 3 - Long-horizon context

The first course's Day 3 treated the context window as a budget for one conversation: count it, cache it, retrieve
into it, trim it. After go-live the conversations get long. Kestrel's field-service agent is opened by a technician at
07:00 and closed at 18:00; in between it hears about six sites, reads three week-long historian exports, looks up a
dozen manual sections, logs seven findings and is asked, at the end, for a report and for everything it was told to
remember. That is a long horizon: forty technician messages, seventy-odd requests, a context that grows past 60,000
tokens, and a model that may be upgraded underneath it next month.

Five things go wrong on a long horizon, and today is about the levers for each: the model gets worse as the context
fills with stale bulk (context rot); the bill grows with the square of the turns; facts said at 09:00 are gone at
17:00 after a compaction nobody checked; thinking and output run away on some turns while a hard `max_tokens` cuts
off others; and, on current models, a harness that edits its own history quietly invalidates the model's preserved
thinking. The levers are newer than the first course: task budgets and per-message effort, turn-scoped system
messages, thinking-binding controls, compaction strategies you can measure against each other, tiered memory,
subagent isolation, and cache engineering for a fleet rather than a single chat.

Everything builds on the first course: caching mechanics (breakpoints, TTLs, the invalidation hierarchy), context
editing and server-side compaction, and the memory tool are assumed. Where this day uses a beta surface it names the
header; where the mock stands in for model behaviour (pacing, summarising, remembering), the lab says so.

## Learning objectives

By the end of the day you can:

1. Diagnose a long-running agent's problem as context rot, a cost curve, lost facts, a budget overrun or a binding
   break, and choose the lever that addresses it.
2. Pace a multi-turn task with a task budget, per-message effort and progress updates - and explain why `max_tokens`
   is a ceiling, not a budget.
3. Compare truncation, your own summaries, server-side compaction, tool-result clearing and a structured scratchpad on
   quality (probe questions) and cost, and design the summary contract that decides what survives.
4. Keep a harness compatible with preserved thinking: know which edits break binding on which model, use
   `drop_block` and `input_transformations`, and plan upgrades and fallbacks around model binding.
5. Deliver per-turn instructions as turn-scoped system messages, placed correctly and accounted for in tokens.
6. Design a tiered memory with write policies, consolidation and a contamination guard, and choose between the memory
   tool, your own store and Managed Agents memory stores.
7. Move bulk reading into isolated subagents with verifiable summary contracts.
8. Engineer caching for a fleet: pre-warm fan-outs, stay inside the 20-position lookback, order multi-tenant prefixes,
   and check each model's minimum cacheable prefix.

## Agenda (about 7 hours)

| Time | Block | Lab |
|---|---|---|
| 0:00 - 0:35 | The long-horizon failure modes and the levers | |
| 0:35 - 1:25 | Task budgets, per-message effort and progress updates | 01 |
| 1:25 - 2:35 | Compaction strategies, measured | 02 |
| 2:35 - 2:50 | Break | |
| 2:50 - 3:50 | Preserved thinking and a compatible harness | 03 |
| 3:50 - 4:30 | Lunch | |
| 4:30 - 5:05 | Turn-scoped system messages | 04 |
| 5:05 - 5:50 | Memory architectures | 05 |
| 5:50 - 6:25 | Subagent isolation | 06 |
| 6:25 - 7:00 | Cache engineering at scale | 07 |

Exercises (`exercises/README.md`, 12 of them, worked answers in `solutions/README.md`) are for the evening.

**Running the labs.** `python advanced/day3_long_horizon_context/labs/01_task_budgets_and_effort.py` from the
repository root. Without an API key the labs run against labkit's mock: the SDK code paths, request validation, cache
accounting, context management and thinking-signature checks are real; the "model" is a set of rule-based policies in
`advanced/mock_scenarios/day3_long_horizon_context.py` that answer only from what is in the request. Excerpts below are
mock-mode output. In live mode the numbers and wording differ; the shapes do not. The day is expensive live because
two labs replay the full day several times (simulated totals: lab 01 about $8.90, lab 02 about $11.60, the other five
about $7.30 together); `--turns 14` cuts the first two by more than half, and `--strategies` trims lab 02 further.

**Betas used today.** `task-budgets-2026-03-13`, `mid-conversation-output-config-2026-07-01` and
`thinking-display-updates-2026-08-18` (section 2); `compact-2026-01-12` and `context-management-2025-06-27` (section 3);
`thinking-binding-controls-2026-08-01` (section 4); `mid-conversation-system-clear-at-2026-08-21` (section 5); and
`agent-memory-2026-07-22`, taught from the docs (section 6). A dated header pins the request shape you coded against.
When a beta ends, the header is no longer needed and the fields move to the non-beta `client.messages.create(...)` path,
and names or defaults can change on the way - which is why each lab keeps its headers in named constants and sends
beta fields only through `client.beta.messages.create(...)`: the change is then a small, visible diff.

---

## 1. The long-horizon failure modes, and the levers

**The idea.** A long-horizon agent fails in five distinct ways, and each has its own lever. Treating them as one
problem ("the context is too big, compact it") is how teams trade a cost problem for a correctness problem.

| failure | what you see | where it comes from | primary levers |
|---|---|---|---|
| context rot | answers get vaguer or wrong late in the session; the model quotes the wrong product's table | attention spread over stale bulk (raw exports read hours ago) | shrink what stays: clearing, compaction, subagents |
| the cost curve | the bill grows faster than the work | every request re-sends the history; caching makes the re-send cheap, not free | caching done right, a smaller context, fewer requests |
| lost facts | "I don't have that any more" - or a confident wrong answer | a summary, a window or a clearing pass dropped something | a summary contract, probes, memory for what must outlive the context |
| budget overrun | a turn thinks for minutes; another is cut mid-sentence | no pacing; `max_tokens` as the only control | task budgets, per-message effort |
| binding break | 400s after an upgrade, or silently dropped thinking and higher cost | the harness edits history the model's thinking is bound to | append-only history, `clear_at`, explicit binding controls |

The first two are the first course's context-window budget at a longer scale. The last three are new with long
sessions and with the current models. They interact: a strategy that rewrites history to save tokens (truncation)
restarts the cache and, on a model that binds thinking to the prefix, invalidates reasoning; a summary that saves the
most tokens can lose the fact the technician asks for at 17:00.

**Where the ideas come from.** Much of today is borrowed. Memory tiers mirror the operating-system hierarchy
(registers, cache, RAM, disk: context, prompt cache, memory store, system of record). Compaction is the
log-structured merge of databases - rewrite a growing log into a smaller equivalent - with the same risk: the rewrite
must preserve what readers will query. Task budgets are error budgets from SRE: a quantity the system spends
deliberately instead of a wall it hits. Subagents are map-reduce with a typed contract between the phases. Probe
questions are an eval (Day 6) pointed at the context instead of the model.

**Measure before choosing.** Every lab today prints the same few numbers: prompt tokens per turn (context size), cache
reads and writes, output tokens, cost, and a quality signal - probe questions answered, report completeness, notes
recalled. Lab 02's baseline day ends at 63,785 tokens of context, and lab 01 counts 54,768 tokens of tool results in
it - most of them three historian exports the agent needed for ten minutes each. That single fact explains most of the
day: the bulk is short-lived, and everything that keeps it around (full history) or throws it away blindly
(truncation) pays for it somewhere.

---

## 2. Task budgets, per-message effort and progress updates

**The idea.** Three request-side controls decide how much the model spends, and they are easy to confuse.
`max_tokens` is a per-response ceiling the model cannot see: when output reaches it, generation stops mid-sentence
with `stop_reason: "max_tokens"`, and a `tool_use` block cut off there must never run. `output_config.effort`
(`low`, `medium`, `high`, `xhigh`, `max`) sets how hard the model thinks and how much it writes, per request. A task
budget tells the model how many tokens the whole task may use, and the model sees a countdown and paces itself across
every turn you keep sending it. The first is a safety net, the second a dial, the third a plan.

| | `max_tokens` | `output_config.effort` | per-message effort | `output_config.task_budget` |
|---|---|---|---|---|
| scope | one response | one request | from the next user turn on, until changed | the turns of a task |
| model sees it | no | yes | yes | yes, as a countdown |
| when it binds | hard cut: `stop_reason: "max_tokens"` | always: less thinking, terser | always | the model wraps up as the countdown falls |
| cache impact | none | a top-level change invalidates the messages cache | none - it is an appended message | none |
| beta | - | - (GA) | `mid-conversation-output-config-2026-07-01` | `task-budgets-2026-03-13` |
| models (this course) | all | Opus 5, Opus 5.5, Fable 5.1 (not Haiku 4.5) | Opus 5, Opus 5.5, Fable 5.1 - Fable 5 is a 400 | Opus 5, Opus 5.5, Fable 5, Fable 5.1 (docs: confirm at launch); not Sonnet 5 |

**Per-message effort** is a system message with empty content: `{"role": "system", "content": [], "output_config":
{"effort": "low"}}`. It carries no text, so the placement rules of section 5 do not apply to it; the latest one wins and
it holds until another changes it. Lowering effort this way is reliable; raising it works best for large jumps (`low` to
`xhigh`). Prefer it to changing the top-level `effort` between requests: a top-level change restarts the messages cache,
and (documented for Fable 5.1) steers less reliably, because the model stays consistent with the replies it wrote at the
old level. Unsupported models answer `output_config.effort requires a model that supports per-turn effort; this model
does not`.

**Task budgets** go in `output_config`: `{"task_budget": {"type": "tokens", "total": 100000}}` with the beta header
`task-budgets-2026-03-13`; `total` must be at least 20,000. The budget counts what the model generates (thinking
included) and the tool results it reads - not the full history you resend on each request. It is advisory: the model may
finish less thoroughly when it runs low, and it says so. Leave the optional `remaining` field unset in a normal loop;
pass it only when you rewrote history (a summary reset, a client-side compaction) and the server can no longer see what
was spent. A Managed Agents session budget (Day 4) is a different thing: a hard, dollar-denominated cap the platform
enforces.

**Progress updates.** On the Fable models (and, per the platform docs, Claude Opus 5.5), text between tool calls comes
back as short `thinking` blocks - empty under the default `display: "omitted"`, which is why a long agentic turn can
look silent for minutes. `thinking: {"type": "adaptive", "display": "updates"}` with the beta header
`thinking-display-updates-2026-08-18` returns those notes as text while the reasoning stays hidden: under `updates`,
any thinking block with non-empty text is a progress note to render as a status line, and a response can have zero or
more of them. They are billed at full length and must be echoed back unchanged like any thinking block. Claude Opus 5
has no `updates` display - its between-tool narration is ordinary text blocks - and the mock rejects it there, as
lab 01's matrix shows.

**When to use which.** Keep a generous `max_tokens` on every request as the ceiling that protects you from a runaway
response. Choose effort per kind of turn - a diagnosis from telemetry deserves `high`, "noted, heading to Harbor
Foods" does not - and change it with per-message effort so the cache survives. Add a task budget when the task is long
enough that pacing matters and you can size it from a measurement (exercise 7). Turn on progress updates when a person
watches a long turn.

**What lab 01 measured.** The day four ways on Claude Opus 5:

| policy | truncated | output tokens | cost | end-of-day report |
|---|---:|---:|---:|---|
| `max_tokens=512`, effort high | 3 | 17,764 | $2.23 | cut mid-sentence |
| `max_tokens=16000`, effort high | 0 | 17,989 | $2.23 | complete |
| task budget 100k + per-message effort | 0 | 10,852 | $2.05 | complete |
| task budget 64k + per-message effort | 0 | 8,256 | $1.97 | one line |

Per-message effort cut output by 40% and the bill by only $0.18: input is 80% of this day's cost. Budgets and effort
make a long session predictable and faster; they are not where its money is (that is sections 3, 7 and 8). The tight
budget shows the other side: the countdown ran out at Westfield and the report came back as one line - a budget below
what the work needs degrades the work, which is why you size it from measurements with a margin.

**Kestrel example.** The field agent sends `effort: "medium"` at the start of the day and a per-message change before
each turn whose kind calls for another level (`high` for telemetry, `low` for "log it" and "remember"), a task budget
of 90,000 tokens per technician-day (65,620 measured plus a margin), and progress updates on the Fable 5.1 route the
supervisors' dashboard watches.

---

## 3. Compaction strategies compared, with measurements

**The idea.** Something has to leave the context of an all-day session. Five strategies decide what, and they differ
in who decides, what survives, what they do to the cache and to preserved thinking, and what they cost. The only way
to compare them honestly is to run the same session through each and ask questions whose answers you know: **probe
questions**, asked at the end of the day in a fork of the conversation, about facts said at 09:00. The mock answers a
probe only from what is still in its context - the same constraint a real model has, without the real model's
judgement.

| strategy | who decides | what survives | cache | preserved thinking | when |
|---|---|---|---|---|---|
| truncation (sliding window) | your code, by size | the newest whole turns | rewritten from the first dropped turn | breaks (retained turns were minted with the dropped ones) | never as the only strategy |
| own summarisation (simple compaction) | your code, at boundaries | what your instructions ask for | restarts at each reset | safe: the new conversation replays nothing older | natural boundaries (a site visit) |
| server-side compaction `compact_20260112` | the API, past a trigger | what the summary keeps | restarts after the compaction block | safe: the checked prefix starts at the block | long sessions with no natural boundary |
| tool-result clearing `clear_tool_uses_20250919` | the API, past a trigger | everything but old tool results | each clearing pass rewrites from the first cleared block | safe: server-side edits do not count | bulky, short-lived tool results |
| structured state extraction (a scratchpad) | your schema, at boundaries | exactly the schema's fields | restarts at each reset | safe, like a summary reset | the state is known in advance |

**How each works underneath.**

* *Truncation* drops the oldest whole turns - a technician message with every tool round it caused - until the request
  fits; dropping single blocks would orphan `tool_use`/`tool_result` pairs (a 400). It forgets whatever sits in the
  oldest turns, which is not the same as what matters least: in lab 02 the window let go of the GBWD visit's manual
  facts but kept Harbor's small turns after its bulky arrival turn had gone.
* *Own summarisation* is a fork: the same system prompt, tools and messages plus one request - "summarise so the
  session can continue" - so the summariser reads the transcript from the cache at 0.1x instead of re-processing it.
  At the next turn the harness starts a new conversation from the summary plus the new message and replays nothing
  older. That is the shape the platform docs recommend for client-side compaction ("simple compaction"). The
  instructions are the contract: they decide what survives. Re-summarising a summary at every boundary can drift;
  carrying structured lines forward verbatim (below) does not.
* *Server-side compaction* (beta `compact-2026-01-12`) summarises earlier turns once the input passes the trigger
  (default 150,000; the minimum is 50,000) and returns a `compaction` block first in the response. Append the **full**
  `response.content`; the API ignores everything before the block from then on. The summarisation pass is its own
  entry in `usage.iterations` and the top-level usage excludes it, so bill by summing the iterations. `instructions`
  replaces the default summarisation prompt. An on-demand variant (`compact-2026-09-04`) lets your code decide when.
* *Tool-result clearing* (beta `context-management-2025-06-27`) replaces old tool results with a placeholder once the
  input passes `trigger`, keeping the newest `keep` tool uses; `clear_at_least` skips a pass that would free too little
  and `exclude_tools` protects small, vital results (here `log_finding`, which the end-of-day report needs). Your
  history keeps everything: the edit happens per request, server-side, and `context_management.applied_edits` reports
  it. The model keeps its own replies - which is why it still answers questions it answered once before.
* *Structured state extraction* asks for one site's state against a JSON schema (`output_config.format`) at each
  departure, validates it, stores it outside the context, and renders it into the next conversation as a scratchpad.
  The schema is the contract, the store is auditable, and the same record feeds long-term memory (section 6).

**What lab 02 measured** - the same 40 messages, six ways, eight probes:

| strategy | probes | report sites | notes recalled | peak context | cost |
|---|---:|---:|---:|---:|---:|
| none | 8/8 | 6/6 | 5/5 | 63,785 | $2.23 |
| truncation (30K window) | 6/8 | 5/6 | 4/5 | 29,406 | $1.64 |
| own summary per site | 7/8 | 6/6 | 5/5 | 21,390 | $1.46 |
| server-side compaction (50K trigger) | 3/8 | 4/6 | 1/5 | 42,697 | $1.94 |
| tool-result clearing (30K trigger) | 7/8 | 6/6 | 5/5 | 23,565 | $2.12 |
| scratchpad per site | 7/8 | 6/6 | 5/5 | 21,529 | $1.39 |

Four readings. The scratchpad and the own summary keep almost everything at two thirds of the baseline's cost, because
they reset at natural boundaries and carry forward only structured lines. Clearing keeps quality but saves little
money: every pass rewrites the cached conversation from the first cleared result, which is why the first course called
context editing a context-window tool rather than a savings lever. The compaction row reflects the mock's stand-in
summary (the recent user requests and the list of tool calls, whatever the instructions say); a real Claude summary
follows your instructions and keeps far more - but the lab's point stands: you learn what a summary dropped only by
probing it. And the one probe every reducing strategy failed - the heatsink temperature at Riverbend's last F05 trip -
lived only in a raw controller-log export that nobody asked a summary to keep. Exercise 11 fixes it by changing the
contract, not the harness.

**The dangerous miss is not "I don't know".** Under truncation the KP-400 grease question came back with the KP-250's
regrease row - a plausible number from the wrong product family, the only grease table left in the window. Probes must
check the value, not just that an answer came back.

**Kestrel example.** The field agent resets at each site departure with a scratchpad extracted against a schema:
findings, open items, cited facts, remember-notes, parts used, and - after the heatsink probe failed in CI - the latest
controller-log entry per fault code. Server-side compaction stays on as a backstop for days with no departures (a
single all-day commissioning job), with a trigger well above the steady state.

---

## 4. Preserved thinking: signatures, binding and a compatible harness

**The idea.** On current models a `thinking` block is signed, and the signature records two bindings: **which model
produced it** and **the conversation prefix it was produced in** - the top-level `system` prompt, the set of tools, and
every message before it (after a server-side compaction, the prefix starts at the compaction block), plus a chain to the
previous thinking block. When the transcript comes back, the API checks both. The point is integrity: reasoning that
was produced for one conversation cannot be replayed into another, or into an edited version of the same one. The
consequence for your harness: **history must be append-only**.

**The two checks.**

* *The model check* runs first. A block the receiving model cannot read is dropped before generation - a 200, unbilled,
  reported as `{"type": "thinking_dropped", "reason": "model_binding_mismatch"}` when the beta header is on. Claude
  Opus 5 blocks are read by Claude Opus 5.5 and Claude Fable 5.1; Claude Opus 5.5 blocks, on the Claude API, only by
  Claude Fable 5.1 and Mythos 5.1; Claude Fable 5.1 blocks only by Fable 5.1 and Mythos 5.1.
* *The prefix check* compares the prefix recorded in the signature with the one in the request. What happens on a
  mismatch depends on the model and the account: accounts created on or after 2026-08-31 are enforced (a 400) on Fable
  5.1 and Opus 5.5; older accounts are "recorded" - the block still reaches the model, listed as
  `thinking_mismatch_allowed` when the beta header is on, and enforced only when the request sets the field below
  (any value). Claude Opus 5 runs no prefix check; Claude Mythos 5.1 does not run it either. The mock models Fable 5.1
  as enforced and Opus 5.5 as recorded, so both behaviours are visible in one lab.

**The controls** (beta `thinking-binding-controls-2026-08-01`): `thinking: {"type": "adaptive", "block_binding":
{"prefix_mismatch_behavior": "error" | "drop_block"}}`. With `drop_block` the API drops the first mismatched block and
every thinking block after it (up to the next compaction block) and proceeds; each drop is listed in the response's
top-level `input_transformations`. The drop applies to that request only. With the header, every response from a
thinking model carries `input_transformations` (an empty list when nothing happened); without it, the field is absent
and `block_binding` is a 400 (`Extra inputs are not permitted`). The 400 on an enforced model reads, in lab 03:

```
  messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different
  conversation. Remove the block, or set `thinking.block_binding.prefix_mismatch_behavior` to
  "drop_block". That setting requires the `thinking-binding-controls-2026-08-01` value in the
  `anthropic-beta` header.
```

**What breaks binding, and what does not** (lab 03 runs every edit below and the first harmless change of each row;
the rest of the harmless rows are the docs' list):

| edit before the new turn | Fable 5.1 | Opus 5.5 (older account) | Opus 5 | append-only form |
|---|---|---|---|---|
| reword or re-order an earlier turn | 400 | allowed, listed | nothing | never edit; append a correction |
| trim or delete an old tool result | 400 | allowed, listed | nothing | server-side clearing |
| drop the oldest turns (truncation) | 400 | allowed, listed | nothing | server-side compaction, or simple compaction |
| re-render the system prompt (a clock, a status line) | 400 | allowed, listed | nothing | freeze it; append a `{"role": "system"}` message |
| change a tool description, add a tool | 400 | allowed, listed | nothing | declare all tools up front; `tool_addition` / `tool_removal` |
| inject a reminder, delete it next request | 400 | allowed, listed | nothing | a turn-scoped system message left in place (section 5) |
| reorder tools; add an unreferenced `defer_loading` tool | fine | fine | fine | - |
| append a system message; leave cleared `clear_at` messages | fine | fine | fine | - |
| change `max_tokens`, `effort`, `tool_choice`, `cache_control` markers | fine | fine | fine | - |

**Model binding across upgrades.** A conversation that moves to a model that cannot read its blocks loses that
reasoning for the request - not the request: Opus 5.5 to Opus 5 is a 200 with every Opus 5.5 block dropped, and the
target model re-plans (higher cost and latency on that turn). Moving up keeps it: Opus 5 to Opus 5.5 or Fable 5.1, and
Opus 5.5 to Fable 5.1, drop nothing. A round trip loses nothing if the harness keeps sending the same messages; never
strip blocks on a switch, the API already leaves out what the target cannot read. One trap: the model check runs first,
so a downgrade *hides* an edit for one request - the edit surfaces as a 400 on the next Fable 5.1 turn.

**Where compaction resets the prefix.** Server-side compaction and context editing never count as edits: the check
compares the conversation *as you sent it*, and after a compaction block the checked prefix starts at the block.
Client-side, simple compaction (a summary plus the new turn, nothing older) is clean because nothing bound to the old
transcript is replayed. *Keep-tail* compaction - a summary plus the last few turns verbatim - fails: the retained turns'
thinking was minted with the full history in front of it. Strip those turns' thinking blocks (text and tool calls stay)
or send `drop_block`; background compaction that swaps a summary in while the conversation continues fails the same way.

**The harness compatibility checklist.** Freeze the top-level system prompt and the tool definitions for the life of a
conversation; append system messages for changes; declare every tool at session start (deferred where needed); store
and replay the wire JSON, never rebuild messages from your own objects (re-serialisation changes bytes); append
`response.content` verbatim, thinking included; deliver per-turn reminders as turn-scoped system messages; compact on
the server or with simple compaction; send the beta header and an explicit `prefix_mismatch_behavior` everywhere -
`"error"` in CI, so an edit fails the build, and `"drop_block"` in production, with an alert on
`input_transformations`. Lab 03 turns this list into a guard that names the broken rule before the request is sent;
exercise 10 extends it to predict exactly which blocks the API will drop.

**Kestrel example.** See the case study: the week the Opus 5.5 upgrade started dropping thinking blocks.

---

## 5. Turn-scoped system messages

**The idea.** A harness often knows something that is true for one turn only - the technician is at the pump with
gloves on, the gas-test certificate expires at 11:40, the budget is at 30%. The first course showed the
mid-conversation `{"role": "system"}` message: operator authority without editing the top-level prompt. A persistent
system message is right for a rule that stays true; for a fact about *this* turn it is wrong twice: it keeps applying
after the turn, and every copy you add is re-read on every later request. Deleting last turn's copy is worse: it is a
history edit (cache restart, and on Fable 5.1 a 400). The turn-scoped system message solves this: `{"role": "system",
"content": "...", "clear_at": "next_user_message"}` (beta `mid-conversation-system-clear-at-2026-08-21`) renders for
one turn, then stays in the transcript, cleared - no tokens, still part of the prefix.

| delivery | authority | what the model reads later | cache | binding | beta |
|---|---|---|---|---|---|
| turn-scoped system message (`clear_at: "next_user_message"`) | operator | nothing once a later user message exists | kept | kept | `mid-conversation-system-clear-at-2026-08-21` |
| persistent system message (`clear_at` "never", the default) | operator | every copy, every turn | kept | kept | none (GA on Opus 5, Opus 5.5, Fable 5.1; not Sonnet 5) |
| `<system-reminder>` text in the user turn, kept | user | every copy, every turn | kept | kept | none |
| `<system-reminder>` injected, deleted next request | user | one copy | rewritten every request | broken | none |

**Placement rules** (each a 400 in lab 04). A system message must follow a user message (or an assistant message ending
in server-tool use) and be the last message or be followed by an assistant message - so it cannot be `messages[0]`, and
it cannot sit between a user message and the next user message: put all of a tool round's results in one user message
and the reminders after it. `clear_at` takes `"never"` or `"next_user_message"`; a turn-scoped message is text only (no
`output_config`, no tool changes) and takes no `cache_control` - put the breakpoint on the preceding user turn. The beta
header is required, and the model must accept mid-conversation system messages: Claude Sonnet 5 accepts none.

**The tool-loop rule.** A `tool_result`-only user message *is* the next user message, so it clears a reminder placed
after the technician's question. If the reminder must stay in view for the answer that follows a tool round, append a
fresh copy after each tool_result message and leave every earlier copy where it is - they are already cleared, cost
nothing and keep the prefix intact. Lab 04 shows the bullet-point reminder vanishing from the post-tool answer when it
was sent once, and holding when re-sent.

**Authority.** A system message carries operator authority and cannot be forged by anything that writes user-visible
input; text inside a user turn - including a `<system-reminder>` your harness wrote - is indistinguishable from text the
technician pasted from a customer's e-mail. That matters for anything security-relevant (Day 5): a reminder like "safety
faults here are escalated, never repaired" belongs in a system message.

**Token accounting.** Lab 04 counts, with `count_tokens`, what the model reads of the status reminder on each turn of
the Cobalt visit: turn-scoped delivery costs one copy per turn (55 tokens, 385 over the visit); persistent messages and
kept injections accumulate (1,540 and 1,680 by the seventh turn); inject-then-delete reads one copy but rewrote the
previous user turn on every request - and because the 16K-token Cobalt export sat after that turn, the visit's cache
writes rose from 20,640 to 124,716 and its cost from $0.33 to $0.93.

**Kestrel example.** At a hazardous-area site the harness sends, after every technician message and after every tool
round, a turn-scoped status line: site, zone, certificate expiry, open work order. The one standing rule of the visit -
escalate, never repair, safety faults in Zone 1 - is a persistent system message appended on arrival and ended by
another appended message on departure ("the Zone 1 rule no longer applies"); nothing is ever deleted.

---

## 6. Memory architectures

**The idea.** The context is working memory: fast, expensive, and gone at the end of the session. Anything the agent
must know next week lives outside it, and "outside" needs the same design care as a database. Three tiers, borrowed from
cognitive science's episodic/semantic distinction and from databases' log-plus-materialised-view:

| tier | holds | written by | lifetime | Kestrel |
|---|---|---|---|---|
| working | this visit's state | the harness, from the model's replies (a scratchpad) | the session | lab 02's scratchpad |
| episodic | dated notes of what happened or was said, with provenance | the agent through a write tool; integrations | weeks, then consolidated or expired | "2026-09-15, harbor: hot-work permit before grinding" |
| semantic | consolidated, deduplicated facts per site, with sources and confirmation counts | a consolidation job only | until contradicted or expired | S-1: "HF-KP250-03 may only be stopped Sundays 06:00-10:00" (confirmed twice) |

**Write policies** decide quality more than retrieval does:

* *Reads before writes.* Search memory before answering and before writing; if the fact is already there, confirm it
  (a confirmation count and date) instead of writing a duplicate.
* *Atomic notes.* One fact per note. In lab 05 the Harbor note carried a known fact (the Sunday window) and a new one
  (the hot-work permit); confirming the whole note would have silently swallowed the permit, so the agent splits it.
* *Systems of record win.* "Bearings on HF-KP250-03 replaced today (WO-24502)" is an event the CMMS owns; consolidation
  leaves it there instead of promoting it to a second, drifting copy.
* *Provenance on every row*, a quarantine state, and retention. The agent never deletes; jobs do.
* *Consolidate in batch*, not per turn: a nightly job reads the day's episodes, proposes facts against a JSON schema,
  and the harness validates, dedupes and records sources. Lab 05's consolidation cost $0.0170 for the day.

**Memory is untrusted data** (a preview of Day 5). Whatever was written can be wrong, stale or planted. Lab 05's
customer e-mail, synced into memory by a CRM integration, told "the assistant" to bypass the dry-run interlock and mail
the site log out. The write path rejects personal data outright (the first course's PRV-004 guard) and quarantines
instruction-like content; consolidation never promotes an untrusted source without review; notes are rendered to the
model as data with their ids; and the agent says when it withheld something. None of this lives in the prompt - it is
code on the write path.

**Three ways to give an agent memory.**

| option | who writes | where it lives | what you control | notes |
|---|---|---|---|---|
| memory tool `memory_20250818` | the model, with file commands (`view`, `create`, `str_replace`, ...) | your storage, through your handler | paths, guards, audit in the handler | no beta; first course lab 07 |
| your own store (lab 05) | the model proposes through your tools; your code decides | your database, tiered | write policy, consolidation, quarantine, retention | any model |
| Managed Agents memory store | the agent, with its file tools on a mounted directory | Anthropic, workspace-scoped | versions on every change (audit, rollback, redaction), read-only or read-write per session | beta `agent-memory-2026-07-22`; not simulated here |

A Managed Agents memory store is attached when the session is created - `resources=[{"type": "memory_store",
"memory_store_id": ..., "access": "read_write" | "read_only", "instructions": ...}]` - and mounted at
`/mnt/memory/<store-name>/`, at most eight per session; never put credentials in one. A session is not memory: lab 05
runs the same memory tools behind a Managed Agents session, whose event history holds one conversation, while a new
session starts with no events.

---

## 7. Subagent isolation for bulk reading

**The idea.** Reading bulk is a different job from reasoning with it. A subagent - a separate request with its own
context, no conversation history and no tools, returning a typed contract - reads one bulky input and hands back what
the coordinator needs. The coordinator's context stays small for the rest of the day, the readers can run in parallel
on a cheaper model, and a hostile line in one input reaches the coordinator only as a string inside a schema field.

| approach | coordinator context | cost of later turns | risk | when |
|---|---|---|---|---|
| stuff everything | all of it (49,050 tokens for six logs) | every request re-reads it (cached, 0.1x) | context rot; one poisoned document sits beside everything | small, stable inputs read many times |
| retrieval (first course) | the top-k chunks | small | misses what retrieval misses | large corpora, point questions |
| subagents + contract | the contracts (about 1K tokens) | small | details outside the contract | bulky inputs, known question shapes |
| compaction after the fact | shrinks later | falls after the compaction | loses what the summary drops | no natural boundary |

**Summary contracts.** A contract is a JSON schema passed as `output_config.format`, plus checks in code before anything
reaches the coordinator: validate the schema, verify every pointer (each fact's `source_line` must contain the fact),
and check completeness where you can count (`readings_scanned` against the log's own line count). Design the fields
from the questions the coordinator will be asked; give it a drill-down tool - "have the reader re-read this source for
this detail" - for the rest. Structured outputs require closed objects (`additionalProperties: false`), so a count map
becomes a list of `{code, count}` items.

**What lab 06 measured.** Six site logs, a plan for the day, six follow-up questions:

| design | coordinator context at the end | cost |
|---|---:|---:|
| stuffed logs | 49,675 | $0.4952 |
| six readers on Claude Sonnet 5 + coordinator on Claude Opus 5 | 2,264 | $0.1867 |

Both answered all six questions - one through a drill-down, because "the heatsink temperature at the latest F05" is not
in the contract. The gap grows with every turn that follows: each request re-reads the coordinator's context, $0.0248
of cached input for the stuffed design against $0.0011 for the subagent design.

**When not to.** When the task needs cross-document reasoning over raw detail (comparing two raw exports line by line),
when the inputs are small and read many times (cache them), or when the contract cannot be designed in advance and the
drill-downs would outnumber the questions. Day 4 turns subagents into a durable coordinator with work queues; Managed
Agents' multiagent sessions (a coordinator with worker agents in threads) are the hosted form.

---

## 8. Cache engineering at scale

**The idea.** The first course cached one conversation. A fleet adds four effects that single-conversation thinking
misses.

**Concurrency.** An entry becomes readable only once the request that wrote it has started responding. N concurrent
requests over the same new prefix all write it - lab 07's eight technicians at 07:00 paid eight writes and read nothing.
Two fixes: send one request, wait for its first streamed token, then fan out the rest; or pre-warm with `max_tokens: 0`.
A pre-warm runs prefill only and returns `content: []`, `stop_reason: "max_tokens"`, zero output tokens and the normal
write charge. Put the breakpoint on the last block shared with the real traffic (the system prompt or tools) - not on
the placeholder user message and not through automatic caching, which would key the entry to the placeholder - and send
the same thinking and effort settings as the real requests. `max_tokens: 0` is rejected with `stream: true`,
`thinking.type: "enabled"`, `output_config.format`, forced `tool_choice` and inside Message Batches. The mock stands in
for response latency with `mock_api().cache.ready_delay`.

**The 20-position lookback.** Each breakpoint walks back at most 20 positions looking for an entry an earlier request
wrote; a run of consecutive `tool_use` blocks is one position, and so is a run of `tool_result` blocks. A request that
appends more than 20 positions since the last entry silently misses and re-writes everything after the last entry it
can reach. Lab 07 finds the boundary exactly (19 appended blocks after a one-block reply hit, 20 miss) and then the way
it happens in agents: a harness that keeps one breakpoint on the newest technician message, a turn of eight sequential
look-ups (four positions each), and a next turn that re-writes a 17K-token export. Fixes: automatic caching (an entry at
the end of every request), batching independent calls into one parallel round, or an intermediate breakpoint - within
the four-breakpoint limit.

**Multi-tenant prefix layout.** The prompt renders tools, then system blocks in order, then messages. Put what all
tenants share first, with a breakpoint, then the tenant's block, with a breakpoint, then the conversation. With the
tenant block first nothing is shared: lab 07's six customers wrote 24,978 tokens against 6,648 for the shared-first
layout. Caches are isolated per workspace (per organisation on Amazon Bedrock and Google Cloud), so "shared" means
shared inside your workspace; keep tenant-private bytes after the shared prefix. If the shared part uses the 1-hour TTL,
it must come before any 5-minute entry - longer TTLs first.

**Breakpoint strategy and minimums.** Four breakpoints per request: typically the static prefix, the tenant block, and
automatic caching for the conversation tail, with one spare for an intermediate breakpoint in a long turn. Below a
model's minimum cacheable prefix nothing is cached and nothing errors:

| model | minimum cacheable prefix | cache read price |
|---|---:|---|
| Claude Opus 5 | 512 | 0.1x |
| Claude Opus 5.5 | 512 | 0.05x |
| Claude Fable 5.1 | 512 | 0.025x |
| Claude Sonnet 5 | 1,024 | 0.1x |
| Claude Haiku 4.5 | 4,096 | 0.1x |

With reads at 0.025x on Fable 5.1, a miss costs 50 times a hit; for idle gaps of 5-60 minutes a `max_tokens: 0`
keep-alive on the 5-minute TTL is usually cheaper than the 1-hour TTL (exercise 6).

**What lab 07 measured**, per unit, with the fleet projection it prints (40 technicians, 250 days):

| technique | measured saving | unit |
|---|---:|---|
| pre-warm before the 07:00 fan-out | $0.1458 | per 8-technician morning |
| automatic caching through long tool loops | $0.1009 | per long look-up turn |
| shared-first prefix layout | $0.1186 | per 6 cold customer prefixes |

---

## Case study: Kestrel's all-day field-service agent

**The system.** Kestrel's field technicians keep the assistant open on a phone all day. The day the labs replay is a
typical one: six sites - a water district, a food plant, a brewery, a dairy, a chemical works with a Zone 1 area and a
hospital plant room - forty technician messages, three week-long historian exports of about 15,000 tokens each, and an
end-of-day report the office bills from. The service manager's requirements: technical answers cited to the manual
section; a complete end-of-day report; next-visit notes kept, without personal data; hazardous-area rules never
softened; a predictable cost per technician-day; and no surprises when the model underneath changes.

**Version 1** ran the whole day in one conversation on Claude Opus 5, with server-side compaction at the 50,000-token
minimum as the only context control and `max_tokens: 1024` as the cost control. The harness re-rendered a clock and the
current site into the system prompt on every request, injected per-turn reminders into the user message and deleted
them on the next request, and kept next-visit notes in the conversation.

**The day it forgot a torque spec.** On a heavy day in July compaction fired as the technician arrived at the chemical
works: the third historian export pushed the input over the trigger. Late in the afternoon the technician asked what
torque had been used on the baseplate foundation bolts at the food plant that morning. The summary had kept the list of
requests and tool calls, not the torque table, and the agent answered with a value from another row it could still
see; the technician caught it against the torque-wrench log. Lab 02 reproduces the shape: after the compaction at turn
27 three of eight probes survive, and under truncation the KP-400 grease question is answered from the KP-250's table.

**What changed first.** Probe questions in CI, asked of a recorded day after every change to the context strategy. A
scratchpad extracted against a schema at each site departure (findings, cited facts, open items, remember-notes, parts,
and - once the heatsink probe failed - the latest controller-log entry per fault code), with server-side compaction kept
only as a backstop. Next-visit notes moved to a tiered memory with nightly consolidation and a quarantine for
customer-supplied notes. Log exports moved behind reader subagents with a contract. `max_tokens` went back to 16,000; a
task budget per technician-day and per-message effort took over pacing.

**The week the Opus 5.5 upgrade started dropping thinking blocks.** In September the team moved the agent to Claude Opus
5.5. The canary showed no errors - the account predates 2026-08-31, so the prefix check was recorded, not enforced - and
the team had added the `thinking-binding-controls-2026-08-01` header with `prefix_mismatch_behavior: "drop_block"` to be
safe. A week later long days were slower and used more output tokens, and `input_transformations` said why:
`thinking_dropped` with `prefix_binding_mismatch` on nearly every request, because the clock in the system prompt and
the deleted reminders invalidated every earlier thinking block and the model re-planned each turn from scratch; and
`model_binding_mismatch` on the turns the overload fallback sent to Claude Opus 5, which cannot read Opus 5.5's blocks.
Nothing had failed; everything had quietly become worse.

**What changed second.** The harness became append-only, and a guard proves it:

| concern | design now | lab |
|---|---|---|
| pacing | `max_tokens` 16,000 as the ceiling; a task budget per technician-day; per-message effort by turn kind | 01 |
| context | a scratchpad per site visit (simple compaction); probes in CI; server compaction as a backstop | 02 |
| binding | frozen system prompt and tools; the guard in CI with `"error"`, `"drop_block"` plus alerts in production; the overload fallback routed to Fable 5.1, which reads Opus 5.5's blocks | 03 |
| per-turn facts | the clock and site status as turn-scoped system messages, re-sent after each tool round, never deleted | 04 |
| next visit | episodic notes through a guarded write path, nightly consolidation into semantic facts | 05 |
| bulk | reader subagents with a verified contract; a drill-down tool for the rest | 06 |
| fleet cost | a `max_tokens: 0` pre-warm per depot before 07:00; automatic caching in tool loops; shared card before tenant block | 07 |

---

## Lab walkthrough

### Lab 01 - `01_task_budgets_and_effort.py`: pacing a 40-turn day

`python advanced/day3_long_horizon_context/labs/01_task_budgets_and_effort.py` replays the day on Claude Opus 5 under
four request policies, prints the pacing of the two budget arms, runs the first site visit on Claude Fable 5.1 with a
task budget, per-message effort and progress updates, and asks the API which model accepts which surface. Observe that
the cap cuts the longest answers - the model never knew it was there - while the budget arms finish every turn; that
per-message effort barely moves a bill dominated by input; and that the tight budget's report is one line. *Mock mode:*

```
  policy                                 turns  truncated  output tokens  cost   day report
  -------------------------------------  -----  ---------  -------------  -----  ----------
  max_tokens=512, effort high               40          3         17,764  $2.23  TRUNCATED
  max_tokens=16000, effort high             40          0         17,989  $2.23  complete
  task budget 100k + per-message effort     40          0         10,852  $2.05  complete
  task budget 64k + per-message effort      40          0          8,256  $1.97  one line
```
```
The cap cut 3 answers mid-sentence (stop_reason=max_tokens), turns 1, 27, 39 (arrive, report). The model never knew the limit was there. The end-of-day report, as the technician received it:
  ...eaned and cooling fan replaced (KC-1-FAN x1), plus a seal inspection scheduled after the F10
  dry-run event.
  - cedar: KP100-2604-0004: mechanical seal failed by dry running after an F10 dry-run trip,
  warranty excluded per SEAL-FA-02 §2. Open: KP100-2604-0004: seal replaced (MS-100 x1) and a low-
  level [cut]
```
```
Task budget 64,000 tokens - sized below what the day needs:
  site       turns  effort per turn  output  output/turn  output + tool results
  ---------  -----  ---------------  ------  -----------  ---------------------
  gbwd           7  M M H L M M L     2,173          310                 19,169
  harbor         7  M M M M H L L     1,801          257                 17,887
  riverbend      6  M M M M L L       1,170          195                  3,192
  cedar          6  M M M M L L       1,158          193                  2,785
  cobalt         7  M H M M L H L     1,213          173                 17,897
  westfield      7  M M M M L M L       741          106                  2,094
  Spend the harness can observe: 8,256 output + 54,768 tool-result tokens = 63,024 of 64,000 (2% left).

Turn 39 under the tight budget:
  End-of-day report, 6 site(s) in my context: gbwd, harbor, riverbend, cedar, cobalt, westfield.
  (Task budget spent: essentials only.)
```

The progress updates arrive as thinking blocks with text, one before each tool call, and the support matrix shows which
model rejects what:

```
  turn  1  progress update: Fetching the site brief for gbwd first.
  turn  1  progress update: Now the log export for gbwd.
  turn  2  progress update: Reading IOM-KP250 §3.
  turn  3  progress update: Asking the historian for vibration_mm_s (summary) on GB-KP250-03.
```
```
  model             task budget  per-message effort  display updates
  ----------------  -----------  ------------------  ---------------
  claude-opus-5     ok           ok                  400 (1)
  claude-fable-5    ok           400 (2)             ok
  claude-fable-5-1  ok           ok                  ok
  (1) thinking.display: "updates" is not supported for claude-opus-5
  (2) output_config.effort requires a model that supports per-turn effort; this model does not
```

Live, Claude paces with judgement rather than the stand-in's thresholds, and the progress notes are its own words.

### Lab 02 - `02_compaction_strategies.py`: six ways to survive the day

`python advanced/day3_long_horizon_context/labs/02_compaction_strategies.py [--strategies ...]` runs the day with no
management and with the five strategies of section 3, asks the eight probes in a fork at the end of the day, and prints
the evidence for each strategy (the truncation window, a summary, the compaction block and its `usage.iterations`, the
clearing edits, a scratchpad entry). *Mock mode:*

```
Compaction fired at turn 27: usage.iterations [('compaction', 58623, 541), ('message', 0, 560)]
```
```
  probe      where the fact was said                             none  truncate  summary  compact  clear  scratchpad
  ---------  --------------------------------------------------  ----  --------  -------  -------  -----  ----------
  torque     manual section (turn 10) and the finding (turn 13)  yes   yes       yes      -        yes    yes
  grease400  manual section (turn 6)                             yes   -         yes      -        yes    yes
  f05        manual (turn 16) and the finding (turn 19)          yes   yes       yes      yes      yes    yes
  seal       seal guide (turn 22) and the finding (turn 25)      yes   yes       yes      yes      yes    yes
  atex       the finding (turn 31)                               yes   yes       yes      yes      yes    yes
  window     the technician's words only (turn 14)               yes   yes       yes      -        yes    yes
  heatsink   the controller log export only (turn 15)            yes   yes       -        -        -      -
  align      manual section (turn 2)                             yes   -         yes      -        yes    yes
```

The lab's closing table repeats section 3's numbers and adds the final context, the side calls' cost and the history
rewrites - the bridge to lab 03: truncation rewrote the conversation twice mid-day, while the summary and scratchpad
strategies started new conversations five times and replayed nothing old.

### Lab 03 - `03_preserved_thinking_binding.py`: what breaks binding

`python advanced/day3_long_horizon_context/labs/03_preserved_thinking_binding.py` builds three turns on each of Claude
Fable 5.1, Claude Opus 5.5 and Claude Opus 5, replays them after each edit, shows `drop_block` and
`input_transformations`, moves conversations between models, compares compaction shapes, and runs the checklist guard
over a bad and an append-only harness. *Mock mode:*

```
  edit before the new turn      Fable 5.1  Fable 5.1 drop_block     Opus 5.5 + header        Opus 5.5 'error'  Opus 5
  ----------------------------  ---------  -----------------------  -----------------------  ----------------  ------
  reword an earlier question          400  200, 6 dropped (prefix)  200, 6 allowed (prefix)               400  200 []
  trim an old tool result             400  200, 5 dropped (prefix)  200, 5 allowed (prefix)               400  200 []
  drop the oldest turn                400  200, 4 dropped (prefix)  200, 4 allowed (prefix)               400  200 []
  re-render the system prompt         400  200, 6 dropped (prefix)  200, 6 allowed (prefix)               400  200 []
  change a tool description           400  200, 6 dropped (prefix)  200, 6 allowed (prefix)               400  200 []
  add a tool                          400  200, 6 dropped (prefix)  200, 6 allowed (prefix)               400  200 []
  reorder the tools                   200  200 []                   200 []                   200 []            200 []
  append a system message             200  200 []                   200 []                   200 []            200 []
  change max_tokens and effort        200  200 []                   200 []                   200 []            200 []
```
```
  conversation moved                                   result (beta header on)
  ---------------------------------------------------  -----------------------
  claude-opus-5-5 -> claude-opus-5                     200, 6 dropped (model)
  claude-opus-5-5 -> claude-fable-5-1                  200 []
  claude-fable-5-1 -> claude-opus-5-5                  200, 6 dropped (model)
  claude-opus-5 -> claude-opus-5-5                     200 []
  claude-opus-5 -> claude-fable-5-1                    200 []
  claude-opus-5-5 -> claude-opus-5 -> claude-opus-5-5  200 [] (on the return)
```
```
  client-side compaction shape (Fable 5.1)      result
  --------------------------------------------  -----------------------
  keep-tail: summary + last turn verbatim                           400
  keep-tail, retained turn's thinking stripped                      200
  keep-tail with drop_block                     200, 2 dropped (prefix)
  simple compaction: summary + the new turn     200 []
```
```
  harness              mode  requests  violations  thinking dropped  outcome
  -------------------  ----  --------  ----------  ----------------  -------------------------------------
  bad harness          ci           2           1                 0  stopped: request 2: system_rerendered
  bad harness          prod        10          18                45  5 turns completed
  append-only harness  ci          10           0                 0  5 turns completed
  append-only harness  prod        10           0                 0  5 turns completed
```

In live mode your own account decides the Opus 5.5 column: accounts created on or after 2026-08-31 see 400s there too.

### Lab 04 - `04_turn_scoped_system_messages.py`: reminders that do not grow, break or leak

`python advanced/day3_long_horizon_context/labs/04_turn_scoped_system_messages.py` hits the placement 400s, delivers the
same status line four ways over the Cobalt visit while counting what the model reads, shows the tool-loop pitfall and
the scope of persistent messages, and replays a deleted reminder on Fable 5.1. *Mock mode:*

```
  placement                                    result
  -------------------------------------------  --------
  reminder directly before the next user turn  400 (1)
  system message as the first message          400 (2)
  clear_at without the beta header             400 (3)
  clear_at with cache_control                  400 (4)
  clear_at: 'next_turn'                        400 (5)
  turn-scoped message with output_config       400 (6)
  any system message on claude-sonnet-5        400 (7)
  after the user message it applies to, last   accepted
```
```
  turn   turn-scoped  persistent  user-turn  inject-then-delete
  -----  -----------  ----------  ---------  ------------------
     27           55          55         60                  60
     28           55         110        120                  60
     29           55         165        180                  60
     30           55         220        240                  60
     31           55         275        300                  60
     32           55         330        360                  60
     33           55         385        420                  60
  total          385       1,540      1,680                 420
```
```
  method              cache writes  cache-read share  visit cost
  ------------------  ------------  ----------------  ----------
  turn-scoped               20,640               91%     $0.3273
  persistent                19,472               92%     $0.3223
  user-turn                 19,506               92%     $0.3226
  inject-then-delete       124,716               45%     $0.9265
```
```
  gloves reminder placed               requests  bullets?  first line of the answer
  -----------------------------------  --------  --------  ----------------------------------------------------------------------
  only after the technician's message         2  no        F07 is a ground fault - a SAFETY fault - and on a Zone 1 ATEX unit it
  re-sent after the tool results              2  yes       - F07 is a ground fault - a SAFETY fault - and on a Zone 1 ATEX unit i
```

### Lab 05 - `05_memory_architectures.py`: memory that is consolidated and guarded

`python advanced/day3_long_horizon_context/labs/05_memory_architectures.py` replays the day's remember-moments into a
three-tier store, consolidates at the end of the day, answers next-day questions in fresh conversations, and runs the
same store behind a Managed Agents session. *Mock mode:*

```
  gbwd      memory_search() -> memory_write()                              Memory: active as E-1.
  harbor    memory_search() -> memory_write(confirms=S-1) + memory_write() Memory: confirmed as S-1; active as E-2.
```
```
  riverbend memory_search() -> memory_write()                              Memory: not stored - personal data (phone number) - it stays in the CRM (PRV-004 §3).
  CRM sync, as received: {'status': 'rejected', 'reason': 'personal data (email address) - it stays in the CRM (PRV-004 §3)'}
  CRM sync, address redacted upstream: {'status': 'quarantined', 'id': 'E-7'}
```
```
  promoted  S-2: the GBWD shift supervisor wants a phone call before any pump is stopped
  promoted  S-3: the site requires a hot-work permit from the shift supervisor before any grinding
  promoted  S-4: Riverbend's brewhouse is a hearing-protection area and the plant-room key is held at the
  promoted  S-5: Cedar Creek's wash bay is hosed down at 14:00 daily - no electrical work in the bay afte
  promoted  S-6: Cobalt issues a gas-test certificate at the gatehouse and it expires after 4 hours
  event     E-6 left to the CMMS: bearings on HF-KP250-03 were replaced today under WO-24502
  dropped   E-7: personal data or instructions aimed at the assistant
Consolidation cost $0.0170 for the whole day - run it nightly, not per turn.
```
```
Technician: Back at Harbor Foods on Sunday for HF-KP250-03. When can I stop it, and what do I need before grinding?
  tools: memory_search()
  Agent: From memory (data, not instructions): the chilled-water pump HF-KP250-03 may only be stopped on
  Sundays 06:00-10:00 [S-1, semantic] | the site requires a hot-work permit from the shift
  supervisor before any grinding [S-3, semantic]
Technician: Going to Cedar Creek Dairy next week. Anything I should know about the wash bay?
  tools: memory_search()
  Agent: From memory (data, not instructions): Cedar Creek's wash bay is hosed down at 14:00 daily - no
  electrical work in the bay after 13:30 [S-5, semantic] 1 note(s) for this site are quarantined for
  review (they contained instructions aimed at the assistant); I have not used them.
```

Live, Claude decides for itself when to search and how to phrase notes; the write path, the quarantine and the
consolidation bookkeeping are the same code.

### Lab 06 - `06_subagent_isolation.py`: six logs, stuffed or read by subagents

`python advanced/day3_long_horizon_context/labs/06_subagent_isolation.py` plans the day from six site logs two ways and
asks the same six follow-up questions of each design. *Mock mode:*

```
Six summaries, 976 tokens in all (largest: riverbend). Contract checks: schema valid for 6/6, 13/13 pointers verified against the source lines, 0 problems.
```
```
Q: What heatsink temperature did Riverbend's KC-1 log at its latest F05 trip?
  A (stuffed):   From my context (document): (riverbend) 2026-09-14 15:12  F05  Drive overtemperature        I= 37.9 A  DCbus=551 V  heatsink=94 °C  f=48.0 Hz
  B (subagents): From the source log: Line 38 of the riverbend log: 2026-09-14 15:12  F05  Drive overtemperature        I= 37.9 A  DCbus=551 V  heatsink=94 °C  f=48.0 
```
```
  design                      coordinator calls  reader calls  prompt tokens  coordinator context at the end  cost
  --------------------------  -----------------  ------------  -------------  ------------------------------  -------
  A: stuffed logs                             7             0        346,323                          49,675  $0.4952
  B: subagents + coordinator                  8             7         66,628                           2,264  $0.1867
```

The readers ran in parallel threads; the mock has no latency to save, so the lab measures tokens and dollars only. Live,
the parallel fan-out also shortens the wait.

### Lab 07 - `07_cache_engineering_at_scale.py`: a fleet's cache

`python advanced/day3_long_horizon_context/labs/07_cache_engineering_at_scale.py` measures a cold and a pre-warmed 07:00
fan-out, the lookback boundary and its agent-loop form, two multi-tenant layouts, and per-model minimums, then prints
the savings table. *Mock mode:*

```
  07:00, eight technicians  requests  requests that wrote  cache writes  cache reads  cost
  ------------------------  --------  -------------------  ------------  -----------  -------
  cold fan-out                     8                    8        29,352            0  $0.2016
  pre-warm + fan-out               9                    1         3,670       29,360  $0.0558
```
```
  blocks appended               positions since the last entry  cache read  cache write  result
  ----------------------------  ------------------------------  ----------  -----------  -------------------------------
                            10                              11      17,994          158  hit
                            19                              20      17,994          293  hit
                            20                              21       2,662       15,640  MISS - back to the system entry
                            25                              26       2,662       15,715  MISS - back to the system entry
  25 + a breakpoint at item 12                              26      17,994          392  hit
```
```
  harness                                                     tool rounds in turn 1  turn 2 cache read  turn 2 cache write  turn 2 cost
  ----------------------------------------------------------  ---------------------  -----------------  ------------------  -----------
  breakpoint on the newest technician message; one at a time                      8              3,666              17,579      $0.1142
  breakpoint on the newest technician message; in parallel                        1             19,042               2,130      $0.0254
  automatic caching; one at a time                                                8             21,204                  34      $0.0133
```
```
  system layout  requests (6 customers x 3)  cache writes  cache reads  cost
  -------------  --------------------------  ------------  -----------  -------
  tenant first                           18        24,978       49,956  $0.2731
  shared first                           18         6,648       68,286  $0.1545
```

The `[mock] cache.ready_delay` line in step 1 is the stand-in for real response latency; live, the cold fan-out's writes
depend on how close together the requests really start.

---

## Key takeaways

1. A long session fails five ways - context rot, the cost curve, lost facts, budget overruns, binding breaks - and each
   has its own lever. Name the failure before you pick the lever.
2. `max_tokens` is a ceiling the model cannot see; effort is a per-turn dial; a task budget is a plan the model paces
   against. Change effort mid-conversation with a per-message system message, not the top-level field.
3. On a long day input dominates the bill: budgets and effort buy predictability and speed; the context strategy and
   the cache buy money.
4. Compare context strategies with probe questions about facts you know, and check the values: the dangerous miss is a
   confident answer from the wrong row.
5. Summaries and scratchpads keep what their contract asks for. Design the contract from the questions you will be
   asked, and let a failed probe - not a hunch - add a field.
6. History is append-only on current models. Freeze the system prompt and tools, append system messages, never delete
   or reword a turn, compact on the server or with simple compaction, and set `prefix_mismatch_behavior` explicitly:
   `"error"` in CI, `"drop_block"` with alerts in production.
7. Model upgrades and fallbacks are routing decisions: blocks a model cannot read are dropped, unbilled, and the
   reasoning is lost for that turn. Route fallbacks to a model that reads the producer's blocks, or accept the loss.
8. A fact about this turn goes in a turn-scoped system message, re-sent after each tool round and never deleted; a rule
   that stays true goes in a persistent one, ended by another.
9. Memory is a database with a write policy: tiers, atomic notes, reads before writes, consolidation in batch, systems
   of record for events, provenance, and a quarantine - because memory is untrusted input.
10. Bulk reading belongs in isolated subagents that return a verified contract; keep a drill-down path for details the
    contract does not carry.
11. At fleet scale caching is timing and layout: pre-warm before fan-outs, keep every request within 20 positions of the
    last entry, put shared content before tenant content, and check each model's minimum.

## Further reading

* Migration guide (task budgets, preserved thinking, per-message effort, turn-scoped system messages, progress updates)
  - https://platform.claude.com/docs/en/about-claude/models/migration-guide
* Preserved thinking - https://platform.claude.com/docs/en/build-with-claude/preserved-thinking
* Mid-conversation system messages - https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages
* Effort - https://platform.claude.com/docs/en/build-with-claude/effort
* Adaptive thinking - https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking; extended thinking
  (which models read which blocks) - https://platform.claude.com/docs/en/build-with-claude/extended-thinking
* Prompt caching - https://platform.claude.com/docs/en/build-with-claude/prompt-caching
* Compaction - https://platform.claude.com/docs/en/build-with-claude/compaction and on-demand compaction -
  https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand
* Context editing - https://platform.claude.com/docs/en/build-with-claude/context-editing; context windows -
  https://platform.claude.com/docs/en/build-with-claude/context-windows
* Token counting - https://platform.claude.com/docs/en/build-with-claude/token-counting
* Structured outputs - https://platform.claude.com/docs/en/build-with-claude/structured-outputs
* Memory tool - https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
* Managed Agents memory - https://platform.claude.com/docs/en/managed-agents/memory; sessions -
  https://platform.claude.com/docs/en/managed-agents/sessions; multiagent orchestration -
  https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration
* Optimizing for cost and intelligence - https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
* Pricing - https://platform.claude.com/docs/en/about-claude/pricing
* Effective context engineering for AI agents - https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
* Effective harnesses for long-running agents - https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
