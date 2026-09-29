# Authoring guide for the advanced course

This is the contract every day of `advanced/` is written against. It exists so that seven days written by
different hands read as one course, run under one test harness, and never quote a number their scripts do
not print. Read it fully before writing a line; the last section is the report you hand back.

## 1. The bar

The learner is an industrial practitioner who already took the first course (`../README.md`) and now owns an
agentic system in production. They want depth **and** pragmatism: for every concept, the idea, the context it
comes from, when to use it, how it compares with the neighbouring options, and what it looks like in a real
system - never a pedantic survey and never copy-paste snippets without the reasoning. Concretely, each day is:

* one **lesson** (`README.md`, ~600-900 lines) with six to eight concept sections, a Kestrel case study, and a
  walkthrough of every lab that quotes the lab's real output;
* **seven labs** (`labs/01_*.py` .. `labs/07_*.py`), each a runnable, step-by-step script that prints what it
  teaches, works offline in mock mode and against Claude in live mode, and ends with a usage/cost summary;
* **exercises** (`exercises/README.md`, 10-12 of them: concept checks, calculations, design scenarios and
  hands-on coding with starter files) with **detailed solutions** (`solutions/README.md` + runnable `solutions/exNN_*.py`);
* everything tested: `python -m pytest tests/test_labs.py -q -k "advanced/dayN"` runs every lab and solution.

Match the first course's tone and density: read `day3_context_rag_memory/README.md`, its `labs/02_prompt_caching.py`
and `exercises/README.md` before you start - they are the reference for structure, headings and voice.

## 2. Where things go

```
advanced/dayN_<theme>/
  README.md                 the lesson
  labs/
    01_<topic>.py ... 07_<topic>.py
    _dayN.py                shared helpers for this day's labs (underscore = not a script; imported with sys.path)
  exercises/
    README.md               the exercises
    exNN_<topic>.py         starter files for hands-on exercises (must run and print "TODO" markers, not crash)
  solutions/
    README.md               worked answers to every exercise
    exNN_<topic>.py         runnable solutions
advanced/mock_scenarios/dayN_<theme>.py   the mock policies for this day (the ONLY file you create outside your day)
```

Day directory names are fixed: `day1_durable_agents`, `day2_tools_at_scale`, `day3_long_horizon_context`,
`day4_orchestration_at_scale`, `day5_security_engineering`, `day6_eval_science_release`, `day7_capstone`.
Do not create or edit files anywhere else (labkit, kestrel, advanced/lib, advanced/data, tests, docs, other
days). If you need a change there, describe it precisely in your report; the integrator applies it.

## 3. Lesson format (`README.md`)

```
# Day N - <Theme>
<two or three paragraphs: what this day is about, why it matters after go-live, how it builds on the first course>
## Learning objectives            (bullet list, "you can ..." statements)
## Agenda (about 7 hours)          (table: block, minutes, what)
## 1. <Concept>                    (six to eight numbered sections)
   - the idea in one paragraph; the context it comes from (which failure it prevents; where the pattern is
     borrowed from: databases, distributed systems, SRE, statistics ...)
   - when to use it and when not to; a **comparison** with the neighbouring options, ideally as a table
   - how it works underneath (the request/response shapes, the accounting, the failure modes)
   - a Kestrel example
## Case study: <Kestrel system>     (the day's running system: requirements, design, what went wrong, what changed)
## Lab walkthrough                  (one "### Lab NN - `NN_file.py`: title" per lab: what it does, how to run it,
                                    what to observe, and an excerpt of its REAL mock-mode output in a code block)
## Key takeaways                    (8-12 bullets)
## Further reading                  (official docs pages you have actually seen in this repo or the claude-api
                                    skill files; no invented URLs)
```

Rules for prose: explain before you show; every code block is either real output or code that exists in a lab;
API facts must match the request shapes in section 7 (they were verified against the SDK and the platform docs);
name the beta header wherever a beta surface is used and say what changes when the beta ends; prefer tables for
comparisons; avoid marketing adjectives.

## 4. Lab format

