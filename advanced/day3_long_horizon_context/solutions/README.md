# Day 3 solutions - worked answers

Runnable solutions: `ex04_07_calculations.py` (exercises 4-7; arithmetic only, no API calls), `ex10_prefix_diff.py`,
`ex11_summary_contract.py` and `ex12_cache_scheduler.py` (each loads its starter from `../exercises/`, plugs the
solution in and runs the starter's bench). Numbers below are those scripts' output. The calculations are exact; the API
numbers come from mock mode (rule-based stand-ins behind the real SDK paths), so the shapes carry over to live runs and
the values do not.

---

## 1. Which failure, which lever?

The rule behind every answer: name the failure by where the symptom comes from, not by what it looks like. A wrong
answer late in the day can be context rot (the fact is there, drowned in stale bulk) or lost facts (the fact is gone),
and the two have opposite fixes - removing bulk cures the first and can cause the second (lab 02).

| | failure | primary lever | second lever | would *not* do |
|---|---|---|---|---|
| a. KP-400 answered from the KP-250 table at 60K tokens | context rot: the morning's exports still fill the context and the model reaches for the wrong product's table | take the bulk out: a scratchpad reset per site, or clearing old tool results (`clear_tool_uses_20250919`, `log_finding` excluded) | reader subagents, so raw exports never enter the conversation (lab 06); a probe that checks the grease *value* | a sliding window: in lab 02 truncation produced exactly this answer - it dropped the KP-400 section and kept the KP-250 row |
| b. bill tripled, output flat | the cost curve: every request re-sends a longer history | a smaller context at natural boundaries (lab 02: scratchpad $1.39 against $2.23 for the same day) | cache engineering: check that `cache_read_input_tokens` dominates, automatic caching through tool loops, a keep-alive over the drives between sites | cut `max_tokens` or effort - output is flat and is not where a long session's money goes (lab 01: 40% less output saved $0.18 of $2.23) |
| c. report leaves out the first site | lost facts: a window, a summary or a compaction dropped the morning | a contract that carries findings forward verbatim (the scratchpad's `findings`), or a report built from the harness's own `log_finding` records | a "report covers 6/6 sites" check on a recorded day in CI (lab 02 prints it per strategy) | lower the compaction trigger, or trust a summary nobody probed (lab 02: compaction 4/6 sites) |
| d. minutes-long turns; 1,400 thinking tokens on "log it" | budget overrun: one effort level for every kind of turn and no pacing | per-message effort by turn kind - `low` for log and remember, `high` for telemetry (beta `mid-conversation-output-config-2026-07-01`) | a task budget per technician-day; progress updates on the long turns someone watches | lower `max_tokens` to cap thinking - it cuts answers mid-sentence (lab 01: three, the report among them), and a truncated `tool_use` must never run |
| e. Opus 5.5 with `drop_block`: dropped thinking on most turns | binding break: the harness edits history (clock in the system prompt, deleted reminders) | an append-only history: frozen system prompt and tools, turn-scoped system messages, replay of the stored wire JSON | CI with `prefix_mismatch_behavior: "error"` and lab 03's guard to name the edit; alerts split by `reason` - `model_binding_mismatch` points at routing, not editing | switch production to `"error"` (a silent degradation becomes an outage), or strip thinking blocks yourself |
| f. three bullets long after the plant floor | a per-turn instruction delivered as persistent context keeps applying - context rot in its instruction form | a turn-scoped system message (`clear_at: "next_user_message"`, beta `mid-conversation-system-clear-at-2026-08-21`), re-sent after each tool round while it applies | for a rule that spans a visit, a persistent system message on arrival and an appended one on departure that ends it | delete the old reminder from history: a history edit - cache restart, a 400 on Fable 5.1, dropped thinking on Opus 5.5 |
| g. eight 07:00 requests each write 6,000 tokens | the cost curve at fleet scale: an entry is readable only once its writer responds, so concurrent requests all write | one `max_tokens: 0` pre-warm per shared prefix, then the fan-out (lab 07, exercise 12) | a shared-first prefix layout, so depots and customers share the reference card | the 1-hour TTL or more breakpoints: neither changes concurrency, and eight writes at 2x cost more |
| h. Monday's next-visit notes gone on Tuesday | lost facts across sessions: the notes lived in a conversation, which is working memory | an episodic tier outside the context, written through a guarded tool and read at the start of each visit (lab 05) | nightly consolidation into per-site facts; a recall check on a recorded day | keep one conversation open for days, or render notes into the system prompt (cache and binding again) |

Tempting wrong answers: (a) "compact it" - a summary answers only from what it kept, and lab 02's compaction kept
less than anything else; (e) "Opus 5.5 is worse at this task" - it is re-planning every turn because the reasoning it
wrote was dropped; (f) "the model ignores instructions" - it follows one too well, because it is still in its context.

## 2. What will the API do?

Two checks, in this order. The **model check**: a block the receiving model cannot read is dropped - a 200, never a
400, whatever `prefix_mismatch_behavior` says. The **prefix check** applies only to blocks whose producer records a
prefix (Fable 5.1, Opus 5.5 - not Opus 5), read by a model that checks. Fable 5.1 enforces it (a 400 unless
`drop_block`); Opus 5.5 on an older account records it (with the header and the field unset, the block is allowed and
listed; with the field set to either value, enforced). A block is affected only if the edit comes before it, and
`drop_block` drops the first mismatched block and every thinking block after it.

| | result | `input_transformations` | why |
|---|---|---|---|
| a | 400 at `messages.3.content.0` | - (no header) | `messages.1` was produced before `messages[2]` existed, so its prefix (system, tools, `messages[0]`) is intact; `messages.3` was minted after the injected text |
| b | 200 | `thinking_dropped`, `prefix_binding_mismatch` at `messages.3.content.0` and `messages.5.content.0` | the first mismatch and everything after it; `messages.1` is kept |
| c | 200 | `thinking_mismatch_allowed` x3 (`.1`, `.3`, `.5`) | the system prompt precedes every block; recorded, not enforced, because the field is unset - the blocks still reach the model |
| d | 200 | `thinking_dropped`, `model_binding_mismatch` x3 | Opus 5 cannot read Opus 5.5 blocks; the model check runs first, so `drop_block` plays no part |
| e | 200 | `[]` | Opus 5 blocks carry no conversation binding and Fable 5.1 reads them; the edit costs a cache restart, nothing else |
| f | 200 | `[]` | append-only: the cleared message is still in the transcript byte for byte (it just renders nothing); the new one is appended |
| g | 200 | `[]` | the tool set is compared as a set, and `max_tokens` is not part of the prefix |
| h | 400 at the retained turn's thinking block | - (no header) | keep-tail: that block was minted with the full history in front of it. Strip the retained turns' thinking, send `drop_block`, use simple compaction, or on-demand compaction (beta `compact-2026-09-04`), which is designed to keep retained turns' blocks valid |

Lab 03 runs each of these shapes (the edits, `drop_block`, the model switches, keep-tail compaction), lab 04 the
turn-scoped case, and exercise 10's bench a-d on six-block conversations. Tempting wrong answers: (a) "the edit is in
a user message, so the thinking is unaffected" - the prefix is every earlier message, whoever wrote it; (d) "`drop_block`
reports prefix drops" - no prefix is compared when the model cannot read the block at all; (e) "any edit before a
thinking block breaks it on Fable 5.1" - only blocks that recorded a prefix can mismatch one.

## 3. Ceilings, dials and plans

a. **What the model sees.** Of `max_tokens`, nothing: the server applies it, and the model finds out only by being cut
   off (`stop_reason: "max_tokens"`), so it cannot plan around it. Of effort, the level: it shapes how much the model
   thinks and writes and how many tool calls it makes, with no number attached. Of a task budget, a countdown - how
   much of the task's tokens is left, updated as the loop goes on - so it can pace itself and wrap up.
b. **What a task budget counts**: what the model generates across the task (thinking, text, tool calls) plus the tool
   results it reads. The history you re-send is the same tokens again; counting every re-send would charge a long task
   for the transport rather than the work - lab 01's four runs of the day re-read 11,085,313 cached tokens between
   them, while the 100k arm's countable spend was 65,620 tokens. The server reads the spend off the history you send,
   which is also why it loses it when you rewrite that history.
c. **`remaining`**: pass it when the history you send no longer shows what the task has spent - after a summary reset,
   a client-side compaction or a truncation - so the countdown continues instead of starting again at `total`; the
   harness computes it from its own accounting (exercise 7). In an ordinary loop the server already counts the spend
   from the history, so a value you pass can only disagree with it - usually by lagging, and a countdown that does not
   fall is a model that never paces. The on-demand compaction docs also make it a 400 on any request that carries a
   compaction block.
d. **A budget of 15,000 tokens** is a 400: `total` must be at least 20,000 (the mock says
   `output_config.task_budget.total: Input should be greater than or equal to 20000`). A task that small needs no
   budget; effort and `max_tokens` are enough.
e. **Top-level effort per turn** invalidates the messages cache on every change (on some models the tools and system
   cache too), so a day that alternates `low` and `high` re-writes its history again and again; and the docs note, for
   Fable 5.1, that it steers less reliably - the model stays consistent with replies it wrote at the old level. A
   per-message effort change is an appended system message: the prefix is untouched, it holds until the next one, and
   the transcript records when the level changed.
f. **Progress updates** exist on the Fable models and, per the platform docs, Claude Opus 5.5 (the mock accepts them on
   Fable 5 and Fable 5.1 only - lab 01's matrix). Claude Opus 5 has no `updates` display: its between-tool narration is
   ordinary text blocks, so render those. Under `"updates"` a UI treats every thinking block with non-empty text as a
   progress note and shows it as a status line ahead of the tool call it announces; expects zero or several per
   response; treats empty thinking blocks as hidden reasoning; and passes every block back unchanged - they are billed
   at full length and belong to the signed transcript.
g. **The one-line report**: the budget was wrong, and the harness made it worse. The 64k arm had spent 63,024 of 64,000
   tokens by the report (2% left), 54,768 of them tool results - three historian exports the agent needed for ten
   minutes each. The model did what a budget asks: it paced down from Cobalt (173 and then 106 output tokens a turn,
   against 336 and 247 under the 100k budget) and answered the report with essentials. Change three things: size the
   budget from measurement with a margin (90,000, exercise 7); stop routing bulk through the budgeted loop (reader
   subagents turn 15,000-token exports into contracts of a few hundred); and have the harness warn when the spend
   passes 90% before the report, so a budget that is too small is caught by the harness, not by the billing office.

## 4. Lookback misses in a 40-turn session

```
Exercise 4 - lookback misses after sequential look-up turns (input side, Opus 5)
  normal turn 21: read 35,000 x 0.1 + write 1,500 x 1.25 = $0.0269
  missed turn 21: read 5,000 x 0.1 + write 31,500 x 1.25 = $0.1994
  extra per miss = (C(t-1) - P) x (1.25 - 0.1) x $5/MTok; at turn 21: $0.1725
  turn after a look-up  extra cost
  --------------------  ----------
                     6     $0.0431
                    11     $0.0863
                    16     $0.1294
                    21     $0.1725
                    26     $0.2156
                    31     $0.2588
  six misses               $0.9056
```

a. Normally turn 21 reads C(20) = 5,000 + 20 x 1,500 = 35,000 tokens at 0.1x and writes the new 1,500 at 1.25x:
   (3,500 + 1,875) x $5/MTok = **$0.0269**. When it misses, it reads only the 5,000-token prefix and re-writes the
   30,000 tokens of history plus the new 1,500: (500 + 39,375) x $5/MTok = **$0.1994**, 7.4 times a normal turn.
b. A miss moves the history C(t-1) - P from the read column (0.1x) to the write column (1.25x): extra = 1,500 (t-1) x
   1.15 x $5/MTok. Over t = 6, 11, ..., 31 the history terms are 5 + 10 + ... + 30 = 105 blocks of 1,500 tokens:
   105 x 1,500 x 1.15 x $5/MTok = **$0.9056**. Each miss costs more than the last, because the history grows.
c. Without misses the day reads 40 x 5,000 + 1,500 x (0 + 1 + ... + 39) = 1,370,000 tokens at 0.1x and writes 40 x
   1,500 = 60,000 at 1.25x: (137,000 + 75,000) x $5/MTok = **$1.06** (the prefix's first write, $0.03, left out). With
   the misses, **$1.97 - six requests out of forty add 85%** to the input bill.
d. Three fixes:
   * **Automatic caching** (`cache_control` at the top level of the request): the entry moves to the end of every
     request, tool rounds included, so no hop is longer than one round. One line of code. **Ship this**, and keep the
     explicit breakpoint on the system prompt so that even a miss reads P.
   * **Parallel look-ups**: eight `tool_use` blocks in one assistant message are one position, and the eight results
     are another. Faster as well (one model round trip instead of eight), but the model decides whether to
     parallelise; you can encourage it in the tool descriptions, not guarantee it.
   * **An intermediate breakpoint** on a block inside a long turn, within the four-breakpoint limit (exercise 12).
   Automatic caching covers the agent loop; keep intermediate breakpoints for the case it cannot cover - a single
   request that appends more than 20 positions (exercise 12).

## 5. Compaction cadence from a budget

```
Exercise 5 - compaction cadence (own summary every k turns; input + summary output, Opus 5)
  largest k under W = 30,000: (W - (P + S)) / D = (30,000 - 5,600) / 1,600 = 15.25 -> k = 15
  compact every k turns  summaries  peak context  under 30,000?  cost per day
  ---------------------  ---------  ------------  -------------  ------------
                      3         13        10,400  yes                 $0.8494
                      5          7        13,600  yes                 $0.7509
                     10          3        21,600  yes                 $0.7405
                     15          2        29,600  yes                 $0.7777
  never                          0        69,000  no                    $1.12
  cheapest k from 1 to 20: k = 8 at $0.7319; k = 6-10 are all within 2% of it - a flat bottom, so let quality and natural boundaries choose inside it
```

a. A conversation that starts from the summary peaks at P + S + kD tokens, so k <= (30,000 - 5,600) / 1,600 = 15.25:
   **k = 15** (peak 29,600). The day's first conversation starts from P alone and peaks lower.
b. Worked for k = 10 (three summaries, after turns 10, 20 and 30), in tokens at the input price:
   * turns 1-10 from P: reads 10 x 5,000 + 1,600 x (0 + ... + 9) = 122,000 at 0.1x = 12,200; writes 10 x 1,600 at
     1.25x = 20,000; together 32,200;
   * each later conversation (turns 11-20, 21-30, 31-40): its first request reads P (500) and writes S + D = 2,200 at
     1.25x (2,750); the other nine read 9 x 5,600 + 1,600 x (1 + ... + 9) = 122,400 at 0.1x (12,240) and write 9 x
     2,000 = 18,000; together 33,490, three times: 100,470;
   * the three summary forks read 21,000 + 21,600 + 21,600 = 64,200 at 0.1x: 6,420;
   * input (32,200 + 100,470 + 6,420) x $5/MTok = $0.6955, plus 3 x 600 summary tokens x $25/MTok = $0.045: **$0.7405**.
   Without compaction the day reads 40 x 5,000 + 1,600 x (0 + ... + 39) = 1,448,000 tokens at 0.1x and writes 40 x
   2,000: (144,800 + 80,000) x $5/MTok = **$1.12**, with a peak of 69,000 tokens that breaks the quality budget.
c. **Why the cheapest k is not the smallest.** A compaction has a price that does not shrink with k: 600 output tokens
   at $25/MTok ($0.015 - as much as reading 30,000 cached tokens), the fork's read of the context, and a reset that
   writes S + D instead of reading. Carrying history costs 0.1x per token per turn and grows with k. The day's cost is
   roughly (fixed price x T/k) + (carrying cost that grows with k); the minimum sits in between - at k = 8 here, with
   6-10 within 2%. Compacting every three turns pays for 13 summaries to save little; compacting every 15 carries
   long histories.
d. **What else decides k.** Natural boundaries first: Kestrel's day has five departures in forty turns - a reset every
   eight turns on average, inside the flat bottom, so resetting at departures is also the cheap choice. Quality: every
   compaction is a lossy rewrite, so more of them means more chances to drop a fact (the probes decide). Turn sizes are
   not uniform: a 15,000-token export turn breaks the D = 1,600 model, so keep a token trigger beside the boundary. The
   cache: a drive between sites outlasts the 5-minute TTL, so a reset at departure costs no cache that was not expiring
   anyway. Preserved thinking: a reset discards the reasoning, which at a site boundary is what you want. Latency: the
   fork is one more request, run while the technician drives.

## 6. Pre-warming and keep-alive

```
Exercise 6 - pre-warming and keep-alive
  (a) 8-way fan-out over a 6,000-token prefix: cold $0.3000 (8 writes), pre-warmed $0.0615 (1 write + 8 reads): $0.2385 per depot-morning, $298.13 per year for 5 depots x 250 days
  idle gap  keep-alive pings  5-min + keep-alive  5-min, let it expire  1-hour TTL premium
  --------  ----------------  ------------------  --------------------  ------------------
  20 min                   4             $0.0300               $0.3675             $0.2250
  30 min                   6             $0.0450               $0.3675             $0.2250
  45 min                   9             $0.0675               $0.3675             $0.2250
  70 min                  15             $0.1125               $0.3675             $0.5925
  (c) Claude Opus 5: a ping costs $0.0150, the 1-hour premium $0.1125; 7 pings cost no more than the premium, so the keep-alive wins for gaps up to about 36 minutes, the 1-hour TTL from there to 60
  (c) Claude Fable 5.1: a ping costs $0.0075, the 1-hour premium $0.2250; 30 pings cost no more than the premium, so the keep-alive wins at every gap the 1-hour TTL can bridge (up to 60 minutes)
```

a. Cold: eight concurrent requests all write, 8 x 6,000 x 1.25 = 60,000 input-price tokens = **$0.3000**. Pre-warmed:
   one write and eight reads, 6,000 x 1.25 + 8 x 6,000 x 0.1 = 12,300 = **$0.0615**. The difference, $0.2385 per
   depot-morning, is **$298.13 a year** for five depots. Small money - the other reasons to pre-warm are the
   technicians' first answers (each reads instead of processing 6,000 tokens) and a predictable morning. Put the
   pre-warm's breakpoint on the shared prefix, not on its placeholder message, and send the real requests' thinking and
   effort settings.
b. On Fable 5.1 the 30,000-token conversation costs $0.0075 to read (0.025x) and $0.3675 to re-write (1.25x instead of
   a read). (i) A `max_tokens: 0` ping every 4.5 minutes bridges a gap of g minutes with ceil(g / 4.5) - 1 pings: 4, 6,
   9 and 15 for the four gaps, $0.03-$0.11. (ii) Letting the entry expire costs one re-write, $0.3675, whatever the
   gap. (iii) The 1-hour TTL costs the premium of writing at 2x instead of 1.25x, $0.2250, and past an hour it expires
   as well - the 70-minute row pays the premium and the re-write.
c. **On Fable 5.1, the keep-alive** at every gap in the table - pings are nearly free when reads cost 0.025x - with the
   5-minute TTL, and stop pinging when the technician closes the day. The answer changes when:
   * reads are dearer: on Opus 5 (0.1x) the keep-alive wins only up to about 36 minutes, and the 1-hour TTL wins for
     gaps of 36-60 minutes;
   * the context is small: after a scratchpad reset at departure the idle conversation holds a few thousand tokens, and
     letting it expire costs cents - do not build a scheduler for that;
   * the return is uncertain: a ping for a conversation that never resumes is wasted, so ping only while the
     technician is on shift;
   * the operations do not fit: a keep-alive needs a timer per conversation, cancelled when the technician resumes;
     it must repeat the exact prefix (model, system, tools, thinking and effort settings) or it warms a different
     entry; and pings count against rate limits.

## 7. Sizing a task budget

```
Exercise 7 - sizing a task budget (lab 01's measured day)
  measured spend: 10,852 output + 54,768 tool results = 65,620 tokens
  budget with a 30% margin: 65,620 x 1.3 = 85,306 -> set 90,000 (a round number above it)
  after site  spent at the site  spent so far  remaining to pass after the reset
  ----------  -----------------  ------------  ---------------------------------
  gbwd                   19,169        19,169                             70,831
  harbor                 18,188        37,357                             52,643
  riverbend               3,268        40,625                             49,375
  cedar                   2,879        43,504                             46,496
  cobalt                 19,037        62,541                             27,459
  westfield               3,079        65,620                             24,380
```

a. **90,000 tokens.** The measured day spent 65,620 (output plus tool results - what the budget counts); a 30% margin
   covers a day with a fourth export or a longer diagnosis. The costs are asymmetric: a budget that is too high costs
   nothing unless the model uses it (it is advisory), one that is too low degrades the work (part c). One day is a
   thin sample - once a few weeks of recorded days exist, set it at the 95th percentile of daily spend plus a margin.
b. The harness keeps its own count - `usage.output_tokens` of every response plus the tokens of every tool result it
   sent - and, with the new conversation after each departure, passes `remaining = 90,000 - spent so far`: 70,831
   after GBWD, 52,643 after Harbor, 49,375, 46,496 and 27,459. There is no reset after Westfield; its row is what is
   left for the report, 24,380. Without `remaining` each new conversation would show a fresh 90,000 and the model
   would pace every site as if it were the first.
c. **64,000**: lab 01's tight arm - the countdown was nearly spent at Westfield (63,024 of 64,000), output per turn
   fell from Cobalt on, and the report came back as one line. **20,000** (the minimum): GBWD alone spent 19,169, so
   the budget would be gone during the first visit and the model would work in wrap-up mode for the rest of the day,
   report included. An advisory budget never stops the day; it degrades all of it.
d. **Per day or per visit.** For a day: it is the unit the service manager plans and bills in; the model can spend
   more on a hard diagnosis and less on "noted"; one number. Against: early overspend is paid for by the late sites and
   the report (the 64k arm), and the harness must carry `remaining` across every reset. For a visit: it matches the
   harness's reset boundaries (no `remaining` bookkeeping), isolates a bad site from the rest of the day, and the report
   can be a task of its own. Against: visits vary six-fold (2,879 to 19,169 tokens), so a per-visit budget needs a size
   per visit type (monitored site with an export or not - the site brief tells you), and the daily total is less
   predictable. Kestrel budgets per technician-day, because that is the unit the manager plans in and the harness
   already counts spend; a per-visit budget would be the better choice for a harness without that accounting.

## 8. Design - the upgrade plan

**Do the harness work first, on Claude Opus 5.** Every fix below is model-independent, so the upgrade itself becomes a
one-line change measured against a clean baseline. The case study's week happened because the order was reversed.

**Capture and diff.** Record the wire JSON of every request for a week of real days. Run exercise 10's `divergence()`
over consecutive requests of each conversation and list every non-append-only change with its location: v1 will show
the system prompt changing on every request (the clock) and `messages[k]` changing wherever a reminder was deleted.
Replay the recorded days against Claude Opus 5.5 and Claude Fable 5.1 with the `thinking-binding-controls-2026-08-01`
header and `"error"` - the number of 400s is the harness's binding debt (lab 03's v1-like harness: 18 violations and 45
dropped blocks in ten requests). Keep Opus 5's numbers as the baseline: output tokens and latency per turn kind, cost
per technician-day, probe score and report completeness.

**The fixes, in order:**

| # | fix | replaces in v1 | done when |
|---|---|---|---|
| 1 | freeze the system prompt; clock and site as a turn-scoped system message after each user message and each tool round (beta `mid-conversation-system-clear-at-2026-08-21`) | the clock re-rendered into `system` | the guard reports no `system_rerendered` |
| 2 | per-turn reminders as turn-scoped system messages, left in place | inject-then-delete | no `blocks_modified`; lab 04's visit writes 20,640 cache tokens instead of 124,716 |
| 3 | replay the stored wire JSON, append `response.content` verbatim, freeze tool definitions, change tools with `tool_addition` / `tool_removal` | messages rebuilt from objects | the replayed days diff append-only |
| 4 | a scratchpad reset at each departure plus probes in CI; server compaction kept as a backstop with a trigger above the steady state | compaction at 50,000 as the only control | probes pass on recorded days (exercise 11's contract) |
| 5 | `max_tokens` 16,000 as the ceiling, a task budget per technician-day, per-message effort by turn kind, the top-level effort set explicitly | `max_tokens: 1024` as the cost control | no `stop_reason: "max_tokens"` in a day; complete reports |
| 6 | the Opus 5.5 specifics: no `thinking: {"type": "disabled"}` (use effort `low`); no forced `tool_choice` (structured outputs - the scratchpad already uses `output_config.format`); `display: "updates"` and a UI that renders non-empty thinking blocks, because Opus 5.5 returns its between-tool notes as thinking blocks | - | the recorded days replay on Opus 5.5 without a 400, and the phone shows status lines |

Fix 5 matters for the upgrade too: Claude Opus 5.5 defaults to effort `medium` where Claude Opus 5 defaults to `high`,
so a route that never set effort changes behaviour silently.

**CI and production settings.** Every request - the main loop, summary and scratchpad forks, probes, the report,
retries, batch jobs - sends the header and an explicit `prefix_mismatch_behavior`. CI: `"error"`, with lab 03's guard
in front of the client so a failing build names the rule. Production: `"drop_block"`, an alert on any
`prefix_binding_mismatch` entry (after the fixes there should be none - each one is a bug with a location), and a
dashboard of `model_binding_mismatch` entries, which should appear only on fallback turns.

**Fallback routing under model binding.** Claude Opus 5 cannot read Opus 5.5 blocks, so an overload fallback to Opus 5
runs those turns without the reasoning: a 200, the drops unbilled, and a model that re-plans from scratch. For the field
agent: retry Opus 5.5 with backoff first (overloads are short), then fall back to Fable 5.1, which reads Opus 5.5 blocks
on the Claude API - dearer per token, but only for the minutes of an overload - and keep Opus 5 as the last resort with
the drop accepted. For the supervisors' route on Fable 5.1 no other model reads its blocks: retry Fable 5.1, then fall
back to Opus 5.5 and accept the drop for those turns (back on Fable 5.1 nothing more is lost, because Fable 5.1 reads
its own blocks and Opus 5.5's), or start the fallback model from the scratchpad, a clean reset, rather than from a
transcript it cannot read. Never strip blocks on a switch. And remember that a downgrade hides an edit: the model check
runs first, so fallback turns must not count as "clean" in the binding dashboard.

**Monitor**: `input_transformations` by type and reason; 400s by message; output tokens and latency per turn kind
(re-planning shows up there first - the case study's week); the cache read share; probe score and report completeness
on recorded days; cost per technician-day (Opus 5.5 is 20% cheaper per token, so a flat bill means more tokens); the
fallback rate and where it went.

**Rollout**: shadow replay of recorded days on both models; a canary of one depot's technicians, new conversations
only; widen depot by depot with rollback criteria (any prefix drop, 400s, output per turn or latency above the
baseline by an agreed margin, a probe regression). Switch models at conversation boundaries - the 07:00 start -
never mid-day: rolling a live conversation back from Opus 5.5 to Opus 5 is exactly the drop described above. Keep the
Opus 5 route deployable until the rollout is done.

**Risks for accounts created on or after 2026-08-31.** There the prefix check is enforced by default, so any path that
edits history without an explicit `drop_block` is a 400 rather than a silent drop: forks and probes built from a
modified copy of the history, retries that rebuild messages, support tools that "repair" a transcript, batch jobs that
forgot the header. Kestrel's own older account hides these unless CI runs with `"error"` - which is why CI sets the
field instead of relying on the account. Client-side keep-tail or background compaction added later would fail the
same way. Model binding is the same for every account, so the fallback drops remain. And platform coverage differs:
that Fable 5.1 reads Opus 5.5's blocks is documented for the Claude API only, and on-demand compaction is not offered
on Amazon Bedrock.

**Rejected alternatives.** "Upgrade first and fix whatever errors appear" - on an older account nothing errors; that
was the case study's week of silent re-planning. "Strip thinking blocks from history to avoid binding" - every turn
re-plans, and round trips lose what the API would have kept. "`drop_block` in CI too" - the build passes while the
harness keeps editing, and the damage shows up only as cost and latency. "Keep Opus 5 as the first fallback because it
is already provisioned" - it cannot read the reasoning it is asked to continue.

## 9. Design - memory for 60 technicians and 400 sites

**Tiers and keys.**

| tier | key | holds | written by | read by |
|---|---|---|---|---|
| working | technician, session, site | the visit's scratchpad | the harness, from the model's structured output at departure | this session |
| episodic | site (plus customer, unit, work order); author and source on every row | dated notes with provenance - "2026-09-15, harbor: hot-work permit before grinding" | the agent through a guarded `memory_write`; integrations with their own `source` | the consolidation job; the agent, for recent notes of its site |
| semantic | site (unit for unit facts, customer for rules that span a customer's sites) | consolidated facts: sources, confirmation count, first seen, last confirmed, status (active, disputed, stale) | the consolidation job only | the agent, in the site brief at arrival and on search |
| technician | technician | working preferences (units, language, level of detail) - nothing about customers | the technician, in settings | that technician's sessions |

Not memory: the CMMS (work orders, parts, asset history) and the CRM (people and contacts) stay the systems of record.
Memory links to them by id and never copies them - lab 05 left the bearing replacement under WO-24502 to the CMMS.

**Write policies.** The agent proposes and code decides. The agent searches before it writes, and a fact that is
already there is confirmed (count and date), not duplicated. Notes are atomic: lab 05 split the compound Harbor note,
because confirming it whole would have swallowed the new permit fact. Every note needs a site; personal data (phone
numbers, e-mail addresses, people's names) is rejected at the write - store the role, "the shift supervisor", not the
person; instruction-like text is quarantined. Integrations write episodic notes that start quarantined and are never
promoted automatically. The agent never edits or deletes; jobs and reviewers do.

**Consolidation and conflicts.** Nightly, per site with new episodes, through Message Batches (nothing waits for it,
and batches cost half) with a JSON schema for facts, confirmations, conflicts and dropped notes; code validates,
deduplicates and records sources. Sixty technicians writing five to seven notes a day is 300-400 episodes a night; at
lab 05's $0.0170 per technician-day, about $1 a night and $255 a year before the batch discount - the money is in the
design, not in the model calls. A weekly job marks facts not confirmed for twelve months as stale, and the agent asks
the next technician on site to confirm them. **When two technicians disagree about a stop window**, the consolidator
returns a conflict, not a winner: both claims with dates, authors and sources. Code marks the existing fact
`disputed` (still readable), opens a review task for the site's service coordinator, and the agent shows both - "two
notes disagree; confirm with the shift supervisor before stopping the pump". Safety and access facts (stop windows,
permits, hazardous areas) are resolved only by a person with a source - the customer's permit rules or a work
instruction; for low-risk facts (where the plant-room key is kept) the newer confirmed note wins, and the old one
stays in the history.

**Contamination controls.** The lab 05 guards on the write path: personal data rejected, instructions aimed at the
assistant quarantined, customer-supplied notes never promoted without review. On the read path, notes are rendered as
data with their ids and provenance inside a delimited block, never as instructions, and the agent says when it
withheld a quarantined note. Memory can never grant a capability - tool permissions are code (Day 5). The guards get
regression tests with attack strings, like any other security control.

**Retention and erasure.** Episodic notes: 90 days after consolidation. Quarantined notes: 30 days unless a reviewer
releases them. Semantic facts: until contradicted, until the site is retired, or unconfirmed for 24 months. Working
memory: the session. Because personal data is rejected at the write, erasure mostly concerns author ids - keep them
pseudonymous (a technician id that maps to a person in the HR system) so a leaver's notes stay usable. A customer's
offboarding deletes every tier for its sites, including derived facts, store versions and backups; a fact whose only
sources were erased is re-evaluated.

**Audit.** An append-only event for every write, confirmation, promotion, quarantine, release, deletion and render:
who (technician, job or integration), which session and request, before and after. The harness logs which memory ids
went into each request, so "why did the agent tell me Sundays 06:00-10:00?" has an answer: S-1, from the June visit,
confirmed on 2026-09-15.

**(a) The technicians' phone app: your own store** (lab 05), behind typed tools. The write policy is code you own,
the keys (site, customer) are first-class, it works on every model and fallback route, and the audit lives in your
database. Rejected: the memory tool (`memory_20250818`) - the model organises files and paths itself, which suits
personal or project notes, but site-keyed retrieval, consolidation and quarantine then have to be retrofitted in the
handler; a Managed Agents memory store - the phone app is a Messages API harness, and a `read_write` mount lets the
agent write through its file tools past your write path (the store's versions give you rollback, not validation).

**(b) The nightly Managed Agents job that prepares site briefs: a Managed Agents memory store per depot**, with a
directory per site, mounted `read_only` and refreshed one way from your store by the consolidation job. The job's
output - the briefs, and anything it wants remembered - goes back through a custom tool into your store, where the
write policy applies. The job runs in a session with file tools anyway, a read-only mount is the simplest safe way to
give it hundreds of sites' facts, and the store's versions show what the facts were on any night. Design for the
limits: at most eight stores per session (hence per depot, not per site), never credentials in a store, and the store
is a copy - your store stays the source of truth. Rejected: a `read_write` mount (a second writer that bypasses
consolidation and quarantine); a custom search tool alone (simpler, and a fine choice if the export is not worth
building, but you lose the versioned record). Managed Agents memory stores are beta (`agent-memory-2026-07-22`) and
not simulated in this course; verify against the docs before relying on the details.

## 10. Hands-on - predict binding breaks before sending

```
Exercise 10 (solution) - predict binding breaks before sending
  case                              prefix_mismatch_behavior  diverges at   predicted = API  check
  --------------------------------  ------------------------  ------------  ---------------  -----
  fable-5-1: append-only            error                     -             all kept         match
  fable-5-1: reword turn 1          drop_block                messages[0]   dropped x6       match
  fable-5-1: trim a tool result     error                     messages[2]   rejected x1      match
  fable-5-1: re-render system       (unset)                   system/tools  rejected x1      match
  fable-5-1: reorder tools          error                     -             all kept         match
  fable-5-1 -> opus-5 with an edit  error                     messages[0]   dropped x6       match
  opus-5-5: append-only             error                     -             all kept         match
  opus-5-5: reword turn 1           drop_block                messages[0]   dropped x6       match
  opus-5-5: trim a tool result      error                     messages[2]   rejected x1      match
  opus-5-5: re-render system        (unset)                   system/tools  allowed x6       match
  opus-5-5: reorder tools           error                     -             all kept         match
  opus-5-5 -> opus-5                drop_block                -             dropped x6       match
  opus-5-5 -> fable-5-1             error                     -             all kept         match
13/13 predictions match the API. Run predict() in the request path: in CI fail the build on any 'rejected' or 'dropped (prefix)'; in production log them before the API does.
```

**The implementation** (`ex10_prefix_diff.py`). `divergence()` canonicalises before it compares - `cache_control`
stripped at every level, keys sorted - because the check compares content, not cache markers. It returns 0 when the
system prompt or the tool set changed (non-deferred tools compared as a name-sorted set: order is harmless, a changed
description is not), otherwise the first message index where the two requests differ, or `None`. `predict()` walks the
thinking blocks in order: the model check first (`can_read_thinking(model, producer)`); then, only for a block whose
producer records a prefix and a reader that checks, the block is affected when the divergence is at or before its
message; the posture decides the outcome - enforced: rejected, or dropped under `drop_block`; recorded: allowed when
the field is unset, enforced when it is set; once `drop_block` drops one block, every later one goes too. The harness
has to supply the producing model per assistant turn - the signature is opaque - so record it when you append the turn.

**What the table teaches.** Reordering tools is not a divergence; re-rendering the system prompt is. The recorded
Opus 5.5 row allows six blocks with the field unset - on an account created after 2026-08-31 the same request is a 400,
as the script's `[mock]` note says, so `binding()` should take the account's posture into account, not only the model's.
A downgrade hides an edit: `fable-5-1 -> opus-5 with an edit` is six model drops and no 400, because the edit is never
prefix-judged - it resurfaces on the next Fable 5.1 request. And `opus-5-5 -> fable-5-1` keeps everything.

**In CI**, replay recorded conversations through the harness and run `predict()` on every consecutive pair of requests;
fail on any `rejected` or prefix `dropped`, printing the divergence index and the rule it broke (lab 03's guard). That
catches the edit without an API call and whatever the account's posture. **In production**, run it before sending: a
predicted prefix drop is a harness bug - log its location and count it (and when the stored original is at hand, send
the original bytes instead of the edit); a predicted model drop tells the router what a fallback would lose, so it can
choose a model that reads the blocks. After the response, compare the prediction with `input_transformations`: a
disagreement means your picture of the API is out of date (a new model, a changed posture), and deserves an alert.

## 11. Hands-on - a summary contract that keeps what the probes need

```
Exercise 11 (solution) - the contract, before and after
  strategy and contract        probes  missed               last summary (tokens)  day cost
  ---------------------------  ------  -------------------  ---------------------  --------
  summary, lab 02 contract     7/9     heatsink, fan_hours                  1,436     $1.46
  summary, richer contract     9/9     -                                    2,110     $1.53
  scratchpad, lab 02 contract  7/9     heatsink, fan_hours                  1,617     $1.39
  scratchpad, richer contract  9/9     -                                    2,291     $1.42
```

**What changed** - the contract, not the harness. The summary instructions no longer say "drop log exports"; they ask,
per site, for the latest controller-log entry for each fault code with its measurements, and for every dated operator
note, and they still drop the hourly readings. The scratchpad schema gains the same two fields, `fault_events` and
`operator_notes` (arrays of strings, required, `additionalProperties: false` preserved), and `render_richer()` shows
them to the model. The ninth probe is the fan module's hour counter at Riverbend (34,120), said only in an operator
note of the controller-log export - the kind of fact a summary drops because nobody asked for it.

**What it costs.** The last summary grows by 674 tokens (1,436 to 2,110; 1,617 to 2,291 for the scratchpad), and the
day by $0.07 and $0.03. Every later turn re-reads the summary, but from the cache; the summary tokens themselves are
output, written five times a day. Three to seven cents a day for two facts is cheap - the discipline is not to add
fields that no question needs.

**What not to add**, and why: the hourly readings (bulk, and one historian call away when a question needs them); the
full fault history (the latest entry per code answers the questions asked; a drill-down re-reads the rest, lab 06);
manual text (cite the section and re-read it - manuals are static and cache well); anything a system of record holds
(work orders, parts stock - query it); operators' names from the shift notes (personal data - keep the role); the
model's own speculation. Each field is paid for on every later turn and competes for attention - context rot on a
small scale. Choose probes the same way you choose fields: from the questions technicians actually ask late in the
day, and from what summaries tend to drop - numbers inside bulk exports, facts said only by the technician, negative
facts ("not to be restarted"), comparisons across sites.

## 12. Hands-on - a pre-warming scheduler and lookback-aware breakpoints

```
Exercise 12 - pre-warming scheduler and lookback-aware breakpoints
  12 requests, 3 depots                          requests that wrote  cache writes  cache reads  cost
  ---------------------------------------------  -------------------  ------------  -----------  -------
  fire everything at 07:00                                        12        28,296            0  $0.1871
  schedule(): pre-warm per prefix, then fan out                    3         7,074       28,296  $0.0687
  30 checklist blocks in one request  cache read  cache write
  ----------------------------------  ----------  -----------
  no intermediate breakpoint               1,352       15,586
  place_breakpoints()                     16,681          257
A turn that appends 60 blocks: 60 blocks after a 1-position gap needs 3 intermediate breakpoints but only 2 are free - split the turn into two requests
```

**`schedule()`** groups the requests by what the cache key is made of - model, system and tools, with `cache_control`
markers stripped - and finds three groups. It sends one `max_tokens: 0` request per group with that group's system
and tools and a placeholder message (the breakpoint sits on the depot's system block, so the entry is keyed to the
shared prefix, not to the placeholder), waits until the entries are readable, then fans the twelve requests out in
parallel. The three writes are the pre-warms themselves; all twelve real requests read, and the morning costs $0.0687
instead of $0.1871. In mock mode "readable" is `ready_delay` seconds after the writer starts; live, the pre-warm's
response arrives after prefill and the entry is readable then - no sleep needed.

**`place_breakpoints()`** treats the lookback as a hop limit. Block b of the new message sits gap + b + 1 positions
after the last entry (`gap` = positions already in between - here the one-block reply), so the first intermediate
breakpoint goes on the last block a 20-position walk can still reach, the next one 20 positions further, and so on,
until the automatic breakpoint on the final block is within reach. Thirty blocks after a one-position gap need one
mark (block 19), and the request reads the 16,681 cached tokens instead of re-writing the Harbor export. When the marks
exceed the free slots (four, minus the system breakpoint and the automatic one), it raises instead of guessing.

**Sixty blocks** need three intermediate breakpoints and only two are free. Options, best first: **merge the blocks** -
the lookback counts blocks, not tokens, so the same checklist as one text block is one position and needs no
breakpoint at all (a run of `tool_result` blocks is also a single position); **split the turn** into two user
messages with a short reply between them, if the flow allows; **free a slot** by dropping the explicit system
breakpoint - it works, but a later miss then re-writes the system prompt too; or **accept one miss** when such turns
are rare - one re-write of the conversation, about $0.10 here on Claude Opus 5. Merge.
