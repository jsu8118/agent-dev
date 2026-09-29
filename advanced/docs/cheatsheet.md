# Advanced cheat sheet (Python SDK `anthropic` 1.x)

The request shapes the second week uses, verified against the SDK and the platform docs on the course date.
The first course's [cheat sheet](../../docs/cheatsheet.md) has models, prices, `stop_reason`, tools, caching,
batches, citations and structured outputs. Betas go in `client.beta.messages.create(..., betas=[...])`.

## Tool search + deferred loading (GA)

```python
SEARCH = {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}   # or _bm25_20251119
tools = [SEARCH] + [dict(t, defer_loading=True) for t in CATALOG if not t["core"]] + [t for t in CATALOG if t["core"]]
msg = client.beta.messages.create(model=MODEL, max_tokens=4000, tools=tools, messages=msgs)
# assistant content: server_tool_use(name="tool_search_tool_regex", input={"query": ...}) -> tool_search_tool_result
#   (content.tool_references[].tool_name)  -> then ordinary tool_use blocks for discovered tools
# never send a tool_result for a srvtoolu_ id; at least one tool must stay non-deferred
client.beta.messages.count_tokens(model=MODEL, tools=tools, messages=msgs).input_tokens   # deferred tools cost 0
```

## Mid-conversation tool changes

```python
msgs += [{"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "book_visit"}}]}]
# or {"type": "tool_removal", "tool": {"name": "book_visit"}}
# or {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {...}}}   # + inline-tools-2026-09-15
client.beta.messages.create(..., betas=["mid-conversation-tool-changes-2026-07-01"])      # cached prefix survives
```

## Code execution + programmatic tool calling (GA)

```python
CODE = {"type": "code_execution_20260120", "name": "code_execution"}
tools = [CODE, {"name": "get_order", ..., "allowed_callers": ["code_execution_20260120"]}]
first = client.beta.messages.create(model=MODEL, max_tokens=4000, tools=tools, messages=msgs)
# stop_reason "tool_use": tool_use blocks with caller={"type": "code_execution_20260120", "tool_id": srvtoolu_id}
msgs += [{"role": "assistant", "content": first.content},
         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id, "content": ...} for c in calls]}]  # ONLY tool_results
second = client.beta.messages.create(..., messages=msgs, container=first.container.id)      # container required
# code_execution_tool_result: content.stdout / stderr / return_code; container.expires_at
```

## Files API (stable)

```python
f = client.files.upload(file=("notes.txt", data, "text/plain")); client.files.list(); client.files.download(f.id)
{"type": "document", "source": {"type": "file", "file_id": f.id}}         # in a user message
{"type": "container_upload", "file_id": f.id}                            # mount into the code container
```

## Effort, budgets, turn-scoped system messages, thinking display

```python
{"role": "system", "content": [], "output_config": {"effort": "low"}}     # per-message effort (latest wins)
#   betas=["mid-conversation-output-config-2026-07-01"]; Opus 5 / Fable 5.1 yes, Fable 5 no
output_config={"task_budget": {"type": "tokens", "total": 50000}}         # >= 20000; betas=["task-budgets-2026-03-13"]
{"role": "system", "content": "Check the inbox first.", "clear_at": "next_user_message"}
#   after the user message it applies to; betas=["mid-conversation-system-clear-at-2026-08-21"]
thinking={"type": "adaptive", "display": "updates"}                       # betas=["thinking-display-updates-2026-08-18"]
```

## Preserved thinking and binding

```python
thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}   # or "error"
#   betas=["thinking-binding-controls-2026-08-01"]; response.input_transformations -> [{"type": "thinking_dropped", "path": ...}]
# Fable 5.1: prefix binding enforced (400 prefix_mismatch) | Opus 5.5: recorded (thinking_mismatch_allowed) | Opus 5: none
# model binding: Opus 5.5 blocks -> Opus 5 dropped; Fable 5.1 reads Opus 5.5 blocks; compaction resets the prefix
# rule: append-only history (never reword, drop or reorder anything before a thinking block)
```

## Cache engineering

```
positions: tools -> system -> messages; lookback 20 positions; a tool_use/tool_result run = 1 position
concurrent identical requests all WRITE until the first writer starts responding -> pre-warm:
    client.messages.create(model=MODEL, max_tokens=0, system=SYSTEM, messages=[...])   # content [], output 0
minimum cacheable prefix: get_spec(model).cache_min_tokens; deferred tools sit outside the prefix until referenced
```

