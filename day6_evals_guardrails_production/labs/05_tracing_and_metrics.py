"""Lab 05 - Tracing and metrics: see inside every agent run, then prove the business constraints with data.

Objective
    Run ten representative inbox tickets through the support agent with a tracer, export the traces
    (JSON Lines + OTLP/JSON), build a metrics dashboard (latency percentiles, tokens, cost per ticket and
    per RESOLVED ticket, tool-error and escalation rates) and check it against Kestrel's go-live constraints:
    < $0.40 per ticket, replies < 30 s, safety to a human within 1 h, every action auditable.

Concepts
    Traces and spans (one trace per ticket: agent.run -> llm.call / tool.* spans); OpenTelemetry GenAI
    attribute names (gen_ai.usage.*, gen_ai.response.*); metrics derived from traces vs. counters;
    percentiles, not averages; cost per resolved ticket vs cost per request; what NOT to log (PII);
    exporting to a real backend (OTLP).

Run
    python day6_evals_guardrails_production/labs/05_tracing_and_metrics.py

What to observe
    * The trace tree of T-1207 shows WHY that ticket costs more: a refused issue_refund, then an escalation.
    * "Cost per resolved ticket" is higher than "cost per ticket": escalated tickets cost tokens AND a human.
    * The auditability and safety checks are computed from the system of record and the spans, not asserted.
    * The PII check FAILS: requester email addresses sit in span attributes.  Exercise 10 adds a redactor.
    * In mock mode latency is local compute only (milliseconds); the per-call latency BUDGET is what to
      verify when you run live.
"""

# test: expect=Business constraints
# test: expect=cost per resolved ticket

from __future__ import annotations

import json

import _evalkit as ek
import _telemetry as tm
from kestrel.support_agent import run_support_agent
from kestrel.support_tools import SupportDesk
from labkit import DATA_DIR, MODEL, get_client, header, is_mock, runs_dir, step
from labkit.data import scratch_db
from labkit.tracing import Tracer

TICKETS = ["T-1001", "T-1101", "T-1201", "T-1206", "T-1207", "T-1301", "T-1403", "T-1507", "T-1801", "T-1006"]
WRITE_ACTIONS = ("create_rma", "issue_refund", "escalate_to_human")
LIMITS = {"cost_per_ticket_usd": 0.40, "reply_latency_s": 30.0}


def run_ticket(client, ticket: dict, out_dir) -> tuple[dict, Tracer, SupportDesk]:
    tid = ticket["ticket_id"]
    tracer = Tracer("support-agent")
    desk = SupportDesk(ticket["from_email"], db=scratch_db(f"day6_lab05_{tid}.db"), ticket_ref=tid)
    message = f"Subject: {ticket['subject']}\n\n{ticket['body']}"
    run_support_agent(client, message, ticket["from_email"], desk=desk, tracer=tracer, ticket_ref=tid)
    tracer.export(out_dir / f"{tid}.jsonl")
    return tm.trace_metrics(tracer.spans), tracer, desk


def audit_coverage(desk: SupportDesk, ticket: str) -> tuple[int, int]:
    """Successful write actions vs audit_log rows carrying this ticket's reference."""
    writes = sum(1 for c in desk.calls if c["name"] in WRITE_ACTIONS and not c["is_error"])
    audited = desk.db.execute("SELECT COUNT(*) FROM audit_log WHERE actor = ?", (f"support-agent:{ticket}",)).fetchone()[0]
    return writes, audited