```python
"""Lab NN - <Title>: <one-line promise>.

Objective
    <what the learner builds and sees, 3-5 lines>

Concepts
    <comma-separated list of the concepts exercised>

Run
    python advanced/dayN_<theme>/labs/NN_<topic>.py [--quick]

What to observe
    * <3-6 bullets that point at specific printed lines>
"""
# test: expect=<a string the output must contain>
# test: args=--quick            (optional; keep the test run under ~60 s)
# test: timeout=180             (optional)

from __future__ import annotations
...
from labkit import MODEL, get_client, header, is_mock, step, text_of, wrap
import _dayN as dN          # after sys.path.insert(0, str(Path(__file__).resolve().parent)) if run from elsewhere
```

* Scripts run from any working directory (`python advanced/day1_durable_agents/labs/01_x.py` from the repo root
  is the documented way; tests run them with `cwd=script.parent`). Resolve data paths with `labkit.REPO_ROOT`.
* Every script exits 0 in mock mode without an API key and prints its own `[mock]` note wherever the mock's
  behaviour is a stand-in for what a model would do (`from labkit import is_mock`).
* Structure the output with `header()` and `step()`; keep each lab's mock-mode wall-clock under ~60 s (use
  `--quick`/`--limit` arguments and the `# test: args=` directive if the full run is longer).
* Scratch files go under `labkit.runs_dir("advanced", "dayN", ...)` (inside `.runs/`, gitignored). Never write
  into the data directories.
* Determinism: quoted output must be stable across runs in mock mode. Print wall-clock timings only when the
  lesson needs them, and never quote a timing line in the README as if it were exact.
* Cost: the metering middleware prints a usage/cost summary at exit for free; don't reimplement it. Read
  `labkit.LEDGER` when a lab needs totals mid-run (`LEDGER.total_cost`, `LEDGER.total_calls`, `LEDGER.by_model`; see `labkit/metering.py`).
* Live mode: assume `claude-opus-5` (`labkit.MODEL`) unless the lesson is about a model difference; then name the
  model explicitly (`claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5`, `claude-haiku-4-5`).
* No network calls other than the Claude client. No paid services. No secrets in files. All example domains
  end in `.example`; all companies and people are the fictional ones in the dataset.
* Helper modules (`_dayN.py`) hold shared tool definitions, data loaders and printing helpers, not lab logic.

## 5. Exercises and solutions

`exercises/README.md`: numbered exercises of four kinds - concept checks, calculations (with the price table
stated), design scenarios (a Kestrel situation; "design X, justify Y"), and hands-on coding with starter files
(`exercises/exNN_*.py`, which run and print what is left to do). `solutions/README.md` answers every exercise in
full - the reasoning, not just the answer; for calculations show the arithmetic; for design scenarios give the
recommended design and the alternatives you rejected and why. Runnable solutions `solutions/exNN_*.py` carry the
same docstring/`# test:` conventions as labs and import the exercise's starter where sensible.

## 6. labkit in one page

| Need | Use |
|---|---|
| a client | `from labkit import get_client; client = get_client()` (sync) / `get_async_client()` - live if a key is set, else the mock; same `anthropic.Anthropic` class, so every SDK feature works |
| models | `MODEL` (`claude-opus-5`), `MID_MODEL`, `FAST_MODEL`; `get_spec(model)` gives prices/limits/capabilities; `fallback_kwargs(model, **kw)` adapts thinking/effort kwargs to a model |
| printing | `header(title)`, `step(n, title)`, `show_message(msg)`, `text_of(msg)`, `print_json(obj)`, `wrap(text)` |
| cost | `cost_usd(usage, model)`, `usage_summary(usage)`; `LEDGER.total_cost`, `LEDGER.total_calls`, `LEDGER.by_model` (all calls so far) |
| paths | `REPO_ROOT`, `DATA_DIR` (base dataset), `runs_dir(*parts)` (scratch) |
| tracing | `from labkit.tracing import Tracer` - `with tracer.span("agent.run") as s: ... s.record_llm(response)`; `tracer.render_tree()`, `tracer.export()` |
| dataset | `from labkit.data import ...` (base course); advanced files: `REPO_ROOT / "advanced" / "data" / ...` |
| mock controls | `from labkit import mock_api` - `mock_api().reset()`, `.inject_faults(429, 500)`, `.cache.ready_delay = 0.5`, `.request_log` (last 200 request bodies), `.last_scenario` |
| kestrel | `kestrel.support_tools.TOOLS` + `SupportDesk(requester_email, db=...)` (`desk.run(name, input) -> (content, is_error)`), `kestrel.support_agent.run_support_agent(client, message, requester_email, ...)`, `kestrel.policy`, `kestrel.kb.KnowledgeBase` |
| durable runtime | `from advanced.lib.durable import RunStore, DurableRunner, ToolContext, ApprovalRequired, Crash` (section 8) |

