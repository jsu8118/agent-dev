# Agent Development with Claude — a one-week, hands-on course for practitioners

An in-depth, pragmatic course on building production AI agents with Claude, for engineers who already
ship software and want to **understand** agents deeply, not copy-paste them. Every concept is taught
with its context, use cases, a comparison with the alternatives, how it works under the hood, the
pitfalls seen in production, and a recipe. Everything is practised on one coherent case study with
realistic data, runnable labs, and exercises with detailed solutions.

> **Every script in this repository runs.** A test harness executes every lab and every solution on each
> change. You can run it all **offline and free** (mock mode) or **against Claude** (live mode).

---

## Who this is for

* **Intermediate to advanced practitioners**: software, data, ML or platform engineers; solution architects;
  technical leads evaluating agentic systems.
* Comfortable with Python and HTTP APIs, and have made at least a few LLM API calls. No prior agent experience required.

## What you will build

You join **Kestrel Pumps & Controls**, a fictional industrial manufacturer (pumps, valves, controllers,
spare parts; three plants; B2B customers from water utilities to data centers). Over the week you build its AI
systems on a shared dataset (ERP database, support inbox, manuals, telemetry, supplier invoices, incident
reports, service logs, evaluation sets):

* a **ticket-triage** pipeline with evaluation against labelled data;
* a **customer-support agent** with tools, policy enforced in code, approval gates and identity checks;
* a **field-service assistant** using caching, retrieval with citations, agentic search and memory;
* an **accounts-payable workflow** (three-way match) and a **multi-agent quality investigation**;
* a **plant-ops MCP server** and an **SRE incident-investigation agent** on the Claude Agent SDK;
* an **evaluation suite, guardrails, tracing and a containerized service** — and a **capstone** that ties it all together.

## Syllabus

| Day | Theme | You learn | Labs |
|---|---|---|---|
| **1** | [Foundations: from LLM calls to agents](day1_foundations/README.md) | single call vs workflow vs agent; the Messages API; models, tokens, cost; adaptive thinking and effort; structured outputs; streaming; errors, retries, refusals and fallbacks | 7 labs · ticket triage |
| **2** | [Tool use & the agent loop](day2_tools_agent_loop/README.md) | how tool use works; the manual loop and the Tool Runner; parallel tools; error handling; tool design as an interface; policy in code; human-in-the-loop; `tool_choice` and strict tools across models | 8 labs · order desk, support agent |
| **3** | [Context engineering: caching, retrieval & memory](day3_context_rag_memory/README.md) | context budgets; prompt caching; long context vs RAG vs agentic search; citations; retrieval evaluation; context editing and compaction; the memory tool | 7 labs · field-service assistant |
| **4** | [Workflow patterns & multi-agent systems](day4_workflows_multi_agent/README.md) | chaining, routing, parallelization, orchestrator–workers, evaluator–optimizer; multi-agent trade-offs; frameworks compared | 7 labs · AP automation, quality investigation |
| **5** | [MCP & the Claude Agent SDK](day5_mcp_agent_sdk/README.md) | the Model Context Protocol (servers, clients, transports, security); the Claude Agent SDK (built-in tools, hooks, custom tools, subagents); choosing a harness | 7 labs · plant-ops MCP server, SRE agent |
| **6** | [Evaluation, guardrails, observability & production](day6_evals_guardrails_production/README.md) | eval harnesses, LLM-as-judge calibration; layered guardrails and prompt injection; tracing and metrics; reliability; batching and cost; deploying a service | 8 labs · go-live review |
| **7** | [Capstone](day7_capstone/README.md) | integrate everything into a go-live-ready system: deterministic gates around an agent, acceptance criteria as executable gates, a staged rollout | project (6 milestones + docs) · reference solution, design doc, go-live memo |

Each day contains a **lesson** (`README.md`), numbered **labs**, **exercises** and detailed **solutions**.
Cross-cutting references: [cheat sheet](docs/cheatsheet.md) · [decision tables](docs/comparisons.md) ·
[glossary](docs/glossary.md) · [troubleshooting](docs/troubleshooting.md) · [final assessment](docs/final_assessment.md).

A suggested pace is one day per theme (≈ 7 hours: ~40% concepts, ~60% labs and exercises). Days 1–3 are
foundational; Days 4–6 can be taken in any order after them.

> **Finished this course?** The second week continues in [`advanced/`](advanced/README.md): *Advanced Agent
> Engineering with Claude* — durable agents, tools at scale, long-horizon context, orchestration at scale,
> security engineering, evaluation science and release engineering, and a multi-day recall-campaign capstone.
> Same dataset, same toolkit, same conventions; the mock API grew the surfaces it needs.

---

## Setup

