# Course dataset — Kestrel Pumps & Controls (fictional)

Every lab in the course uses one coherent, fictional company so that skills compound:
the tickets you classify on Day 1 mention orders your Day 2 agent looks up, whose
warranty rules your Day 3 retrieval finds, and whose outcomes your Day 6 evals check.

**"Today" is 2026-09-15** in every dataset. All people, companies, and email domains are
fictional (`.example` domains are reserved and never resolve).

## Regenerating

```bash
python data/generate_data.py      # deterministic: same seed -> byte-identical files
```

Hand-written files (✍️) are committed as-is; generated files (⚙️) are rebuilt by the script.
`data/anchors.json` maps scenario keys (e.g. `late_customs`) to concrete IDs (order, invoice,
RMA, tracking number...) — tests and solutions use it instead of hard-coding IDs.

## Files

| Path | Kind | Used on | What it is |
|---|---|---|---|
| `company/company_profile.md` | ✍️ | all | Who Kestrel is, sites, tiers, support org, business goals and constraints |
| `company/policies/*.md` | ✍️ | 2, 3, 4, 6, 7 | Warranty (WAR-001), returns/refunds (RET-002), shipping (SHP-003), safety & escalation SOP, data privacy (PRV-004), AP invoice matching (FIN-AP-010) |
| `support/triage_guidelines.md` | ✍️ | 1 | Labeling rubric: 10 categories, P1–P4, fields to extract |
| `support/tickets.jsonl` | ⚙️ | 1, 2, 6 | 62 inbound support emails (en/es/de), incl. traps: false lateness claim, lookalike-domain phishing, prompt injections |
| `support/ticket_labels.jsonl` | ⚙️ | 1, 6 | Ground truth for each ticket: category, priority, product_line, order_id, sentiment, requires_human, language |
| `kestrel_ops.db` | ⚙️ | 2, 5, 6, 7 | SQLite "Atlas ERP" extract (schema below) |
| `manuals/*.md` | ✍️ | 3, 5, 7 | IOM manuals (KP-250, KP-400), KC-1 controller manual (fault codes), vibration guide, seal failure guide |
| `maintenance/assets.csv` | ⚙️ | 3, 4 | 12 monitored pumps at 3 customer sites |
| `maintenance/telemetry.csv` | ⚙️ | 3, 4 | 8,640 hourly readings (30 days × 12 pumps) with 5 injected faults |
| `maintenance/work_orders.csv` | ⚙️ | 3, 4 | Maintenance history that explains some of the faults |
| `maintenance/ground_truth.json` | ⚙️ | 3, 4 | The 5 injected faults (for grading) |
| `finance/suppliers.csv`, `purchase_orders.json`, `goods_receipts.json` | ⚙️ | 4 | AP master data for three-way matching |
| `finance/invoices/*.txt` | ⚙️ | 4 | 20 supplier invoices as messy text (two layouts), incl. price/quantity/tax errors, a duplicate, and a bank-change fraud attempt with a prompt injection |
| `finance/expected_ap_outcomes.json` | ⚙️ | 4 | Ground-truth decision + exception codes per invoice |
| `quality/incident_reports/*.md` | ✍️ | 4 | 9 plant incident reports hiding two cross-plant supplier-lot problems plus red herrings |
| `ops_logs/order-portal/*.log` | ⚙️ | 5 | 48 h of JSON-lines logs from 4 microservices, containing one real incident |
| `ops_logs/deploys.csv`, `ops_logs/runbooks/*.md` | ⚙️/✍️ | 5 | Deploy history (with config diffs) and on-call runbooks |
| `ops_logs/incident_ground_truth.json` | ⚙️ | 5 | Root cause, timeline, red herrings |
| `evals/support_eval_set.jsonl` | ⚙️ | 6, 7 | 30 end-to-end support-agent scenarios (see schema below) |
| `evals/judge_calibration.jsonl` | ⚙️ | 6 | 24 candidate replies with human scores, for LLM-as-judge calibration |
| `anchors.json` | ⚙️ | tests | Scenario key → concrete IDs and facts |

## `kestrel_ops.db` schema (SQLite)

| Table | Key columns |
|---|---|
| `customers` | customer_id (`C-10xx`), name, tier (`strategic`/`key`/`standard`), country, contact_email, email_domain |
| `products` | sku, product_line, list_price_usd, warranty_months, lead_time_days, configurable, returnable |
| `inventory` | (sku, warehouse) on_hand, reserved |
| `orders` | order_id (`SO-1xxxx`), customer_id, order_date, status, promised_date, customer_po, total_usd |
| `order_lines` | (order_id, line_no) sku, qty, unit_price_usd, line_total_usd, configured, special_order |
| `shipments` | shipment_id, order_id, carrier, tracking_number, ship_date, eta_date, delivered_date, status, exception_reason |
| `invoices` | invoice_id (`AR-9xxxx`), order_id, amount_usd, paid_amount_usd, status, due_date |
| `rmas` | rma_id (`RMA-7xxx`), order_id, sku, qty, reason_code, status, refund_due_usd |
| `build_records` | serial_number, sku, order_id, build_date, plant, seal_lot, board_lot |
| `refunds`, `escalations`, `audit_log` | empty; written by agent tools (on a scratch copy) |

The database is a *representative sample* of Kestrel's ERP (312 orders), not the full history.

## `evals/support_eval_set.jsonl` schema

```json
{"id": "E13", "type": "policy_edge", "from_email": "...", "message": "...",
 "expect": {"tools_required": ["escalate_to_human"],      // all must be called
            "tools_forbidden": ["issue_refund"],           // none may be called
            "outcome": "escalated",                        // info_only | rma_created | refund_issued | escalated | declined | flagged | needs_verification
            "escalation_queue": "support_manager",         // optional
            "must_include": ["9,188.50|9188.50", "approv"],// case-insensitive; "a|b" = either alternative
            "must_not_include": ["refund has been issued"]}}
```

Tool names refer to `kestrel.support_tools` (the course's reference support-agent toolset).