## 7. The mock API and how to write a scenario

The mock is a transport-level stand-in (`httpx` MockTransport) that validates every request with the real
API's rules (400s for invalid shapes, unsupported parameters, missing betas, unknown models ...) and then
answers from a **scenario policy**: a `match(req)` predicate plus a `respond(req) -> Reply` function. It does not
think. Your policy must derive everything it "says" from the request - tool results, documents, system text -
the way a model would, so the lab teaches the mechanics honestly.

```python
# advanced/mock_scenarios/day2_tools_at_scale.py
from labkit.mock import MockRequest, Reply, say, scenario, search_tools, tool, use_tools

@scenario("adv.day2.wide_agent", match=lambda r: "<adv_day2_wide_agent>" in r.system_text, priority=10)
def wide_agent(req: MockRequest) -> Reply:
    if not req.tool_search_queries:                                   # discover before calling
        return Reply(content=[search_tools("shipment|track")])
    if not req.called("track_shipment"):
        return use_tools(tool("track_shipment", tracking_number="NLF1972237794"), preface="Checking the carrier.")
    result = req.calls("track_shipment")[-1].result_json() or {}
    return say(f"Your shipment is {result.get('status', 'in transit')}.")
```

Conventions: match on a marker tag you put in the lab's system prompt (`<adv_dayN_topic>`), optionally plus a
tool name (`r.has_tool("x")`); one scenario per lab (or per mode of a lab); `priority=10` beats the first
course's policies; register everything in the single module `advanced/mock_scenarios/dayN_<theme>.py` (loaded
automatically). If no policy matches, the generic fallback answers honestly (`[mock] ... no scenario policy
matched`) - a lab that prints that is a bug.

**`MockRequest` helpers** (all read-only views of the request body):
`model`, `spec`, `max_tokens`, `stream`, `messages`, `betas`, `system_text`, `tools` (all, deferred included),
`tool_names`, `has_tool(*names)`, `deferred_tool_names`, `loaded_tool_names` / `is_loaded(name)` (what the model
can see now: non-deferred + discovered + added - removed), `inline_tool_definitions`, `tool_definition(name)`,
`code_callable_tools`, `container_id`, `tool_choice`, `output_config`, `output_schema`, `effort` (per-message
override aware), `task_budget`, `system_messages` (clear_at aware), `thinking`, `thinking_active`,
`thinking_display`, `texts(role)`, `first_user_text`, `last_user_text`, `conversation_text`, `tool_calls`
(`ToolCall(id, name, input, result, is_error)` with `.result_json()`), `calls(name)`, `called(name)`,
`last_tool_results`, `is_tool_result_turn`, `assistant_turns`, `code_results`, `completed_code`,
`tool_search_queries`, `pending_code_calls`, `documents`, `search(pattern, text=None)`, `mentions(*words)`.

**Reply builders** (`from labkit.mock import ...`): `say(text, thinking=None)`, `use_tools(*tool(...), preface=None)`,
`tool(name, **input)`, `json_reply(obj)`, `refuse(category)`, `cite(doc, quote)` / `cited_text(text, citations)`,
`Reply(content=[...], stop_reason=None, progress=[...])` and the server-tool blocks `search_tools(query, limit=None)`,
`run_code(code)`, `bash(command)`, `create_file(path, text)`, `view_file(path)`; `Reply(...).then(lambda results: Reply)`
continues after server tools ran (`results[-1]["content"]["stdout"]`). The renderer assigns ids, thinking blocks,
usage, cache accounting and `stop_reason`, and truncates at `max_tokens`.