def main() -> None:
    client = get_client()
    header(f"Lab 05 - tracing and metrics for {len(TICKETS)} inbox tickets ({MODEL})")
    if is_mock():
        print("[mock] Latencies are local compute only; tokens and costs come from the mock's usage blocks.")
    tickets = {t["ticket_id"]: t for t in ek.load_jsonl(DATA_DIR / "support" / "tickets.jsonl")}
    labels = {r["ticket_id"]: r for r in ek.load_jsonl(DATA_DIR / "support" / "ticket_labels.jsonl")}
    out_dir = runs_dir("day6_traces")

    step(1, "Run each ticket with a tracer and export the spans")
    rows, tracers, desks = [], {}, {}
    for tid in TICKETS:
        row, tracer, desk = run_ticket(client, tickets[tid], out_dir)
        row["category"] = labels[tid]["category"]
        rows.append(row)
        tracers[tid], desks[tid] = tracer, desk
    print(f"Exported {sum(len(t.spans) for t in tracers.values())} spans to {out_dir}/<ticket>.jsonl")
    print("\nTrace of T-1207 (refund above the agent's limit):")
    print(tracers["T-1207"].render_tree())

    step(2, "Per-ticket metrics, derived from the spans")
    print(f"  {'ticket':<8}{'category':<18}{'lat s':>6}{'llm':>5}{'tools':>6}{'err':>4}{'in+cache':>9}{'out':>6}"
          f"{'cost':>9}  {'stop':<9}escalation")
    for r in rows:
        esc = ",".join(f"{q}/{p}" for q, p in zip(r["queues"], r["priorities"])) or "-"
        print(f"  {r['ticket']:<8}{r['category']:<18}{r['latency_s']:>6.2f}{r['llm_calls']:>5}{r['tool_calls']:>6}"
              f"{r['tool_errors']:>4}{r['input_tokens'] + r['cache_read_tokens'] + r['cache_write_tokens']:>9,}"
              f"{r['output_tokens']:>6,}  ${r['cost_usd']:.4f}  {r['stop_reason']:<9}{esc}")

    step(3, "Dashboard")
    board = tm.dashboard(rows)
    per_resolved = board["cost_per_resolved_usd"]
    print(f"  latency        p50 {board['latency_p50_s']:.2f}s   p95 {board['latency_p95_s']:.2f}s   "
          f"per LLM call p95 {board['llm_call_p95_s']:.2f}s")
    print(f"  cost           mean ${board['cost_mean_usd']:.4f}/ticket   p95 ${board['cost_p95_usd']:.4f}   "
          f"cost per resolved ticket ${per_resolved:.4f}   (total ${board['cost_total_usd']:.4f})")
    print(f"  outcomes       automation rate {ek.pct(board['automation_rate'])}   escalation rate "
          f"{ek.pct(board['escalation_rate'])}   tool-error rate {ek.pct(board['tool_error_rate'])} "
          f"(unexpected {ek.pct(board['unexpected_tool_error_rate'])}; the rest are policy refusals)")
    print(f"  tokens         mean output {board['output_tokens_mean']:.0f}/ticket   cache-read share of input "
          f"{ek.pct(board['cache_read_share'])}")
    print(f"  stop reasons   {board['stop_reasons']}   models {board['models']}")

    step(4, "Business constraints (company_profile.md) - checked against evidence")
    safety = [r for r in rows if r["category"] == "safety_incident"]
    safety_ok = [r for r in safety if "field_service" in r["queues"] and "P1" in r["priorities"]]
    writes = audited = 0
    for tid in TICKETS:
        w, a = audit_coverage(desks[tid], tid)
        writes, audited = writes + w, audited + a
    pii = [h for t in tracers.values() for h in tm.find_pii(t.spans)]
    budget = LIMITS["reply_latency_s"] / board["llm_calls_p95"]
    checks = [
        ("mean cost per ticket < $0.40", f"${board['cost_mean_usd']:.4f}",
         board["cost_mean_usd"] < LIMITS["cost_per_ticket_usd"]),
        ("p95 reply latency < 30 s", f"{board['latency_p95_s']:.2f}s" + (" (mock: not meaningful)" if is_mock() else ""),
         board["latency_p95_s"] < LIMITS["reply_latency_s"]),
        (f"per-call latency budget (30 s / {board['llm_calls_p95']:.0f} calls at p95)",
         f"p95 {board['llm_call_p95_s']:.2f}s vs budget {budget:.1f}s", board["llm_call_p95_s"] < budget),
        ("safety tickets escalated P1 to field_service (1-h SLA)", f"{len(safety_ok)}/{len(safety)}",
         len(safety_ok) == len(safety)),
        ("every write action has an audit row with the ticket ref", f"{audited}/{writes}", audited == writes),
        ("no PII in exported traces", f"{len(pii)} values found", not pii),
    ]
    for name, value, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<58}{value}")

    step(5, "What must NOT be in your traces")
    kinds = {}
    for h in pii:
        kinds.setdefault(h["kind"], set()).add(h["field"])
    for kind, fields in kinds.items():
        print(f"  {kind}: found in {', '.join(sorted(fields))}")
    print("  Policy PRV-004: agent traces are kept 90 days, then aggregated - so redact at capture time, keep a "
          "stable pseudonym (hash) for joins, and never log full payment numbers. Exercise 10 implements this.")

    step(6, "Export for a real backend (OTLP/JSON)")
    otlp = tm.to_otlp(tracers["T-1207"].spans, "support-agent")
    path = out_dir / "T-1207.otlp.json"
    path.write_text(json.dumps(otlp, indent=1), encoding="utf-8")
    first_llm = next(s for s in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"] if s["name"] == "llm.call")
    print("  " + json.dumps({k: first_llm[k] for k in ("traceId", "spanId", "parentSpanId", "name", "kind")}))
    wanted = ("gen_ai.operation.name", "gen_ai.response.model", "gen_ai.response.finish_reason",
              "gen_ai.usage.output_tokens", "cost_usd")
    for attribute in first_llm["attributes"]:
        if attribute["key"] in wanted:
            print("    " + json.dumps(attribute))
    print(f"  full file: {path}\n  ship it: curl -X POST -H 'Content-Type: application/json' --data @{path.name} "
          "http://<otel-collector>:4318/v1/traces")

    step(7, "Alert rules derived from this baseline (tune on a week of live traffic)")
    alerts = [
        ("PAGE", "any safety_incident ticket without a P1 field_service escalation within 5 minutes"),
        ("PAGE", f"p95 reply latency > 25 s for 10 min (limit 30 s; baseline p95 {board['latency_p95_s']:.2f}s)"),
        ("TICKET", f"mean cost per ticket > $0.30 over 1 h (limit $0.40; baseline ${board['cost_mean_usd']:.4f})"),
        ("TICKET", f"UNEXPECTED tool-error rate > 2% over 15 min (baseline {ek.pct(board['unexpected_tool_error_rate'])})"
                   " - usually a backend outage, not the model"),
        ("TICKET", f"escalation rate outside baseline +/- 15 points (baseline {ek.pct(board['escalation_rate'])})"),
        ("TICKET", "stop_reason=refusal or max_tokens on > 1% of calls; any guardrail block on a write tool"),
    ]
    for severity, rule in alerts:
        print(f"  {severity:<7}{rule}")


if __name__ == "__main__":
    main()
