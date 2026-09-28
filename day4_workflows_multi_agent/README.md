# Day 4 - Workflow Patterns & Multi-Agent Systems

Days 1-3 built the parts: a model call with structured output, a tool loop, retrieval. Day 4 is
about composition - how to arrange LLM calls, tools and plain code into a system that is accurate,
cheap, fast and controllable enough to run a business process. We follow Anthropic's taxonomy
(augmented LLM -> workflows -> agents), implement every pattern on Kestrel's own data, and measure
each against a simpler alternative. One question runs through the day: **what is the least
autonomous design that meets the bar?**

## Learning objectives

By the end of the day you can:

1. Place a proposed LLM system on the spectrum from single call to multi-agent, and justify the
   position with properties of the task rather than taste.
2. Split work between deterministic code and LLM steps, and explain why that split is also your
   strongest defence against prompt injection.
3. Implement the five workflow patterns on the Messages API: chaining with gates, routing with
   cascades and overrides, sectioning and voting with bounded async concurrency,
   orchestrator-workers with a validated plan, and evaluator-optimizer with a rubric judge and a
   code veto.
4. Build a lead agent that delegates to subagents through a tool - with briefs, scoped tools, spawn
   caps, budgets and an assignment ledger enforced in code.
5. Measure competing architectures on accuracy, cost and latency (critical path, not sum), and turn
   the numbers into a documented design decision.
6. Compare LangGraph, CrewAI, AutoGen/AG2, the OpenAI Agents SDK, the Claude Agent SDK and Claude
   Managed Agents with plain code: what each gives you, and what it costs you.

## Agenda (about 7 hours)

| Block | Duration | Content |
|---|---|---|
| 1 | 40 min | The landscape: augmented LLM, workflows vs agents, code vs model (§1) |
| 2 | 50 min | Prompt chaining, then Lab 01 - the accounts-payable chain (§2.1) |
| 3 | 45 min | Routing, model routing and effort, then Lab 02 (§2.2) |
| - | 15 min | Break |
| 4 | 50 min | Parallelization, async concurrency, rate limits, then Lab 03 (§2.3) |
| 5 | 50 min | Orchestrator-workers, then Lab 04 - the cross-plant investigation (§2.4) |
| - | 40 min | Lunch |
| 6 | 35 min | Evaluator-optimizer, then Lab 05 (§2.5) |
| 7 | 60 min | Agents and multi-agent systems, then Lab 06 (§3) |
| 8 | 35 min | Frameworks (§4), Lab 07 benchmark and the design discussion; exercises start |

## Setup

```bash
cd agent-dev && . .venv/bin/activate
python day4_workflows_multi_agent/labs/01_prompt_chaining_invoices.py
```