### Option A — local Python (3.10+; tested on 3.11)

```bash
git clone <this repo> && cd <repo>
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt                          # includes the course's own packages (labkit, kestrel)
python day1_foundations/labs/01_first_call.py            # runs in mock mode until you add a key
```

### Option B — Docker (nothing to install but Docker)

```bash
docker build -t claude-agent-course .
docker run --rm claude-agent-course                      # the full test suite, in mock mode
docker run --rm -it --env-file .env -v "$PWD:/course" claude-agent-course bash
# or: docker compose run --rm course bash
```

Behind a TLS-inspecting corporate proxy, pass its CA certificate:
`docker build --build-arg EXTRA_CA_CERT="$(cat proxy-ca.pem)" -t claude-agent-course .`

### Live mode: your Claude API key

```bash
cp .env.example .env        # then set ANTHROPIC_API_KEY=...   (.env is git-ignored)
```

The only paid service the course uses is the **Claude API**. No cloud accounts, vector-database subscriptions or
other paid services are needed; everything else runs locally or in Docker.

| | Live mode | Mock mode |
|---|---|---|
| When | `ANTHROPIC_API_KEY` is set | no key (or `LABKIT_MODE=mock`) |
| Answers come from | Claude | `labkit`'s offline mock of the Claude API |
| Good for | real behaviour and quality numbers | learning mechanics offline, CI, testing your own agents |

The mock runs *under* the real Anthropic SDK (at the HTTP transport layer) and **validates requests with the
real API's rules**: message ordering, tool-result pairing, thinking-block integrity, per-model parameters,
schema rules, beta headers. It simulates prompt caching, context editing, compaction, batches, fallbacks and
fault injection. It does not think: answers come from small rule-based policies per lab. Use it to learn
mechanics; use live mode to judge quality.

Every run prints a **usage summary** (tokens and cost per model; simulated in mock mode). Useful overrides:

```bash
LABKIT_MODEL=claude-sonnet-5 python day1_foundations/labs/04_triage_pipeline.py    # main model (default claude-opus-5)
LABKIT_FAST_MODEL=claude-haiku-4-5 ...                                             # cheap model for routing/judging
LABKIT_QUIET=1 ...                                                                   # no banners/summaries
```

**Live-mode cost.** Labs are designed to be cheap: most cost cents; a full day on Claude Opus 5 typically stays
within a few dollars. `python scripts/smoke_live.py --cap 3` runs a representative subset of labs live with a hard
spend cap.

---

## Running and testing

```bash
make test                   # every lab and solution, in mock mode (≈ a few minutes)
make test-day D=3           # one day
python -m pytest tests/test_labkit_mock.py   # the mock API's own tests (it must behave like the real API)
python scripts/smoke_live.py --cap 3         # live smoke test (needs a key)
python data/generate_data.py                 # regenerate the dataset (deterministic)
python day7_capstone/starter/run_starter_check.py   # capstone progress, milestone by milestone
docker compose up copilot                    # the capstone's HTTP service on http://localhost:8080
make test-advanced                           # the advanced course only (its tests, labs and solutions)
```

## Repository layout

```
day1_foundations/ ... day7_capstone/   lessons, labs, exercises, solutions
advanced/                              the second week (see advanced/README.md): its own days, data, library, docs
data/                                  the Kestrel dataset (see data/README.md) + deterministic generator
kestrel/                               Kestrel's "internal library": policy engine, knowledge base, support tools & agent
labkit/                                course toolkit: client factory (live/mock), mock Claude API, metering, tracing
docs/                                  cheat sheet, decision tables, glossary, troubleshooting, final assessment
tests/                                 runs every script in mock mode; mock-API and domain-library tests
scripts/smoke_live.py                  live-mode smoke test with a cost cap
Dockerfile, docker-compose.yml, Makefile, .github/workflows/ci.yml
```

## Conventions worth knowing

* **Default model:** `claude-opus-5` (via `labkit.MODEL`). Cheaper models appear only where the lesson is about
  routing, judging or cost, and the course shows how to decide with measurements.
* **Current API only.** The course teaches today's API surface: adaptive thinking and `effort` (no
  `budget_tokens`), no `temperature` on current models, structured outputs instead of prefill, SDK 1.x
  (`httpx2`), MCP Python SDK 2.x (`MCPServer`), and server-side refusal fallbacks on Claude Opus 5.
  Where older tutorials differ, the lessons say so explicitly.
* **"Today" in the data is 2026-09-15.** All companies, people and domains are fictional (`.example`).

## Disclaimer

Kestrel Pumps & Controls, its customers, suppliers, employees, products and data are fictional and were generated
for teaching. Technical values in the manuals are simplified for training; don't use them to operate real equipment.
