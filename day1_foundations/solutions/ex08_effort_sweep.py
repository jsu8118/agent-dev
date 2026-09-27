"""Solution - Exercise 8: does effort matter for triage?

Runs the triage pipeline at every effort level the model supports and prints one comparison table.
In mock mode, accuracy is identical across levels (the stand-in is a heuristic) while simulated
thinking tokens and cost change; live runs give you the real trade-off.
"""

# test: args=--limit 12
# test: expect=effort

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _triage import load_labels, load_tickets, score, to_jsonable, triage  # noqa: E402
from labkit import LEDGER, MODEL, get_client, get_spec, header, is_mock  # noqa: E402


def run(client, tickets, labels, model, effort):
    LEDGER.reset()
    latencies, predictions, out_tokens = [], {}, 0

    def work(t):
        return t["ticket_id"], *triage(client, t, model=model, effort=effort)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for tid, result, message, seconds in pool.map(work, tickets):
            predictions[tid] = to_jsonable(result)
            latencies.append(seconds)
            out_tokens += message.usage.output_tokens
    report = score(predictions, labels)
    latencies.sort()
    return {"effort": effort, "acc": report["accuracy"]["category"], "p1": report["p1_recall"],
            "human": report["requires_human_recall"], "out_tokens": out_tokens, "cost": LEDGER.total_cost,
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "wall": time.perf_counter() - start}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    client = get_client()
    labels = load_labels()
    everything = load_tickets()
    # Stratified sample: always include the safety (P1) cases, then evenly spaced others.
    p1 = [t for t in everything if labels[t["ticket_id"]]["priority"] == "P1"]
    rest = [t for t in everything if t not in p1]
    step_size = max(1, len(rest) // max(args.limit - len(p1), 1))
    tickets = (p1 + rest[::step_size])[: args.limit]
    levels = get_spec(args.model).effort_levels
    header(f"Exercise 8 - effort sweep on {args.model} ({len(tickets)} tickets)")
    if not levels:
        print(f"{args.model} has no effort parameter; compare models instead (e.g. --model claude-opus-5).")
        return
    fmt = lambda v: "n/a" if v is None else f"{v:.0%}"
    print(f"{'effort':<8} {'cat acc':>8} {'P1 rec':>7} {'human rec':>9} {'out tok':>8} {'cost $':>8} {'p95 s':>6}")
    for effort in levels:
        r = run(client, tickets, labels, args.model, effort)
        print(f"{r['effort']:<8} {fmt(r['acc']):>8} {fmt(r['p1']):>7} {fmt(r['human']):>9} {r['out_tokens']:>8} "
              f"{r['cost']:>8.4f} {r['p95']:>6.2f}")
    if is_mock():
        print("\nMock mode: accuracy is flat by construction; only the simulated thinking cost moves.")
    print("Pick the lowest effort whose P1 recall and requires_human recall meet the gate on a large enough set; "
          "then prefer the cheaper of (lower effort, smaller model) by cost per correctly triaged ticket.")


if __name__ == "__main__":
    main()