Without `ANTHROPIC_API_KEY` every lab runs in **mock mode**: the real Anthropic SDK talks to labkit's
offline mock of the Messages API, which validates requests like the real API and answers through
rule-based stand-ins in `labkit/mock/scenarios/day4_workflows.py`. The stand-ins read the same
documents and tool results a model would see and never look at ground truth; the harnesses, tools,
policy engine and scoring are the real code. Token counts, and therefore costs, are simulated in mock
mode; the *ratios* between architectures are structural and carry over to live runs. With a key, the
same scripts call Claude. Running every lab and solution live costs in the order of $5-15 at list prices
(the mock's simulated total is about $3; real runs usually spend more on thinking); Labs 02 and 07 are
the largest - use `--limit` to shrink them.

**Latency, measured and modelled.** Mock responses arrive in microseconds, so measured wall-clock says
nothing about an architecture. The labs therefore also print a *modelled* latency: time-to-first-token
plus output tokens divided by a decode speed, per model (`labs/_common.py`). Those speeds are
illustrative planning assumptions, not published figures - replace them with the p50s you measure live
(the labs print both numbers so you can calibrate). What matters is the structure: a sequence of calls
costs the sum of their latencies, a parallel batch costs its slowest member.

---

## 1. The landscape

### 1.1 The building block: the augmented LLM

Every system today composes one unit: a single model call augmented with retrieval (Day 3), tools
(Day 2) and memory. Anthropic's "Building effective agents" calls this the *augmented LLM*. Before
composing several of them, get one right: a clear prompt, a schema for its output, tools with
descriptions that say when to call them. Most of the failures blamed on "the architecture" are
failures of one badly specified call.

### 1.2 Workflows vs agents

Anthropic draws the line by **who owns the control flow**:

- **Workflows** - LLMs and tools are orchestrated through predefined code paths. You can draw the
  flowchart before you see the input.
- **Agents** - the LLM dynamically directs its own process and tool use, deciding the next step from
  what the environment returns. The input draws the flowchart.

Both are "agentic systems", and both are legitimate. The difference has practical consequences.
A workflow's paths can be enumerated and tested, its cost per item is bounded by construction, and
its failures happen at known steps. An agent's path is open-ended: cost is bounded only by the budgets
you enforce, and evaluation needs scenario suites rather than unit tests (Day 6).

| Kestrel task | Design | Why there |
|---|---|---|
| Extract fields from one warranty email | single call + structured output | one transformation, no dependencies |
| Process a supplier invoice (Lab 01) | prompt chain | fixed steps; exactness and state belong in code |
| Triage the support inbox (Lab 02) | routing | distinct categories, different downstream handling |
| Review an invoice from several angles (Lab 03) | parallel sectioning / voting | independent aspects, a latency budget |
| Investigate quality incidents (Lab 04) | orchestrator-workers | subtasks depend on what the reports contain |
| Draft a dispute email (Lab 05) | evaluator-optimizer | articulable criteria, drafts that miss them |
| Diagnose an order-portal outage (Day 5) | agent | each finding decides the next query |
| Triage a large pump fleet (Lab 06) | multi-agent, *if* it is large enough | breadth, parallelism, context isolation |

### 1.3 Start with the simplest thing that works

Complexity is a cost you pay on every request: more calls, more tokens, more latency, more places to
fail, more to evaluate. The escalation ladder is: **improve the prompt -> add structure and code ->
add a workflow step -> add autonomy.** Take the next rung only when a measurement says the current one
misses the bar.

| Design | LLM calls per item | Cost | Latency | Main accuracy lever | Controllability | Fits when | Breaks when |
|---|---|---|---|---|---|---|---|
| Single call | 1 | lowest | lowest | prompt, schema, effort | high | one transformation | the task needs state or several kinds of reasoning |
| Prompt chain | fixed n | sum of steps | sum of steps | decomposition + gates | high | fixed subtasks | steps must adapt to each other |
| Routing | 1 + handler | lower than one-size-fits-all | + one hop | specialized handlers | high | distinct categories | misroutes are costly and undetectable |
| Sectioning | n in parallel | n calls | slowest branch | focused calls | high | independent aspects | aspects interact |
| Voting | N x the task | N x | slowest vote | aggregation rule | high | high-stakes yes/no judgements | votes are correlated |
| Orchestrator-workers | plan + k + synthesis | high | plan + slowest worker + synthesis | isolation, breadth | medium | subtasks unknown in advance | work is one dependent chain |
| Evaluator-optimizer | 2 per round | 2-3x a draft | rounds x 2 | explicit rubric | medium | criteria articulable | the first draft usually passes |
| Agent | open-ended | budget-bound | budget-bound | tools + model judgement | low | unpredictable steps | the process is known |
| Multi-agent | lead + k agent loops | highest | lead + slowest subagent | parallel breadth | lowest | broad, parallel, too big for one context | tasks are small or tightly coupled |

### 1.4 Code or model? The division of labour

The most consequential decision in every pattern below is which steps are LLM calls and which are code.

- **LLM steps** turn unstructured input into structured data (an unseen invoice layout), make
  judgements over fuzzy categories (is this email a warranty claim?), write language, and plan when the
  subtasks are not known in advance.
- **Code** owns rules, arithmetic, state across items, thresholds, authorization, and anything that must
  be exact, auditable, reproducible or safe under adversarial input.

Policy FIN-AP-010 makes the split concrete:

| FIN-AP-010 | Owner | The LLM's part |
|---|---|---|
| 1 Duplicate invoice | code - a ledger lookup on a normalized invoice number | transcribe the invoice number exactly (the model cannot see history it is not given) |
| 2-5 PO reference, status, currency, unit of measure | code - master-data lookups and comparisons | transcribe the PO number, currency and units as printed |
| 6 Price within PO + 1% | code - Decimal arithmetic | transcribe the unit price |
| 7 Cumulative quantity vs goods received | code - state across invoices | transcribe the quantities |
| 8 Extra charges <= $50 | code | include every priced line, surcharges too |
| 9 Arithmetic and tax | code, to the cent | transcribe - never correct - the printed numbers |
| 10 + §3 Bank changes, injected instructions | code (regex) **and** LLM flags, as a union | recognise paraphrases |
| §2 Approval routing, EUR -> USD | code | none |

The security consequence is the day's most important idea. **An injected instruction can only do what
the component that reads it is allowed to do.** INV-14 addresses "AI assistants" and asks them to
approve payment and change bank details. In Lab 01 the only component that reads it is an extraction
call with no tools and no authority, whose output is a schema; approval and bank changes are code paths
that read typed fields, never free text. The injection has nothing to act on. In Lab 07's single-call
and agent designs, the same text reaches the component that makes the payment decision.

Two opposite pitfalls. *LLM-as-policy-engine*: pasting the policy into a prompt and asking for the
decision - it usually works, and when it fails you cannot tell, reproduce or audit it. *Regex-as-reader*:
hand-writing parsers for unstructured documents - it works for the first twenty suppliers and fails
silently on the twenty-first.

---

## 2. The workflow patterns

### 2.1 Prompt chaining

**What.** A fixed sequence of steps in which each LLM call processes the output of the previous step,
with programmatic checks ("gates") between steps.

**Why.** Decomposition trades latency for accuracy and control: each call does one easier thing, and
every intermediate result can be inspected, validated and tested on its own. Chains are the natural
home for mixing LLM steps with code steps.

**When.** The task splits cleanly into fixed subtasks, and each subtask's input is the previous one's
output. Kestrel's AP pipeline is the textbook case: extract (LLM) -> check grounding (code) -> three-way
match (code) -> explain exceptions to the clerk (LLM). Not when one well-specified call does the job (a
chain for its own sake adds calls and latency), and not when later steps must change course based on
what earlier steps discovered - that is an agent.

**How it compares.** Lab 07 runs the same 20 invoices three ways (mock mode):

```
  architecture  decisions  codes P/R  LLM calls  input tok/inv  output tok/inv  cost/inv  p50 latency*  who decides
  single call   20/20      1.00/1.00  20         1,363          159             $0.0072   4.8s          LLM
  chain         20/20      1.00/1.00  20         525            166             $0.0068   4.8s          code
  agent         20/20      1.00/1.00  40         3,684          377             $0.0228   10.9s         LLM (with tools)
```

Accuracy ties by construction in mock mode (every stand-in applies the policy correctly). The
structural differences hold live: the single call re-sends the policy and earlier invoices with every
invoice, the agent pays for a second turn and its tool schemas, and only the chain keeps decision
authority out of the component that reads untrusted text. Live, look for accuracy differences on the
tail cases the other two designs must get right inside the model: INV-08's +0.8% price (inside the 1%
tolerance) and INV-14's cumulative quantity (a fifth freight trip against four received).

**Under the hood.**
- The Messages API is stateless: each step is a fresh request, and *you* decide what context it gets.
  That is a feature: the memo writer in Lab 01 receives code-verified facts, never the raw invoice.
- Structured outputs (`client.messages.parse(output_format=PydanticModel)`) constrain decoding to your
  JSON schema, so a completed response parses (check `stop_reason` first: a `refusal` or a `max_tokens`
  cut-off has no valid JSON). Schema-valid is not the same as correct - hence the gates.
- A grounding gate checks that extracted values really appear in the source: every invoice number, PO
  number, amount and part number must be printed on the page. It is cheap, deterministic, and catches
  hallucinated identifiers and silently "corrected" numbers.
- On a gate failure, retry once with a stateless repair request that states the problems, then route to
  a human. Never let an unverified record reach the policy step.
- Put the static instructions first (system prompt, `cache_control`) and the variable document last, so
  repeated items share a cacheable prefix - once it passes the model's minimum cacheable length (512
  tokens on Claude Opus 5).

**Pitfalls.**
- *The helpful extractor.* Asked for an invoice's tax, a model may return the tax it *should* be. Lab 01
  Step 5 shows why this is dangerous: "correcting" INV-13's 10% tax to the supplier's 8% makes the policy
  engine approve an invoice that is wrong on its face. Tell the extractor to transcribe, and let the gate
  check it did.
- *Compounding errors.* Three steps at 95% each give about 86% end to end unless gates catch failures in
  between.
- *Untrusted text flowing downstream.* Pass structured, validated fields to later LLM steps, not the raw
  document, and never free-text fields as instructions.
