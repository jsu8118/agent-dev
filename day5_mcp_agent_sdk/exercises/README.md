# Day 5 exercises — MCP and the Claude Agent SDK

Twelve exercises: four concept checks, one security review, three design scenarios and four
hands-on tasks. Worked answers are in [`../solutions/README.md`](../solutions/README.md); the
hands-on tasks (and the security review) have runnable solutions in `../solutions/`.

Starter files for the hands-on tasks are in this directory. They run as-is (in mock mode, no key
needed) and print `TODO` notes where your code goes:

```bash
python day5_mcp_agent_sdk/exercises/ex09_asset_resource.py
python day5_mcp_agent_sdk/exercises/ex10_rma_approval.py
python day5_mcp_agent_sdk/exercises/ex11_secret_guard_hook.py
python day5_mcp_agent_sdk/exercises/ex12_tool_runner_controls.py
```

---

## Concept checks

### Exercise 1 — Choose the transport

Kestrel wants the `kestrel-plant-ops` server (lab 01) available in three places:

a. on each field engineer's laptop, inside Claude Code;
b. for five internal agent services running on Kestrel's Kubernetes cluster;
c. for an agent that calls the Claude API with the **MCP connector** (`mcp_servers` + `mcp_toolset`).

For each, choose **stdio** or **Streamable HTTP** and explain the consequences for: process
lifecycle, authentication, scaling and failure modes. In (b), would you run the server stateful
(sessions) or stateless, and why? What does the 2026-07-28 protocol revision change about that answer?

### Exercise 2 — Tool, resource or prompt?

Decide which MCP primitive should expose each capability, and say *who* is in control of it
(model, application or user) and what follows from that:

a. the KP-250 installation & maintenance manual;
b. current stock for a SKU;
c. "open an RMA for this order line";
d. the monthly "reliability review" checklist that engineers run with the assistant;
e. the list of monitored assets at a customer site (changes a few times a year).

A colleague argues "just make everything a tool — the model can call what it needs". When is
that argument right, and when is it wrong?

### Exercise 3 — Why hooks beat prompts for enforcement

Lab 05 protected the answer key three ways: a sentence in the system prompt, a deny rule
(`disallowed_tools=["Read(./incident_ground_truth.json)"]`), and (in lab 06) a `PreToolUse` hook.

1. Which of the three are *enforcement* and which is *guidance*? Why?
2. Give three distinct ways a prompt-only rule fails in production.
3. Where does a `PreToolUse` hook itself stop protecting you? Name at least three gaps and the
   control that closes each one.

### Exercise 4 — Predict the permission outcome

An agent runs with

```python
ClaudeAgentOptions(
    tools=["Read", "Write", "Edit", "Bash"],
    allowed_tools=["Read"],
    disallowed_tools=["Bash(rm *)"],
    permission_mode="bypassPermissions",
    hooks={"PreToolUse": [HookMatcher(matcher="Write", hooks=[deny_all_writes])]},
    cwd="/srv/agent-workspace",
)
```

For each call, say whether it runs, is denied, or reaches `can_use_tool`, and which step of the
evaluation order decides it:

1. `Bash("rm -rf build/")`
2. `Bash("/bin/rm -rf build/")`
3. `Write("notes.md", ...)`
4. `Edit("config.yaml", ...)`
5. `Read("/etc/passwd")`

Then change **one** option so that call 2 and call 4 are denied too, and explain why
`allowed_tools=["Read"]` did not do what its author probably expected.

---

## Security review

### Exercise 5 — Review a third-party MCP server

The field-service team wants to add a community MCP server, **docs-helper**, to Claude Code so
engineers can summarize PDF manuals. Kestrel IT reviewed and approved version 1.2.0 last month
(`exercises/data/docs_helper_tools_v1.json`). Today the server auto-updated to 1.3.1; its
`tools/list` response is in `exercises/data/docs_helper_tools_v2.json`. Open both files (view
the raw JSON, not a rendered view).

1. List every security problem you can find in v2 and in the way the server is installed.
   For each: name the attack class, what an attacker gains, and its severity for Kestrel.
2. Which problems would a human reviewer of the *v1* listing have missed, and why?
3. Propose mitigations at three levels: host configuration (Claude Code / Agent SDK / your own
   host), process (how Kestrel approves and monitors MCP servers), and protocol/platform features.
4. Write a one-page checklist Kestrel can use to review any third-party MCP server.
5. *Hands-on (optional):* write a script that flags suspicious tool descriptions and detects
   changes between two `tools/list` snapshots (the solution has one: `solutions/ex05_audit_mcp_server.py`).

