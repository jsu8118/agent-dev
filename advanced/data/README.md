# Advanced course dataset

Everything here extends the base course's Kestrel Pumps & Controls world (`data/`): the same customers,
orders, products and build records, read from `data/kestrel_ops.db` so the two never disagree. All files
are generated deterministically by `python advanced/data/generate_data.py` (seed in `generators/common.py`);
CI regenerates them and fails on drift.

| Path | Used on | What it is |
|---|---|---|
| `tools/catalog.json` | Day 2, Day 5, Day 7 | 120 tools in 12 domains (10 each) with full JSON schemas and `meta` (domain, risk `read/write/irreversible`, `pii`, `approval_required`, `core`). Near-duplicates are deliberate (`get_shipment` vs `track_shipment`, `issue_refund` vs `issue_credit_note`). |
| `security/attacks.jsonl` | Day 5 | 42 labelled cases: 30 attacks across channels (email, tool results, documents, MCP descriptions, web pages, memory) and 12 benign hard negatives that keyword filters get wrong. `expected` says what a correct agent does and which tool calls would mean the attack worked. |
| `security/mcp_manifests/` | Day 5 | Six MCP server manifests (two clean, four with a problem: typosquat + hidden instruction, exfiltrating parameter, over-broad scopes, rug pull) and `labels.json` with the verdicts. |
| `security/tenants.json` | Day 5, Day 7 | Tenants, roles, capability grants, row filters and approval roles for the authorization labs. |
| `traces/traces.jsonl` | Day 6 | 480 production traces of the Service Desk Copilot over five deployment arms (`traces/deployments.json`): tools, usage, latency, cost, outcome, and a reviewer's findings on 35%. `_truth.issues` is hidden ground truth for the tests; labs must mine the observable fields. |
| `traces/pairwise_judgments.jsonl` | Day 6 | 120 human A/B preferences over reply pairs, 30% double-annotated. |
| `traces/splits.json` | Day 6 | Ticket-level train / validation / test split. |
| `recall/*` | Day 4, Day 7 | Recall campaign RC-2026-03: notice with hazards, remedies, priorities, SLAs, budget and approvals (`campaign.json`); the 11 affected units read from `build_records` (`affected_units.json`); contacts, engineers with free slots, remedy-kit stock; and 19 inbound replies, 7 of them hostile, spoofed or injected (`inbound_replies.jsonl`). |
| `manifest.json` | CI | File list and counts for drift detection. |

Conventions: dates are ISO 8601, the course's "today" is 2026-09-15 (the recall is issued on the 16th),
money is USD, every email address ends in `.example`, and every identifier follows the base dataset's
formats (`SO-`, `AR-`, `RMA-`, `C-`, serials like `KP250-2608-0002`).