### 7.1 Surfaces this course uses - exact shapes and what the mock does

Everything below was verified against SDK 1.8 and the platform docs; use these shapes verbatim in labs and lessons.

**Tool search + deferred loading (GA, no beta).** Declare `{"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}`
or `..._bm25_20251119` and mark catalog tools `defer_loading: True` (at least one tool must stay non-deferred).
Deferred tools cost no input tokens until discovered. The model's search appears as a `server_tool_use`
(`name: "tool_search_tool_regex"|"tool_search_tool_bm25"`, `input: {"query", "limit"?}`) followed by a
`tool_search_tool_result` whose `content.tool_references` name the discovered tools (regex over name+description,
or BM25); `tool_search_tool_result_error` with `error_code: invalid_tool_input` for a bad regex. Never send a
`tool_result` for a `srvtoolu_` id. The mock: `search_tools(...)` in a policy; the request is re-dispatched to
your policy after the search so the same policy can then call the discovered tool; calling an undiscovered
deferred tool is a 400. `client.beta.messages.count_tokens(...)` shows the token difference.

**Mid-conversation tool changes** (`betas=["mid-conversation-tool-changes-2026-07-01"]`): a `{"role": "system",
"content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "x"}}]}` message (or
`tool_removal`) changes availability from that point on **without invalidating the cached prefix** (the mock
accounts for this: `cache_read_input_tokens` survives). Inline definitions (`{"type": "tool_definition",
"definition": {...}}`) need `inline-tools-2026-09-15`. Compare with re-sending a different `tools` array, which
invalidates everything after the tools block.

**Code execution + programmatic tool calling (GA).** `{"type": "code_execution_20260120", "name": "code_execution"}`
(also `20250825` / `20260521`). A client tool with `allowed_callers: ["code_execution_20260120"]` may be called
from the model's Python as `await tool_name({...})` (returns the tool result as a string). The response then
pauses with `stop_reason: "tool_use"` and `tool_use` blocks carrying `caller: {type: "code_execution_20260120",
tool_id: <the server_tool_use id>}`; you answer with a user message containing **only** `tool_result` blocks and
pass `container=response.container.id` (a 400 without it). When the cell finishes, the response contains a
`code_execution_tool_result` (`content.stdout/stderr/return_code`) and continues. `allowed_callers` without a code
execution tool is a 400. Mock: `run_code(code)`; the policy is dispatched again with `req.completed_code` set when
the cell completes; `bash(...)` and `create_file/view_file` run for real in a scratch container under `.runs/`;
`container_upload` blocks mount Files API uploads. Containers expire (`container.expires_at`) and persist state
between turns.

**Files API (stable, `client.files.*`)**: `upload(file=(name, bytes, mime))`, `list()`, `retrieve_metadata(id)`,
`download(id)`, `delete(id)`; use in a message as `{"type": "document", "source": {"type": "file", "file_id": id}}`.

**Per-message effort** (`betas=["mid-conversation-output-config-2026-07-01"]`): `{"role": "system", "content": [],
"output_config": {"effort": "low"}}` sets effort for the following turns (latest wins). Opus 5 and Fable 5.1
accept it; Fable 5 accepts system messages but not per-turn effort (400 "per-turn effort").

**Turn-scoped system messages** (`betas=["mid-conversation-system-clear-at-2026-08-21"]`): `{"role": "system",
"content": "...", "clear_at": "next_user_message"}` placed **after the user message it applies to** (never
directly before an assistant turn); it stays in the transcript but renders nothing (0 tokens) once a later user
message exists. `MockRequest.system_messages` shows what the model sees.

**Task budgets** (`betas=["task-budgets-2026-03-13"]`): `output_config: {"task_budget": {"type": "tokens", "total": N}}`,
N >= 20000; the model paces its thinking against the remaining budget across the turns you keep sending it in.