---

## Design scenarios

### Exercise 6 — Expose Kestrel's ERP to five agent teams

Five teams want ERP access for their agents: (1) the customer-support agent (Day 2; talks to
customers), (2) the accounts-payable agent (Day 4; can release payments), (3) field engineers'
assistants in Claude Code/Desktop, (4) the SRE incident agent (this day), (5) a BI reporting
agent. Should Kestrel build **one MCP server**, **several MCP servers**, or ship the tools as a
**Python library** each team imports (direct tool definitions)? Design the authentication and
authorization model end to end: who authenticates whom, what a token looks like, how scopes map
to tools, how user identity flows (or doesn't), and what gets audited. Name the two things you
would refuse to do.

### Exercise 7 — Pick the harness

For each Kestrel project, choose **single call / workflow**, **manual agent loop**, **Tool Runner**,
**Claude Agent SDK**, or **Claude Managed Agents**, and justify it with the harness-vs-deployment
split, cost and operational ownership:

a. classify the 1,900 support tickets that arrive each month into 10 categories, overnight;
b. the customer-facing chat agent in the Kestrel Connect portal (latency < 30 s, cost < $0.40 per ticket,
   strict tool set, per-customer identity);
c. the SRE incident investigator from labs 05–07, running inside Kestrel's on-call environment next
   to the logs;
d. a weekly "reliability analyst" that should run on a schedule without anyone's laptop, keep notes
   between runs, and produce a report file.

### Exercise 8 — Production-harden the SRE agent

Your manager wants lab 06's agent to run automatically on every PagerDuty alert. List what you
would add or change before that happens: isolation, credentials, limits, observability,
evaluation, human hand-off. One risk deserves special attention: the agent reads log lines that
contain **user-controlled strings** (request paths, headers, error messages). Show a concrete
attack through the logs and how your design contains it.

---

## Hands-on

### Exercise 9 — Add a resource template

Extend the plant-ops server (lab 01) with a resource template `kestrel://assets/{asset_id}` that
returns JSON: the asset's record from `data/maintenance/assets.csv` plus its five most recent work
orders. Requirements:

* unknown asset IDs produce a resource error that lists valid IDs (not a crash);
* path-like values such as `../secrets` are rejected;
* `resources/templates/list` shows the template with a description and MIME type;
* bonus: a completion handler, so hosts can autocomplete `asset_id`.

Test it in-process with `Client(server)`. Starter: `exercises/ex09_asset_resource.py`.

### Exercise 10 — A tool with input validation and an approval step

Add a `request_rma(order_id, sku, qty, reason)` tool to the plant-ops server that:

* validates types and ranges declaratively (`reason` is one of four values, `1 <= qty <= 500`);
* validates business rules in code (order exists, SKU is on the order, qty ≤ ordered qty, and the
  order was delivered) with error messages that tell the caller how to recover;
* asks a **human** to approve before it "creates" anything, using MCP **elicitation** — the server
  asks, the host shows the question to the user, and the answer comes back to the tool;
* never writes to `data/` (return a draft RMA; keep any state under `.runs/`).

Demonstrate approve, decline and invalid-input paths with a host that answers elicitation
requests. Where else could the approval live (hint: two other layers you met today) and when would
you prefer each? Starter: `exercises/ex10_rma_approval.py`.

### Exercise 11 — A hook that blocks reading secrets

Write a `PreToolUse` hook for the Agent SDK that denies any `Read` of a file whose *content*
contains the word "secret" (case-insensitive), and apply the same policy to `Grep` so the agent
cannot read the file's lines another way. Unit-test the hook function directly, then run an agent
over a scratch workspace that contains one such file and confirm that the file's content never
reaches the model. What can this hook *not* catch? Starter: `exercises/ex11_secret_guard_hook.py`.

### Exercise 12 — Convert lab 03 to the Tool Runner, keeping your controls

Lab 03's manual loop let you see and control every tool call. Rebuild it on
`client.beta.messages.tool_runner`, **without** the `async_mcp_tool` helper, so that you keep:

* an allow-list of MCP tools (least privilege per task);
* a per-run tool-call budget (the model gets a clear error when it is exhausted);
* per-call logging with latency and result size, and a cap on result size;
* an approval gate for any MCP tool not annotated `readOnlyHint: true`.

Starter: `exercises/ex12_tool_runner_controls.py`.