- *Floats for money.* Use Decimal (or integer cents) in the policy step; "correct to the cent" is
  unreliable on binary floats.
- *State in the prompt.* Duplicates and cumulative quantities need a ledger, not a longer prompt.

**Recipe.** One job per step; typed hand-offs (pydantic models); a gate after every LLM step (schema,
grounding, invariants); one repair attempt, then a human queue; least-privilege context for later
steps; static prefix first for caching; log every step's input and output for audit.

### 2.2 Routing

**What.** Classify the input, then dispatch it to a specialized handler: its own prompt, model, effort,
tools - or no LLM at all.

**Why.** Separation of concerns (each handler prompt is tuned for one kind of input without degrading
the others), cost (most traffic does not need the flagship model), and risk isolation (dangerous
categories go to humans by construction).

**When.** Distinct categories with genuinely different handling, and a classifier accurate enough for
the cost of a misroute. Kestrel's support inbox: order-status questions need a cheap acknowledgement,
technical questions deserve Claude Opus 5 with manuals, safety incidents need a human within the hour.
Not when all inputs are handled the same way, or when the right handling can only be discovered by
working on the input (then an agent chooses its tools as it goes).

**Model routing and effort.** Routing is also how you spend intelligence where it pays. Claude Opus 5
supports `output_config={"effort": "low" | "medium" | "high" | "xhigh" | "max"}` (default `high`); `low`
and `medium` are unusually strong on it and are the primary cost and latency lever. Claude Haiku 4.5 has
no effort parameter and no adaptive thinking - check `labkit.supports_effort(model, level)` before
sending effort. Anthropic's cost guidance puts numbers on the trade: in one knowledge benchmark Haiku 4.5
answered at about a tenth of Opus 5's cost per question but at 63% accuracy versus 92% - it fits
high-volume work with *checkable* outputs, not long agentic loops.