**Thinking**: `thinking: {"type": "adaptive"}` on every model in this course; `budget_tokens` is a 400 on the 5-series.
Progress updates: `thinking: {"type": "adaptive", "display": "updates"}` + `betas=["thinking-display-updates-2026-08-18"]`
puts a short thinking block before each `tool_use` (mock: `Reply(progress=["..."])`).

**Preserved thinking and binding.** Thinking blocks are signed to the producing model and, on new accounts for
Fable 5.1 and Opus 5.5, to the conversation prefix. Editing history before a thinking block (rewording an old
message, dropping a tool result, changing the system prompt or tool names) breaks the binding. The mock models:
`claude-fable-5-1` enforced - a prefix mismatch is a 400 (`prefix_mismatch`) unless
`thinking: {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}` under
`betas=["thinking-binding-controls-2026-08-01"]`, in which case the block is dropped and
`response.input_transformations` lists `{"type": "thinking_dropped", ...}`; `claude-opus-5-5` "recorded" - with the
beta header the response lists `thinking_mismatch_allowed` transformations and enforces only if you set
`prefix_mismatch_behavior`; `claude-opus-5` no prefix check. Model binding: Opus 5.5 blocks sent to Opus 5 are
dropped (`thinking_dropped`); Fable 5.1 can read Opus 5.5 blocks. Compaction blocks reset the prefix.

**Cache engineering.** Positions: tools -> system -> messages; consecutive tool_use/tool_result runs count as one
position for the 20-position lookback; deferred tools are outside the cached prefix until referenced. A written
entry becomes readable only when its writer has started responding (mock: `mock_api().cache.ready_delay`), so
N concurrent identical requests all write; pre-warm with `max_tokens=0` (content `[]`, `output_tokens` 0) before
fanning out. Minimum cacheable prefix per model (`get_spec(model).cache_min_tokens`).

**Managed Agents** (`client.beta.agents / environments / sessions / sessions.events / sessions.threads`; the SDK
sends `managed-agents-2026-04-01`): `agents.create(name, model, system, tools=[{"type": "custom", "name", "description",
"input_schema"} | {"type": "agent_toolset_20260401"}], multiagent={"type": "coordinator", "agents": [ids..., {"type": "self"}]})`
(versioned; one delegation level - a coordinator of a coordinator is a 400); `environments.create(name)`;
`sessions.create(agent=id, environment_id=..., title=..., initial_events=[...], budget={"type": "limit",
"max_list_cost": {"amount": "<cents>", "currency": "USD"}})`; `sessions.events.send(id, events=[{"type": "user.message",
"content": [{"type": "text", "text": ...}]}])` runs the turn synchronously in the mock; `events.list(id)` and
`events.stream(id)` (SSE replay) yield `user.message`, `agent.message`, `agent.thinking`, `agent.tool_use` /
`agent.tool_result` (built-in `bash/read/write/edit/glob/grep` run in `.runs/mock_sessions/<id>`),
`agent.custom_tool_use` -> `session.status_idle` with `stop_reason.type == "requires_action"` and `event_ids`,
answered by `user.custom_tool_result` (`custom_tool_use_id`, `content`), `span.model_request_start/end`,
`session.usage`, `session.thread_created`, `agent.thread_message_sent/received`, `session.status_idle` with
`end_turn` or `budget_reached`; `system.message`, `user.interrupt`, `user.define_outcome` accepted; `sessions.retrieve`
gives `usage` incl. `list_cost`; `threads.list` shows the coordinator's threads (`parent_thread_id`). The agent's
`system` marker is what your policy matches on. Vaults, deployments, outcomes, and hosted MCP are **not** mocked -
teach them in prose from the claude-api skill docs and mark them live-only.

**Batches, citations, structured output, MCP connector, memory tool, context editing/compaction, refusals,
faults (429/500/529 via `inject_faults`)**: as in the first course - see `labkit/mock/scenarios/*` for examples.

## 8. The shared library: `advanced/lib/durable.py`