## Managed Agents (beta, SDK adds managed-agents-2026-04-01)

```python
agent = client.beta.agents.create(name="desk", model=MODEL, system=SYSTEM,
    tools=[{"type": "custom", "name": "lookup_order", "description": "...", "input_schema": {...}},
           {"type": "agent_toolset_20260401"}])                                   # bash/read/write/edit/glob/grep
lead = client.beta.agents.create(name="lead", model=MODEL, system=..., multiagent={"type": "coordinator", "agents": [agent.id, {"type": "self"}]})
env = client.beta.environments.create(name="lab")
s = client.beta.sessions.create(agent=agent.id, environment_id=env.id, title="t",
    budget={"type": "limit", "max_list_cost": {"amount": "2500", "currency": "USD"}})     # cents
client.beta.sessions.events.send(s.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "..."}]}])
for e in client.beta.sessions.events.list(s.id): ...    # agent.message, agent.custom_tool_use, session.status_idle(stop_reason.type)
client.beta.sessions.events.send(s.id, events=[{"type": "user.custom_tool_result", "custom_tool_use_id": use.id, "content": [...]}])
client.beta.sessions.events.stream(s.id); client.beta.sessions.retrieve(s.id).usage; client.beta.sessions.threads.list(s.id)
```

## The durable runtime (`advanced/lib/durable.py`)

```python
store = RunStore(".runs/durable.db"); run = store.create("support", input={"message": text}, run_id="support:T-1301")
runner = DurableRunner(store, client, model=MODEL, system=SYSTEM, tools=TOOLS, execute=execute, worker="w1")
outcome = runner.run(run.id)                     # status: completed | failed | waiting_approval; replayed_tools, executed_tools
def execute(name, input, ctx):                   # ctx.idempotency_key = "<run>:<tool_use>"
    with ctx.effect() as eff:
        if eff.done: return eff.stored
        if eff.in_flight: ...                    # ask the system of record by key
        return eff.commit(do_it(idempotency_key=ctx.idempotency_key))
raise ApprovalRequired({"summary": ...})         # parks the run; store.decide(id, approved=True, by=...); runner.resume_after_decision(run.id)
store.acquire / heartbeat / release / stuck      # leases; store.events(run.id) is the log; rebuild() is byte-for-byte
```

## Lab map

| Day | Labs |
|---|---|
| 1 durable agents | 01 event-sourced run · 02 crash and resume · 03 idempotent effects · 04 sagas · 05 approvals that wait · 06 leases and stuck runs · 07 replay and time travel |
| 2 tools at scale | 01 catalog cost · 02 regex vs BM25 search · 03 wide agent · 04 mid-conversation tool changes · 05 programmatic tool calling · 06 eager input streaming · 07 contracts and selection eval |
| 3 long-horizon context | 01 task budgets and effort · 02 compaction strategies · 03 preserved thinking binding · 04 turn-scoped system messages · 05 memory architectures · 06 subagent isolation · 07 cache engineering at scale |
| 4 orchestration at scale | 01 durable coordinator · 02 work queues and shared state · 03 budgets, breakers, loops · 04 agent-to-agent protocols · 05 Managed Agents sessions · 06 managed coordinator · 07 multi-agent eval and decision |
| 5 security engineering | 01 threat model · 02 injection defence in depth · 03 capability authorization · 04 sandboxing agent code · 05 MCP supply chain · 06 exfiltration channels · 07 red teaming and forensics |
| 6 eval science & release | 01 mining evals from traces · 02 CIs and paired tests · 03 pairwise judges and Bradley–Terry · 04 finding the regression · 05 prompt hill-climbing · 06 model × effort staircase · 07 canary, drift, rollback |
| 7 capstone | `run_campaign.py` · `run_ops.py` · `run_evals.py` · starter milestones M1–M7 |

## Commands

```bash
python -m pytest tests/ -k advanced -q                    # every advanced test, lab and solution (mock mode)
python -m pytest tests/test_labs.py -q -k "advanced/day3"  # one day
python advanced/data/generate_data.py                     # regenerate the advanced dataset (deterministic)
python scripts/smoke_live.py --only advanced --cap 5      # live smoke test with a cost cap
python advanced/day7_capstone/starter/run_starter_check.py   # capstone progress
```