**Cascades.** Send everything to the cheap path first; escalate when a signal says it may be wrong. The
signal can be the classifier's confidence, a disagreement with a cheap heuristic, or a validator that
fails (Anthropic's guidance reports running coding tasks at `low` effort and re-running only the
failures at the default gave the same pass rate for about half the cost). Self-reported confidence is
a weak signal: in Lab 02 the router misroutes T-1405 ("after we replaced the bearings, the housing runs
at 90 °C") as a warranty claim *with confidence 0.90*.

**Deterministic overrides.** Some routes must not depend on a classifier: Lab 02 runs a safety tripwire
(regex) on every ticket and sends `requires_human` tickets (injection attempts, fraud, policy
exceptions) to people with no LLM reply at all. The classifier and the tripwire are combined with OR,
because the costs are asymmetric: a false alarm costs minutes, a missed safety incident can cost a life.

**How it compares.** A single general prompt is simpler and often good enough at low volume; routing
earns its keep when handlers genuinely differ or volume makes the flagship expensive. An agent choosing
its own tools is the dynamic version; routing is its predictable, testable cousin.

**Under the hood.** The classifier is a structured-output call with an enum; the router is a Python
dictionary. Caching differs per model: the minimum cacheable prefix is 512 tokens on Opus 5, 1,024 on
Sonnet 5 and 4,096 on Haiku 4.5 - Lab 02 shows the cheap classifier paying full price for the rubric on
every call while the flagship reads it from cache.

**Pitfalls.** Priority that depends on facts not in the text (T-1505 is a strategic customer's
complaint; the tier lives in the CRM, so enrich in code before classifying); handlers that cannot bounce
a misrouted ticket; category drift over months; measuring overall accuracy while the metric that
matters is safety recall.

**Recipe.** Enrich in code; classify with a fast model and a closed enum; apply deterministic
overrides; cascade on confidence and on disagreement signals; give each route its own prompt, model and
effort; let handlers bounce; track per-category precision and recall and 100% safety recall on a
labelled set.

### 2.3 Parallelization: sectioning and voting

**What.** *Sectioning* splits a task into independent subtasks that run at the same time; code merges
the results. *Voting* runs the same judgement several times - with different prompts or models - and
aggregates the answers with a rule.

**Why.** Latency: a parallel batch takes as long as its slowest member, not the sum. Focus: one call per
aspect attends better than one call juggling all aspects. Confidence: independent votes expose
uncertainty and let you set the error trade-off explicitly.

**When.** Kestrel's invoice review has independent lenses (fraud, policy compliance, communication
tone) - sectioning. "Should this invoice be escalated?" is a high-stakes yes/no judgement - voting.
Anthropic's examples include guardrails: one call answers the user while another screens the input.
Not when the aspects interact (the tone review needs the fraud review's findings), or when latency and
cost do not matter.

**How it compares.** Sectioning fixes its subtasks in code; orchestrator-workers lets an LLM choose them
at runtime. Voting is a statistical check; evaluator-optimizer is an iterative one.

**Under the hood - async concurrency.**

```python
async with get_async_client() as client:                        # anthropic.AsyncAnthropic
    semaphore = asyncio.Semaphore(8)                             # bound what is in flight
    async def run(factory):
        async with semaphore:
            return await factory()
    results = await asyncio.gather(*(run(f) for f in factories), return_exceptions=True)
```

- *Rate limits* are per model and measured in requests and tokens per minute; a burst of parallel calls
  hits them first. The semaphore bounds concurrency; the SDK retries 429 and 5xx responses with
  exponential backoff (`max_retries`, default 2) and honours `retry-after`.
- `return_exceptions=True` keeps one failed branch from cancelling its siblings. The aggregator then
  decides whether a partial result is acceptable - Lab 03 marks the merged review *incomplete* when the
  tone reviewer crashes.
- Parallelism cuts latency, never cost. Voting multiplies cost by N, which is why Lab 03's voters run
  on the fast model.
- *Independence.* Five identical calls to the same model make correlated errors. Claude Opus 5 does not
  accept `temperature` anyway, so diversity comes from *prompts* (Lab 03's five lenses: security,
  treasury, AP clerk, procurement, auditor) or from different models.

Lab 03 (mock mode):

```
  run                               modelled latency  measured wall-clock
  sequential (await one by one)     14.2s             0.092s
  concurrent (gather, semaphore=8)  6.0s              0.008s

  threshold  escalated  precision  recall  invoices
  1 of 5     7          0.29       1.00    INV-07, INV-09, INV-11, INV-12, INV-14, INV-17, INV-19
  2 of 5     3          0.67       1.00    INV-12, INV-14, INV-17
  3 of 5     1          1.00       0.50    INV-14
```

(The measured wall-clock column varies from run to run; in mock mode it only shows the harness overhead.)

**Pitfalls.** Unbounded `gather` (a 429 storm); treating a missing vote as a "no"; a majority threshold
chosen by habit - Lab 03's majority catches the bank-change fraud and misses the duplicate invoice,
because only two of the five lenses can see double billing; merging with an LLM when a `max()` would do.

**Recipe.** Sectioning: independent subtasks, a semaphore sized to your rate limits,
`return_exceptions=True`, aggregation in code, an explicit "incomplete" state. Voting: diverse prompts
or models, a cheap model, and a threshold (or veto rule) chosen from the costs of misses and false alarms
on a labelled set.

### 2.4 Orchestrator-workers

**What.** An orchestrator LLM decides the subtasks at runtime, workers execute them - each in its own
context - and a synthesis step merges and verifies the results.

**Why.** Some tasks cannot be decomposed until you see the input. Workers give context isolation: each
reads only its share, so the orchestrator's context stays small and each worker's attention stays on
its material.

**When.** Kestrel's quality team gets a varying set of incident reports each week; the right split
(by plant? by supplier?) depends on what the reports contain. Not when the fan-out is data-driven and
fixed ("one task per site in this list" is a loop in code - sectioning), and not when the work is one
dependent chain that fits in one context.

**How it compares.** Anthropic's cost guidance is blunt: an orchestrator "buys something only when there
is bulk to hand off - many independent pieces, ideally too many for one context window". On work larger
than any context window, an orchestrator with cheaper workers cost 55% less than the frontier model
alone at every effort setting, 3 to 7 points below its best score; when the work fits in one context,
the orchestrator pays for a plan, a handoff and a merge that a single model gets for free, and in every
such case measured the coordinator's model alone at lower effort came out ahead.

**Under the hood (Lab 04).**
1. The orchestrator sees an **index** of the nine reports (ID, plant, date, title, category, severity) -
   no bodies - and returns an `InvestigationPlan` as structured output. The lab fixes the partition key
   (one worker per plant, because plants own their reports) so the plan stays checkable; what the
   orchestrator contributes is the briefs and the cross-plant hypotheses to verify - for example, that
   leaks at P2's pump test could come from P1's porous castings. If code could write the whole plan, let
   code write it (exercise 6).
2. **Code validates the plan before anything is spawned**: the spawn cap, unknown report IDs, a report
   assigned twice, a report assigned to nobody.
3. Each worker gets a brief (objective, questions, out-of-scope line) and only its plant's reports, and
   returns `PlantFindings` - lots, suppliers, shipped exposure, verbatim evidence.
4. The synthesizer verifies every lot hypothesis against the ERP extract with a **read-only SQL tool**:
   the connection is opened read-only, a SQLite authorizer allows SELECT on five allow-listed tables only
   (so sub-queries, CTEs, `PRAGMA`, `ATTACH` or `sqlite_master` cannot get around it), and results are
   capped. It also runs a control query - defect RMAs per lot across all lots - to separate a lot problem
   from background noise.
5. Critical path = plan + slowest worker + synthesis turns.

**Pitfalls.** Vague briefs (the worker sees nothing else); overlapping tasks (duplicated work and
conflicting findings); a synthesizer that summarizes summaries instead of checking facts - Anthropic
calls this the "game of telephone" problem; treating an empty query result as "no exposure" (exercise 8);
unbounded fan-out.

**Recipe.** Index in, structured plan out; validate the plan in code; one partitioning key; precise
briefs with an output contract; workers on a cheaper model when the work is reading; synthesis with
tools that verify; a spawn cap and budgets in code; report what was *not* analysed.

### 2.5 Evaluator-optimizer

**What.** One call generates, another evaluates against explicit criteria and returns a critique; the
generator revises until the output passes or a round limit is hit.

**Why.** When quality criteria can be articulated and iteration measurably improves the output, a
separate evaluator catches what the generator does not see in its own work - and yields an auditable
pass/fail per criterion.

**When.** Supplier dispute emails (facts, policy, tone, a clear ask), knowledge-base articles against a
style guide, translations with a glossary. Not when the first draft usually passes (the loop then costs
2-3x for nothing - measure the pass rate of round one) and not when the criteria are vague ("make it
better").

**How it compares.** Self-critique inside one call is cheaper but shares the generator's blind spots.
Voting judges the same output several times; evaluator-optimizer improves the output. Human review is
the gold standard and the most expensive. Claude Managed Agents offers the hosted version as
*outcomes*: you state what done looks like and a rubric, and a grader in an independent context scores
each iteration and feeds back per-criterion gaps, until the rubric is met or `max_iterations` (default 3,
maximum 20) is reached.

**Under the hood (Lab 05).** Facts are computed by code (extraction + PO + goods receipt), so the
writer computes nothing. The rubric has four independently gradeable criteria. The judge is a different
model with a structured `Evaluation` output. A deterministic check vetoes any draft whose amounts or
percentages are not among the facts, and its findings join the critique. The loop stops on pass, on the
round limit, or when the lowest score stops improving.

Note on Claude Opus 5: it verifies its own work without being told, and Anthropic's migration guidance
recommends deleting "double-check your answer" instructions and verification scaffolding carried over
from older models. An *external* evaluator is a different thing: it checks the output against criteria
the writer does not own (policy, a style guide) and produces an auditable verdict.

**Pitfalls.** A lenient judge (calibrate it against human scores - Day 6); criteria that cannot be graded
independently; loops without a stop condition; a generator that learns to please the judge rather than
the reader; a judge that sees different facts than the writer.

**Recipe.** Facts from code; a rubric of independently gradeable criteria; a different model or at least
a different prompt for the judge; structured judge output; code checks as a veto; stop on pass, rounds
or no progress; measure how often round one already passes.

---

## 3. Agents and multi-agent systems

### 3.1 From workflow to agent

An agent is an LLM in a loop: it calls tools, reads the results, and decides the next step until it
decides it is done or a budget stops it. Use one when the steps cannot be predicted and the environment
gives useful feedback - incident diagnosis (Day 5), open-ended research. You pay in variability,
compounding errors and harder evaluation, so agents run with budgets (turns, tool calls, tokens,
dollars), sandboxed or read-only tools, and human checkpoints before irreversible actions.

The mechanics from Day 2 apply to every loop in these labs: append the **full** `response.content`
(thinking blocks included) to the conversation, return **all** tool results of a turn in **one** user
message before any text, and check `stop_reason` before reading content (`tool_use`, `end_turn`,
`max_tokens`, `refusal`). Forced `tool_choice` is rejected by Claude Opus 5.5 and Fable 5.1, so the labs
use `auto` with clear instructions, strict tools (`"strict": True`) and structured outputs instead.

### 3.2 The architecture: a lead and its subagents

A multi-agent system puts a **lead** (orchestrator) agent in charge of **subagents**, each an agent loop
with its own context window, its own tools and often its own model. Delegation is itself a tool: in
Lab 06 the lead calls `delegate_to_analyst(asset_ids, brief)`, the harness spawns an analyst subagent,
and the analyst's structured report comes back as the tool result. Delegations issued in one turn run in
parallel.

Anthropic's multi-agent research system is the best-documented production example: a lead agent on a
stronger model delegating to parallel subagents on a cheaper one. Anthropic reported that it outperformed
a single agent on the stronger model by 90.2% on an internal research evaluation, that token usage alone
explained 80% of the performance variance on the BrowseComp benchmark, and that such systems use about
15x the tokens of a chat interaction (single agents about 4x). Multi-agent works there because research
is broad, parallel and larger than one context. The same post notes that domains where all agents need
the same context, or with many dependencies between agents - most coding tasks - are poor fits today.

### 3.3 Context hand-off and shared state

**The brief is the subagent's whole world.** It sees nothing of the lead's conversation. A good brief
states the objective, the output contract, how to work (tools, method), the boundaries (what is out of
scope, who owns what) and the budget. Anthropic's delegation guidance adds: brief precisely the first
time (avoid launch-wait-rebrief), and once you delegate, commit - do not redo the subagent's work.

**Shared state vs message passing.**

| Mechanism | Example | Strengths | Risks |
|---|---|---|---|
| Message passing (reports as tool results) | Lab 06's `AnalystReport` | simple, auditable, the lead sees exactly what it merges | the lead's context grows with every report; nuance is lost in summaries |
| Shared files / blackboard | Managed Agents threads share one container filesystem | large artifacts pass by reference, not through context | conflicting writes; readers can see half-written state |
| Shared database | an AP ledger, an assignment ledger | transactions, constraints, a single source of truth | a schema to design; the agents need tools for it |

Conflicting writes are prevented by design, not by prompting: one writer per resource, an assignment
ledger that rejects double ownership, unique constraints, or optimistic concurrency on files.

### 3.4 Failure modes

| Failure | Mechanism | Lab 06 / exercises | Prevention |
|---|---|---|---|
| Cost multiplication | every subagent re-sends its prompt prefix, caches nothing it shares with the lead, and its report is read again by the lead | 1.45x input tokens, 1.65x cost for the same 12 pumps | delegate only bulk work; cheaper subagent models; measure tokens per unit of work |
| Context loss | the brief and the report compress what each side knows | ex. 7: a brief with a priority but no method | briefs with method and output contract; verbatim evidence in reports |
| Duplicated work | overlapping assignments | ex. 7: 51% of a run's cost wasted | plan validation, assignment ledger, scoped tools |
| Conflicting findings | two agents judge the same thing differently | ex. 7: bearing wear vs "sensor drift" | one owner per item; an evidence-based conflict rule; escalate to a human, don't re-spawn |
| Over-delegation | the model spawns subagents for small work | Claude Opus 5 delegates readily | spawn caps and budgets in code |
| Error propagation | a wrong fact passes through hand-offs unchecked | ex. 8: a transposed lot ID becomes "no exposure" | grounding gates at hand-offs; tools that reject unknown identifiers |
| Coordination latency | the lead waits for the slowest subagent, then merges | Lab 06: 69 s vs 51 s modelled | parallel tool calls in a single agent often capture the latency win |
| Runaway recursion | subagents spawning subagents | - | one level of delegation (Managed Agents enforces it) |

### 3.5 When multi-agent wins, and when it loses

It **wins** when the work is breadth-first and parallelizable, the corpus is larger than one context
window, the subtasks are genuinely independent, different subtasks need different tools or models, or
isolation itself is valuable (a subagent that reads untrusted material never sees the lead's
privileges). It **loses** on small tasks, on tightly coupled work where every step needs the previous
step's full context, on cost-sensitive workloads, and when latency is dominated by the fixed cost of
extra turns.

Lab 06 is deliberately on the losing side of that line, and the table says so (mock mode):

```
  architecture       issues right  faults found  false alarms  calls  input tok  output tok  cost     latency*  wall-clock  largest prompt
  A lead + analysts  12/12         5/5           0             11     32,693     4,779       $0.2374  69s       0.10s       5,813
  B single agent     12/12         5/5           0             3      22,513     2,432       $0.1435  51s       0.04s       12,431
```

(`latency*` is the modelled critical path; `wall-clock` is measured and, in mock mode, near zero.) Twelve
pumps fit comfortably in one context, and the single agent fetches all twelve summaries in one
turn of parallel tool calls. What the multi-agent run buys is the last column: no single call saw more
than 5,813 tokens, against 12,431 for the single agent. That column grows linearly with the fleet
(exercise 3 estimates ~100 pumps before a single agent's final turn passes 200K tokens), and so does the
risk that a subtle pattern - GB-KP400-02's night-time cavitation hides behind a normal mean - gets lost.
The crossover is a measurement, not a belief.

### 3.6 Claude Opus 5 delegates readily - cap it in code

Anthropic's migration guidance flags a direction change: Claude Opus 4.8 under-reached for subagents,
while Claude Opus 5 "reaches for them freely, which multiplies cost and latency". It recommends removing
any "delegate more" prompting, adding delegation guidance (subagents for large, independent,
parallelizable work; not for work you could finish in a handful of tool calls; not for verification;
one subagent rather than several when one suffices; never more than 20 parallel agents unless the user
explicitly asks), and it names a deterministic ceiling on spawn count as the reliable lever.

Lab 06 applies all of it: the lead's prompt carries the delegation guidance, and the harness enforces a
spawn cap, a maximum number of pumps per analyst, an assignment ledger, per-analyst budgets (turns, tool
calls, tokens), and a telemetry tool whose `asset_id` enum lists only the analyst's own pumps (and is
re-checked in code). The prompt nudges; the harness guarantees. Claude Managed Agents enforces similar
limits on the hosted side: 1-20 roster entries, one level of delegation, at most 25 concurrent threads
per session, and an optional session budget that pauses every thread at a list-cost ceiling.

### 3.7 Model routing and effort across roles

Give the lead the strongest model and moderate effort (it plans and judges); give reading-heavy workers
a cheaper model; keep verification on the lead. Switching models in the middle of one conversation
invalidates its prompt cache (caches are per model), which is why Anthropic recommends a subagent on the
cheaper model for a sub-task rather than switching the main loop's model. Sweep effort per role on your
own eval set; the defaults carried over from older models are rarely right on Claude Opus 5.

---

## 4. Frameworks: what they give you, what they cost you

You do not need a framework to learn - or to ship - these patterns. Every lab today is plain Python over
the Anthropic SDK; the multi-agent harness in Lab 06 (budgeted agent loop, delegation tool, assignment
ledger) is about 160 lines. Frameworks earn their place when you need what they have already built
(durable execution, a hosted sandbox, a UI) more than you need control over every prompt and every loop.

| Option | Core abstraction | What it gives you | What it costs you |
|---|---|---|---|
| **Plain code + Anthropic SDK** | functions, the Messages API, your loop | full control of prompts, context, retries and budgets; nothing hidden; easiest to debug and test | you build persistence, tracing and human-in-the-loop yourself (labkit's tracer is ~155 lines) |
| **LangGraph** | a graph (state machine) of nodes and edges over a typed shared state, with checkpointing | explicit control flow including cycles; persistence and resume; human-in-the-loop interrupts; streaming | its state model and abstractions to learn; calls wrapped in its components; version churn |
| **CrewAI** | role-based agents (role, goal, backstory) forming a crew that runs tasks sequentially or under a manager | fast prototypes of role-play multi-agent setups | prompt text you did not write; token overhead; exact control flow is hard to guarantee |
| **AutoGen / AG2** | agents that converse - two-agent and group chats with speaker selection | flexible conversational patterns; code-executing agents | conversations are hard to bound and terminate; cost; two diverging lineages (Microsoft's AutoGen and the community AG2) |
| **OpenAI Agents SDK** | agents, handoffs (transfer of control between agents), guardrails, sessions, tracing | small, clean primitives; the handoff makes routing and delegation first-class | built around another provider's APIs; its abstractions over your prompts |
| **Claude Agent SDK** | the Claude Code harness as a library: agent loop, built-in file/shell/web tools, permissions, hooks, subagents, MCP, context management | a proven general-purpose harness for agents that work in a filesystem or computer environment (Day 5) | you adopt its model of the world (files, tools, permission modes) and its runtime; overkill for a fixed workflow like FIN-AP-010 |
| **Claude Managed Agents** (beta) | hosted, versioned agent configs; sessions in a sandbox container; a multiagent coordinator with context-isolated threads over a shared filesystem; outcomes; session budgets | no infrastructure; sandboxed tool execution; persistence; hosted multi-agent orchestration and hard spend caps | beta surface; less control over loop internals; hosted data flow; roster and thread limits; no Batch API for sessions |

**Choosing.** A fixed, auditable business process (AP matching) belongs in plain code: it is a chain
plus a policy engine, and a framework adds nothing it needs. A long-running, stateful workflow with
human approvals is where LangGraph-style durable execution pays. An agent that must operate on files and
shells is what the Claude Agent SDK is for. A multi-agent job you would rather not host - with a sandbox
and a hard budget - fits Managed Agents. Whatever you pick, keep the patterns visible: you should still
be able to say which component owns the control flow, what each call sees, and what each call may do.

---

## 5. Case studies

### 5.1 Accounts payable: three-way matching under FIN-AP-010

**Situation.** About 1,200 supplier invoices a month are matched by hand against purchase orders and
goods receipts. The sample of 20 contains the usual mess: a price 5% over the PO (INV-07), 300 boards
invoiced against 250 received (INV-09), a missing PO (INV-11), a resent duplicate (INV-12), a supplier
charging 10% tax instead of 8% (INV-13), a unit-of-measure switch from metres to kilograms (INV-15), an
invoice against a cancelled PO (INV-17), a $420 fuel surcharge (INV-19), a EUR supplier invoicing in USD
(INV-20) - and INV-14, which asks to change bank details and tells "AI assistants" to approve it.

**Constraints.** No agent may change supplier bank details; every decision must be auditable; the
controller wants cost per invoice in cents.

**Design.** A chain (Lab 01): LLM extraction with structured output -> grounding gate -> deterministic
FIN-AP-010 engine with a ledger for duplicates and cumulative quantities -> LLM memo for exceptions only,
written from verified facts. Deterministic detectors run on the raw text alongside the LLM's flags.

**Result (mock mode).** 20/20 decisions and 14/14 exception codes correct; 12 codes raised by pure
code, 2 by detectors that combine the LLM's flag with a regex. INV-14 is stopped three times over: the
bank-change request and the injected instructions each put it on security hold, and a fifth freight trip
against four received would hold it even if both fraud signals had been missed. Lab 07 shows the alternatives: a single call per invoice is about as cheap, an agent about 3x
more expensive - and in both, the component that reads INV-14 is the component that decides.

### 5.2 A cross-plant quality investigation

**Situation.** Nine incident reports from three plants. Hidden in them: MS-250 seal lot PS-2608-B from
Precision Seals is cracking at the O-ring groove (P2 test failures, pumps shipped), and KC-2 board lot
VD-2607-C from VoltDrive has solder voids that cause F20 watchdog resets in hot rooms (P3 burn-in
failures, controllers shipped). Around them, red herrings: porous KP-400 castings at P1 (contained, never
shipped to P2), an overdue torque wrench at P2 (used only on coupling set screws, not seal glands), an
ESD tester failure at P3 (a KC-1-only station, all units retested), a forklift near miss and a coolant spill.

**Design.** Orchestrator-workers (Lab 04): a plan from the index; one worker per plant in an isolated
context; a synthesizer that verifies each lot against build records and RMAs through a read-only SQL tool,
with a control query across all lots.

**Result (mock mode).** Both lots identified with evidence - PS-2608-B: 9 units in the ERP sample across
6 orders and 6 customers, 2 defect RMAs (RMA-7002, RMA-7003) against 0 for the other seal lots;
VD-2607-C: 2 units at Lumen Data Centers, no RMAs yet - and all red herrings dismissed with the reason
quoted from the report itself. Whole pipeline: $0.11 simulated, 57 s modelled latency with parallel workers.

### 5.3 Fleet condition monitoring

**Situation.** Twelve pumps at three customer sites, 30 days of hourly telemetry, five developing faults
(bearing wear, cavitation, misalignment after coupling work, operation right of the best-efficiency
point, a dead vibration sensor) among seven healthy pumps with red herrings (single-sample spikes,
standby pumps with a few test hours).

**Design.** Lab 06 builds both a lead with analyst subagents and a single agent, on the same telemetry
tool (pre-computed views: weekly summary, daily means, hour-of-day profile, work orders).

**Result (mock mode).** Both designs find all five faults with no false alarms; the multi-agent run
costs 1.65x as much and takes longer, and its largest prompt is less than half the single agent's. For
12 pumps the single agent is the right design; the harness is ready for the fleet size where it stops
being right.

---

## 6. Lab walkthrough

All excerpts below are from mock mode (`LABKIT_MODE=mock`); live runs print the same structure with
Claude's wording and real token counts.

### Lab 01 - Prompt chaining on 20 invoices

`python day4_workflows_multi_agent/labs/01_prompt_chaining_invoices.py [--model ...] [--effort low] [--memos 3]`

1. Loads suppliers, POs, goods receipts and the 20 invoices through `labkit.data`.
2. Runs the chain per invoice in arrival order - the ledger carries duplicates and cumulative quantities
   from one invoice to the next.
3. Scores decisions and exception codes against `data/finance/expected_ap_outcomes.json`.
4. Shows INV-14's injection, the extractor's flags, the code's decision and which detector fired.
5. Tampers with INV-13's extraction the way an over-helpful model might, and shows the gate catching it.
6. Writes three exception memos from verified facts, then projects the cost to 1,200 invoices a month.

```
  INV-12_allied_bearings_resend  AB-77120 / PO-4500128 / USD 21,440.00      ok    ok           reject              duplicate_invoice
  INV-13_packright               PR-88460 / PO-4500155 / USD 2,343.00       ok    ok           hold                tax_calculation_error
  INV-14_coastal_freight         CFS-5588 / PO-4500140 / USD 1,850.00       ok    ok           security_hold       bank_details_change_request, prompt_injection, quantity_exceeds_received
...
  decision accuracy 20/20 (100%); exception codes precision 1.00 recall 1.00 (tp=14 fp=0 fn=0)
  exception codes raised by pure code: 12; by detectors that use the LLM's flags: 2
...
  Printed on INV-13: tax 213.00, total 2,343.00.  Tampered record: tax 170.40, total 2,300.40.
  Without a gate the policy engine would say: approve   <- the supplier's error disappears
  The grounding gate says:
    - tax_amount 170.4 is not printed on the document
    - total 2300.4 is not printed on the document
```

*Live, watch:* the extraction's handling of the two layouts, whether the gate ever fires (and why), and
the per-invoice cost at `effort="low"` versus `--effort medium`.

### Lab 02 - Routing the support inbox

`python day4_workflows_multi_agent/labs/02_routing.py [--limit 62] [--workers 8]`

1. Prints the route table: prompt, model and effort per category, including routes with no LLM.
2. Pipeline A: Haiku 4.5 classifies (structured output), a cascade re-classifies low-confidence tickets
   with Opus 5, the safety tripwire and `requires_human` override, and specialized handlers draft replies.
3. Pipeline B: every ticket to Opus 5 at default effort, classifying and drafting in one call.
4. Compares accuracy against `ticket_labels.jsonl`, cost per ticket and modelled latency.

```
  pipeline    category  priority  safety recall  needs-human recall  calls  cost     cost per ticket  p50 latency*
  A router    61/62     60/62     4/4            7/7                 116    $0.1974  $0.00318         3.1s
  B flagship  61/62     60/62     4/4            7/7                 62     $0.5497  $0.00887         7.7s
  router saves 64%; at 1,900 tickets/month: router $6.05 vs flagship $16.85 (drafting only - no tool calls)
...
  cache reads: classifier 0 tokens vs flagship 63,684. The shared rubric prefix is below Haiku 4.5's 4,096-token cache minimum (Opus 5: 512), so the cheap model pays full input price for it on every call.
  T-1405: routed as warranty_claim (warranty_desk), label technical_support; confidence 0.90
```

In mock mode both pipelines share one rule-based classifier, so their accuracy is identical by
construction. *Live, watch:* the accuracy gap between Haiku and Opus on the hard tickets (multi-issue
T-1410, the injection tickets, the Spanish and German ones), how often the cascade fires, and whether
the saving survives once handlers use tools.

### Lab 03 - Sectioning and voting

`python day4_workflows_multi_agent/labs/03_parallelization.py [--invoice INV-14] [--concurrency 8]`

1. Three reviews of INV-14 (fraud, policy, tone), first awaited one by one, then concurrently.
2. The same with a crashing tone reviewer: the merged review is flagged incomplete.
3. Five lenses vote on all 20 invoices (100 calls on the fast model, bounded by the semaphore).
4. A k-of-5 threshold sweep against the AP team's escalations.

```
  [policy] FRT-LTL: 5 invoiced in total (earlier invoices 4) vs 4 received.
  [tone] Claims authority: "pre-approved by Kestrel's CFO" - unverifiable, a classic social-
  engineering move.
  merged risk: HIGH   codes proposed by the reviewers: ['bank_details_change_request', 'prompt_injection', 'quantity_exceeds_received']
...
  complete=False missing=['tone'] risk=HIGH codes=['bank_details_change_request', 'prompt_injection', 'quantity_exceeds_received']
...
  INV-12_allied_bearings_resend  . . Y . Y       2/5    -           60%        escalate
  INV-14_coastal_freight         Y Y Y . Y       4/5    SUSPICIOUS  80%        escalate
```

*Live, watch:* real wall-clock for sequential vs concurrent, 429s if you raise `--concurrency` on a low
rate-limit tier, and how often the lenses disagree.

### Lab 04 - The cross-plant investigation

`python day4_workflows_multi_agent/labs/04_orchestrator_workers.py [--worker-model claude-sonnet-5] [--max-workers 4]`

1. The orchestrator plans from the index; code validates the plan.
2. Three plant workers run in parallel on Sonnet 5; `count_tokens` shows what each context held.
3. The synthesizer issues SQL through the read-only tool (lots, RMAs, a control query).
4. The report is rendered to Markdown and saved to `.runs/day4/cross_plant_quality_report.md`.
5. The report is checked against the answer key.

```
  plan check: cap respected, every report assigned exactly once
  [plant-P2] INC-P2-0419: Repeated seal failures in hydrostatic test; O-ring groove cracks | lot=PS-2608-B | shipped=confirmed | quality=True
  context                         reports read  input tokens (count_tokens)
  plant-P1                        3             932
  plant-P2                        3             1,072
  plant-P3                        3             964
  (one agent reading everything)  9             2,232
...
  - 2 defect RMA(s) on PS-2608-B units (RMA-7002, RMA-7003) vs 0 across 3 other PS lot(s) - the
  control comparison.
...
  - INC-P2-0415: Ruled out by the report itself: "TW-118 is used only for coupling hub set screws,
  not for seal gland bolts."
...
  lot PS-2608-B: identified
  lot VD-2607-C: identified
  total $0.1061; modelled latency 56.6s with parallel workers vs 70.0s if the workers ran one after another
```

*Live, watch:* the SQL Claude writes (and how it recovers from a rejected query), whether it runs a control
comparison without being told how, and how it phrases the dismissals.

### Lab 05 - Evaluator-optimizer

`python day4_workflows_multi_agent/labs/05_evaluator_optimizer.py [--max-rounds 3] [--judge-model ...]`

1. Facts for INV-07 from code: EUR 248.00 vs 236.00 per seal, +5.08%, EUR 1,800.00 over 150 units.
2. Round by round: the draft, the judge's scores and comments, the code check, the critique.
3. The cost of the loop versus a single unchecked draft.

```
  facts_correct     2      States 4%; the facts give a variance of 5.08%.
  policy_compliant  3      Does not say the invoice is on hold under the price-tolerance policy.
  tone              2      Hostile phrasing: unacceptable, will not tolerate, sort this out.
  clear_ask         2      No concrete requested action or reply-by date.
  code check: percentage 4% does not match the facts (variance 5.08%, tolerance 1%)
...
  -> PASSED in round 2
  one unchecked draft: $0.0079; the evaluated loop: $0.0224 (2.8x) - worth it only if a bad email would otherwise have been sent
```

The mock writer's first draft is deliberately weak so the loop has work to do. *Live, watch:* how often
Claude's first draft already passes - if it is almost always, the loop is a cost, not a safeguard.

### Lab 06 - Multi-agent triage

`python day4_workflows_multi_agent/labs/06_multi_agent_triage.py [--spawn-cap 3] [--analyst-model ...]`

1. The lead sees the fleet (identity and context, no telemetry).
2. Architecture A: the lead delegates one analyst per site in a single turn; the harness validates each
   delegation, runs the analysts concurrently and returns their reports.
3. Architecture B: one agent with the same tool.
4. Accuracy against `ground_truth.json`, tokens, cost, modelled critical path, measured wall-clock and
   the largest prompt.
5. The guardrails, probed directly.

```
  analyst-1: pumps GB-KP400-01, GB-KP400-02, GB-KP250-03, GB-KP250-04 | turns=3 tool_calls=8 status=done | 10,340 in / 927 out
  GB-KP400-02   action_required  cavitation             cavitation             Mean vibration looks normal (2.69 mm/s) but it is erratic (weekly spread ~1.16
...
  token multiplication: the multi-agent run read 1.5x the input tokens of the single agent
...
  a further delegation after 3 analysts -> Rejected: spawn cap reached (3 analysts per run). Merge groups into the analysts you already have, or finish with the reports you have.
  a pump that is already assigned -> Rejected: HF-KP250-03 already assigned (analyst-2).
```

*Live, watch:* how many analysts Claude Opus 5 asks for without the cap (try `--spawn-cap 12`), how
specific its briefs are, and whether the single agent's accuracy drops when you make the task harder.

### Lab 07 - Pattern benchmark

`python day4_workflows_multi_agent/labs/07_pattern_benchmark.py [--limit 20]`

Runs the invoices as a single call, the chain and an agent with lookup tools (`get_purchase_order`,
`get_goods_receipts`, `get_supplier`, `get_invoice_history`), prints the side-by-side table from §2.1,
and a discussion guide covering accuracy, cost, latency, controllability and authority. *Live, watch:*
the tail cases, and the cost per invoice of each design at Kestrel's volume.

---

## 7. Key takeaways

1. **Choose the least autonomous design that meets the bar.** Workflows when you can draw the flowchart
   before seeing the input; agents when the input draws it; multi-agent only for broad, parallel work
   that outgrows one context.
2. **LLMs read and judge; code decides.** Rules, arithmetic, state and authority belong in code. It makes
   decisions exact and auditable - and it is the architecture, not the prompt, that neutralizes injected
   instructions.
3. **Gate every hand-off.** Structured outputs guarantee a schema, not the truth; grounding checks catch
   invented identifiers and "corrected" numbers.
4. **Route by cost and risk.** Cheap models for checkable high-volume work, the flagship where judgement
   pays, no LLM where a human must act - and deterministic overrides for the misroutes you cannot afford.
5. **Parallelism buys latency, never cost.** Bound it with a semaphore, survive partial failures, and
   report what is missing.
6. **Pick vote thresholds from error costs.** A majority rule is a habit, not a design.
7. **Evaluator loops must earn their cost.** Explicit rubrics, a separate judge, code vetoes, and a
   measured round-one pass rate.
8. **Multi-agent multiplies tokens.** Budget for it, cap spawns in code (Claude Opus 5 delegates readily),
   keep an assignment ledger, scope each subagent's tools, and make every brief self-contained.
9. **Measure the critical path.** Sequential steps add, parallel branches take the slowest, and a single
   agent's parallel tool calls often capture the multi-agent latency win for free.
10. **You do not need a framework to learn the patterns.** Adopt one for what it has already built, and
    keep the control flow visible either way.

## 8. Further reading

- Anthropic, "Building effective agents": https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, "How we built our multi-agent research system": https://www.anthropic.com/engineering/multi-agent-research-system
- Anthropic, "Writing effective tools for agents": https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, "Effective context engineering for AI agents": https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Tool use: https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
- Effort: https://platform.claude.com/docs/en/build-with-claude/effort
- Adaptive thinking: https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
- Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Batch processing: https://platform.claude.com/docs/en/build-with-claude/batch-processing
- Rate limits: https://platform.claude.com/docs/en/api/rate-limits
- Errors and retries: https://platform.claude.com/docs/en/api/errors
- Optimizing for cost and intelligence: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
- Model migration guide (Claude Opus 5 delegation guidance): https://platform.claude.com/docs/en/about-claude/models/migration-guide
- Claude Managed Agents overview: https://platform.claude.com/docs/en/managed-agents/overview
- Managed Agents multi-agent orchestration: https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration
- Managed Agents outcomes: https://platform.claude.com/docs/en/managed-agents/define-outcomes
- Claude Agent SDK (Python): https://github.com/anthropics/claude-agent-sdk-python