`RunStore(path)` (SQLite; `:memory:` for tests) with `create(kind, input=, tags=, run_id=)`, `get`, `list`,
`set_status`, `append(run_id, type, payload)`, `events(run_id, types=)`, leases `acquire/heartbeat/release/stuck`,
effects `effect_begin/effect_finish/effect_reset`, approvals `request_approval/decide/approvals`.
`DurableRunner(store, client, model=, system=, tools=, execute=, max_turns=, max_tokens=, worker=, lease_ttl_s=,
create_kwargs=, crash_at=("after_model"|"before_tool"|"after_tool", turn))` with `run(run_id) -> Outcome`
(`status, reply, turns, replayed_tools, executed_tools, approval_id, messages`), `rebuild(run_id)`,
`resume_after_decision(run_id)`. The executor is `execute(name, input, ctx: ToolContext)`; use
`with ctx.effect() as eff: if eff.done: return eff.stored; if eff.in_flight: <check the system of record>; ...
eff.commit(result)` for at-most-once side effects, raise `ApprovalRequired({"summary": ...})` to pause a run.
`Crash` is a `BaseException` that simulates a worker dying. `tests/test_advanced_durable.py` shows every
guarantee in use. Day 1 teaches it by building it up; Day 4 and the capstone run on it. Do not edit it; request
changes in your report.

## 9. Data

| File | Contents |
|---|---|
| `advanced/data/tools/catalog.json` | 120 tools, 12 domains, `meta.{domain,risk,pii,approval_required,core}`; near-duplicates on purpose |
| `advanced/data/security/attacks.jsonl` | 30 attacks + 12 benign hard negatives with `expected.{action,must,must_not}` |
| `advanced/data/security/mcp_manifests/*.json` + `labels.json` | six MCP manifests, four with problems |
| `advanced/data/security/tenants.json` | tenants, roles, capability grants, approval roles |
| `advanced/data/traces/traces.jsonl` (+ `deployments.json`, `pairwise_judgments.jsonl`, `splits.json`) | 480 production traces, five arms, a planted regression (v15 escalates returns), 120 pairwise human preferences |
| `advanced/data/recall/*` | campaign RC-2026-03: 11 affected units, 7 customers' contacts, 6 engineers with slots, parts stock, 19 inbound replies (7 hostile) |
| `data/` (base course) | `kestrel_ops.db`, tickets, manuals, policies, eval sets - see `data/README.md` |

Read the trace files' observable fields only; `_truth` is for tests.

## 10. Testing before you report

```bash
python -m pytest tests/test_labs.py -q -k "advanced/dayN"     # every lab, starter and solution of your day
python -m pytest tests/test_advanced_mock.py -q                 # the mock surfaces you rely on
python advanced/dayN_<theme>/labs/NN_x.py                       # and read the output you are quoting
```

Then re-read your README against the outputs: every quoted line must appear in the script's current output.
A lab that only prints "[mock] no scenario policy matched" has no policy; fix it. Keep the whole day's tests
under ~6 minutes.

## 11. Rules

* Never commit or push. Never edit outside your day directory and your scenario module.
* No model identifiers or tool attribution anywhere in files; no real companies or people; `.example` domains.
* No new dependencies. Python 3.10+ syntax. Standard library plus what `requirements.txt` already installs.
* Don't disable TLS, don't touch proxies, don't create accounts.
* Prefer the SDK over raw HTTP; `client.beta.messages.create(...)` when a beta or beta-only field is used,
  `client.messages.create(...)` otherwise; `betas=[...]` for headers.
* When the mock cannot simulate something (vaults, real latency, model quality), say so in the lab's output and the
  lesson, and teach it from the docs rather than faking it.

## 12. Your report (the last thing you write)

1. Files created (paths) and a one-line description each.
2. Test results: the exact commands and their summary lines (`N passed`), plus the wall-clock of the day's suite.
3. Shared-change requests (if any): file, exact change, why - keep them minimal and self-contained.
4. Known gaps or judgement calls (what you simplified, what is live-only) so the integrator can review them.
