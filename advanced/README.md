# Advanced Agent Engineering with Claude — a second one-week course for practitioners

The first course ([`../README.md`](../README.md)) taught you to build an agent and take it to a go-live review.
This one is about what happens **after** go-live, and at scale: agents that run for hours and survive crashes,
toolsets with a hundred tools, context that stays healthy over long horizons, swarms of agents that don't
multiply your bill, adversaries who read your prompts, and the statistics you need before you believe an
eval. Same fictional company (Kestrel Pumps & Controls), same dataset extended, same conventions: every
concept is taught with its context, alternatives and internals, and **every script runs** offline in mock
mode or against Claude in live mode.

> **Prerequisites.** The first course or equivalent experience: you have built a tool-using agent on the
> Messages API, used prompt caching, written an eval harness, and shipped something with an LLM in it.
> Day 1 of this course assumes `kestrel/support_agent.py`, `labkit` and the Kestrel dataset are familiar.

## Who this is for

* Engineers and architects who **own an agentic system in production** (or are about to) and need it to be
  durable, cheap at scale, secure against adversarial input, and provably improving.
* Comfortable with Python, SQL, concurrency and HTTP services. Statistics at "I once computed a confidence
  interval" level is enough; Day 6 fills the rest.

## What you will build

* a **durable agent runtime** (event-sourced runs, checkpoints, resumption, compensation, approvals that wait for days);
* a **120-tool agent** that discovers tools on demand, composes them in code, and streams large inputs;
* a **long-horizon field-service agent** with budgets, compaction strategies, structured memory, and thinking that survives model upgrades;
* a **multi-agent investigation swarm** with a durable coordinator, shared state, budgets, and its hosted twin on Managed Agents;
* a **security program** for agents: threat model, defense in depth, capability-scoped tools, sandboxing, MCP supply-chain controls, automated red-teaming;
* an **evaluation and release science kit**: eval sets mined from traces, paired tests and power, judges at scale, prompt hill-climbing with holdouts, canaries and drift detection, model-migration audits;
* a **capstone**: the *Recall Campaign Orchestrator*, a durable, multi-agent, secure, evaluated system that runs a supplier-defect recall across days and dozens of customers.

## Syllabus

| Day | Theme | You learn | Labs |
|---|---|---|---|
| **1** | [Durable agents](day1_durable_agents/README.md) | the agent run as an event-sourced state machine; checkpoints and resumption; idempotency and exactly-once effects; sagas and compensation; approvals that wait hours; leases, heartbeats, stuck-run detection; replay for debugging | 7 labs · durable support runs |
| **2** | [Tool engineering at scale](day2_tools_at_scale/README.md) | 120-tool catalogs and their context cost; tool search (regex/BM25) and deferred loading; mid-conversation tool changes vs re-sending tools; programmatic tool calling and code execution; eager input streaming; tool contracts, versioning and selection evals; capability-scoped toolsets | 7 labs · the wide agent |
| **3** | [Long-horizon context](day3_long_horizon_context/README.md) | task budgets and per-turn effort; compaction strategies compared; preserved thinking (prefix and model binding) and how to keep a harness compatible; turn-scoped system messages; memory architectures with consolidation; subagent isolation; cache engineering at scale (concurrency, pre-warming, lookback, multi-tenant prefixes) | 7 labs · field-service agent, 40 turns |
| **4** | [Multi-agent orchestration at scale](day4_orchestration_at_scale/README.md) | durable coordinators and work queues; shared state and conflict resolution; swarm budgets, circuit breakers, loop detection; agent-to-agent protocols and handoffs; Managed Agents (agents, sessions, events, custom tools, coordinators); evaluating multi-agent systems; the single/multi/hosted decision with numbers | 7 labs · fleet investigation swarm |
| **5** | [Security engineering for agents](day5_security_engineering/README.md) | threat modeling for agents; injection defense in depth measured on an attack corpus; capability-based authorization and dual control; sandboxing agent-written code; MCP supply chain (rug pulls, pinning, manifests); exfiltration channels; automated red-teaming and forensics from traces | 7 labs · the hardened copilot |
| **6** | [Evaluation science & release engineering](day6_eval_science_release/README.md) | evals mined from production traces; confidence intervals, paired tests, power, pass^k; pairwise judges, Bradley–Terry, judge drift; prompt hill-climbing with holdouts; the model × effort staircase and caching health; model-migration audits; shadow/canary analysis, drift detection, rollback | 7 labs · release gate |
| **7** | [Capstone](day7_capstone/README.md) | the Recall Campaign Orchestrator: durable multi-day workflow, outreach and scheduling agents, adversarial inbound mail, budgets, evals, operations | project + reference solution |

