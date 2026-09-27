"""Lab 04 - A production-style triage pipeline with evaluation.

Objective
    Triage all 62 tickets concurrently, score the results against ground-truth labels, and
    report accuracy, safety recall, latency and cost per ticket - the numbers you need before
    anyone lets this near real customers.

Concepts
    Batch-style processing with a thread pool; structured outputs at scale; evaluation against
    labels (per-field accuracy, recall on the class that matters, confusion analysis);
    cost per item; caching a shared system prompt.

Run
    python day1_foundations/labs/04_triage_pipeline.py [--model claude-opus-5] [--effort low] [--limit 20]

What to observe
    * Accuracy differs a lot by field: sentiment and priority are harder than order_id.
    * P1 recall is the metric that matters for safety - accuracy alone hides misses.
    * Cache reads on the shared guidelines after the first call (cheaper input).
    * In mock mode the classifier is a keyword heuristic - the errors are real but different
      from Claude's. Run live to see real numbers.
"""

# test: args=--limit 25
# test: expect=P1 recall

import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from _triage import load_labels, load_tickets, print_report, score, to_jsonable, triage
from labkit import LEDGER, MODEL, get_client, header, is_mock, runs_dir, step


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default=None, help="low | medium | high | xhigh | max (ignored if unsupported)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    client = get_client()
    tickets = load_tickets()[: args.limit]
    labels = load_labels()
    header(f"Lab 04 - triage {len(tickets)} tickets with {args.model} (effort={args.effort or 'default'})")
    if is_mock():
        print("Mock mode: the 'model' is a keyword heuristic, so accuracy here illustrates the method only.")

    step(1, "Run the pipeline (concurrently)")
    predictions, latencies, messages = {}, {}, {}

    def work(ticket: dict):
        result, message, seconds = triage(client, ticket, model=args.model, effort=args.effort)
        return ticket["ticket_id"], result, message, seconds

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for tid, result, message, seconds in pool.map(work, tickets):
            predictions[tid] = to_jsonable(result)
            latencies[tid] = seconds
            messages[tid] = message
    print(f"Processed {len(predictions)} tickets.")

    step(2, "Score against the labels")
    report = score(predictions, labels)
    print_report(report)

    step(3, "Look at the errors that matter")
    for tid, pred in predictions.items():
        if pred is None:
            print(f"  {tid}: no result (stop_reason={messages[tid].stop_reason})")
            continue
        truth = labels[tid]
        if truth["priority"] == "P1" and pred["priority"] != "P1":
            print(f"  MISSED SAFETY CASE {tid}: predicted {pred['category']}/{pred['priority']}")
        if truth["requires_human"] and not pred["requires_human"]:
            print(f"  {tid}: should have been routed to a human (label requires_human=true)")

    step(4, "Latency and cost")
    ordered = sorted(latencies.values())
    p50 = ordered[len(ordered) // 2]
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    cache_reads = sum(m.usage.cache_read_input_tokens or 0 for m in messages.values())
    print(f"latency p50={p50:.2f}s p95={p95:.2f}s (wall-clock; near zero in mock mode)")
    print(f"total cost ${LEDGER.total_cost:.4f}  ->  ${LEDGER.total_cost / max(len(tickets), 1):.5f} per ticket")
    print(f"cache_read_input_tokens across the run: {cache_reads} (the guidelines are cached after the first call)")

    out = runs_dir("day1") / f"triage_{args.model}_{args.effort or 'default'}.json"
    out.write_text(json.dumps({"predictions": predictions, "report": {**report, "confusion": [
        [list(k), v] for k, v in report["confusion"].items()]}}, indent=2))
    print(f"Saved predictions and report to {out}")


if __name__ == "__main__":
    main()