Each day contains a **lesson** (`README.md`), numbered **labs**, **exercises** and detailed **solutions**.
Cross-cutting references: [advanced cheat sheet](docs/cheatsheet.md) · [decision tables](docs/comparisons.md) ·
[glossary](docs/glossary.md) · [troubleshooting](docs/troubleshooting.md) · [final assessment](docs/final_assessment.md).

A suggested pace is one day per theme (≈ 7 hours each). Days 1–3 build the runtime, tools and context skills the
later days assume; Days 4–6 can be taken in any order after them.

---

## Setup

Everything from the first course applies (`pip install -r requirements.txt` at the repository root, or the Docker
image). The advanced course adds nothing to install: it lives in this directory and uses the same `labkit` toolkit,
whose mock API grew the surfaces this course needs.

```bash
python advanced/day1_durable_agents/labs/01_event_sourced_run.py      # mock mode until you add a key
python -m pytest tests/ -k advanced -q                                # every advanced lab and solution, in mock mode
python advanced/data/generate_data.py                                 # regenerate the advanced dataset (deterministic)
```

### What the mock simulates for this course

The mock API from the first course (transport-level, validating requests with the real API's rules) gained:

| Surface | Mock behaviour |
|---|---|
| Tool search (`tool_search_tool_regex` / `_bm25`) with `defer_loading` | deferred tools cost no tokens until discovered; `tool_search_tool_result` blocks with `tool_reference`s; undiscovered tools can't be called |
| Mid-conversation tool changes (`tool_addition` / `tool_removal`, inline definitions) | cache-preserving; availability tracked per turn; the runner's `add_tools`/`remove_tools` work |
| Code execution and programmatic tool calling | a local sandbox runs the (mock-written) Python; tool calls from code pause the container exactly like the API (`caller` on `tool_use`); containers persist state |
| Files API (`client.files.*`) | upload, list, retrieve, download, delete; `document` blocks by `file_id` |
| Task budgets, per-message `effort`, turn-scoped (`clear_at`) system messages, `thinking.display: "updates"` | validated and accounted for (tokens, cache) as the API documents |
| Preserved thinking | signatures bound to the producing model and the conversation prefix; `prefix_mismatch_behavior`, `input_transformations`, model-binding drops |
| Cache engineering | concurrent-request timing (an entry is readable only once its writer has started responding), `max_tokens: 0` pre-warming, the 20-position lookback |
| Managed Agents (`client.beta.agents / environments / sessions / sessions.events`) | agents, environments, sessions, event history and SSE streams, custom-tool round trips, coordinator rosters with threads, session budgets |

As before, the mock does not think: answers come from rule-based scenario policies written for each lab. Use it to learn
mechanics offline; use live mode to judge quality, cost and latency.

**Live-mode cost.** The advanced labs run longer loops than the first course: expect a few dollars per day on Claude
Opus 5, about $6 for Day 4's swarms, and up to ~$30 for Day 3's full-length 40-turn runs (about $15 with `--turns 14`
on labs 01-02). Every lab prints its cost; `python scripts/smoke_live.py --only advanced --cap 5` runs a representative
subset with a hard cap.

## Repository layout (this course)

```
advanced/
  day1_durable_agents/ ... day7_capstone/   lessons, labs, exercises, solutions
  lib/                                     shared library: durable runtime, tool catalog, helpers
  data/                                    the advanced dataset (tool catalog, attack corpus, production traces,
                                           recall campaign, tenants) + deterministic generator
  mock_scenarios/                          rule-based mock policies for the advanced labs
  docs/                                    cheat sheet, decision tables, glossary, troubleshooting, final assessment
```

## Conventions worth knowing

* **Default model** is still `claude-opus-5` (`labkit.MODEL`). Labs use `claude-opus-5-5` or `claude-fable-5-1`
  explicitly where the lesson is about their differences (forced tool choice, preserved thinking, per-turn effort).
* **Betas are named where they are used.** Advanced surfaces are mostly beta: the labs pass the exact header and the
  lesson says what changes when the beta ends.
* **"Today" in the data is still 2026-09-15.** The recall campaign in the capstone starts on 2026-09-16.
