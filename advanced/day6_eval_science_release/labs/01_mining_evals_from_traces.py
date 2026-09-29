"""Lab 01 - Mining evals from traces: from 480 production traces to a stratified, labelled, split eval set.

Objective
    Turn the Service Desk Copilot's production traces into an eval set you could hand to a reviewer: derive
    labels from what the traces already contain (reviewer findings, CSAT, human edits, run status), measure how
    much those signals agree, de-duplicate, sample with stratification so rare categories and arms are covered,
    split by TICKET so the same request never sits on both sides of a comparison, and write the set plus a
    manifest under .runs/ - an eval set is a versioned product, not a CSV someone once exported.

Concepts
    observable signals vs hidden truth, weak labels and label noise, agreement between signals (Cohen's kappa),
    de-duplication, stratified vs random sampling and coverage, leakage through ticket-level duplicates,
    ticket-level splits, the eval-set manifest.

Run
    python advanced/day6_eval_science_release/labs/01_mining_evals_from_traces.py
    python advanced/day6_eval_science_release/labs/01_mining_evals_from_traces.py --size 160

What to observe
    * The three label sources agree 77-88% of the time but only kappa 0.45-0.67 beyond chance (step 2): a label
      is a measurement with its own error, and the eval set records WHICH measurement produced each label.
    * Random sampling leaves rare cells empty (step 4); the stratified draw guarantees a floor per cell and
      reports coverage per category and per arm.
    * A random split BY TRACE leaks: 13 tickets land on both sides and most held-out items have a sibling in
      training (step 5). The ticket-level split from splits.json leaks nothing.
    * The manifest (step 6) carries a content hash, the label policy and the counts - what you version.
"""
# test: expect=stratified
# test: expect=eval_set.jsonl

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _day6 as d6  # noqa: E402
from labkit import header, runs_dir, step  # noqa: E402

LABEL_POLICY = "review > csat (>=4 passes) > human_edit (an untouched, non-failed reply passes)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=120, help="target size of the eval set")
    parser.add_argument("--seed", type=int, default=6)
    return parser.parse_args()


def step_inventory(traces: list[dict]) -> None:
    example = traces[3]
    print(f"{len(traces)} traces from {min(d6.day_of(t) for t in traces)} to {max(d6.day_of(t) for t in traces)}, "
          f"{len({t['ticket_id'] for t in traces})} distinct tickets, {len(d6.ARMS)} deployment arms.")
    print("Observable fields of one trace (the hidden `_truth` is stripped by the loader):")
    print("  " + ", ".join(sorted(example)))
    print(f"  {example['trace_id']}: {example['category']} on {d6.arm_of(example)}, tools={example['tools']}, "
          f"status={example['outcome']['status']}, csat={example['outcome']['csat']}, "
          f"human_edited={example['outcome']['human_edited']}, review={'yes' if example['review'] else 'none'}")
    rows = []
    for arm in d6.ARMS:
        s = [t for t in traces if d6.arm_of(t) == arm]
        rows.append([arm, len(s), sum(t["review"] is not None for t in s), sum(t["outcome"]["csat"] is not None for t in s),
                     sum(t["outcome"]["human_edited"] for t in s), sum(t["outcome"]["status"] == "failed" for t in s)])
    d6.table(rows, ["arm", "traces", "reviewed", "with CSAT", "human-edited", "failed"])
    print("\nPer category:")
    counts = Counter(t["category"] for t in traces)
    print("  " + ", ".join(f"{c}={counts[c]}" for c in d6.CATEGORIES))


def step_labels(traces: list[dict]) -> None:
    print("Three signals say whether a reply was good. None is the truth; each is a measurement:")
    print("  review  - a QA reviewer read 35% of replies and listed issues (misses some, over-flags a few)")
    print("  csat    - the customer rated 46% of replies 1-5 (self-selected: unhappy customers answer more)")
    print("  edit    - a human rewrote the reply before sending (every trace; a rewrite usually means a problem)")
    overlaps = [("review", "csat", lambda t: t["review"] is not None and t["outcome"]["csat"] is not None,
                 lambda t: not t["review"]["issues"], lambda t: t["outcome"]["csat"] >= 4),
                ("review", "edit", lambda t: t["review"] is not None,
                 lambda t: not t["review"]["issues"], lambda t: not t["outcome"]["human_edited"]),
                ("csat", "edit", lambda t: t["outcome"]["csat"] is not None,
                 lambda t: t["outcome"]["csat"] >= 4, lambda t: not t["outcome"]["human_edited"])]
    rows, agreements, kappas = [], [], []
    for a, b, both, fa, fb in overlaps:
        s = [t for t in traces if both(t)]
        xa, xb = [fa(t) for t in s], [fb(t) for t in s]
        agree = sum(x == y for x, y in zip(xa, xb)) / len(s)
        kappa = d6.cohens_kappa(xa, xb)
        agreements.append(agree)
        kappas.append(kappa)
        rows.append([f"{a} vs {b}", len(s), d6.pct(agree), f"{kappa:.2f}", d6.pct(d6.rate(xa)), d6.pct(d6.rate(xb))])
    d6.table(rows, ["signals", "traces with both", "agreement", "kappa", "pass rate (1st)", "pass rate (2nd)"])
    print(f"  Raw agreement {d6.pct(min(agreements), 0)}-{d6.pct(max(agreements), 0)} looks fine; kappa "
          f"{min(kappas):.2f}-{max(kappas):.2f} (agreement beyond chance) says the signals\n"
          "  see the same replies differently. A label derived from them carries that noise, so record its SOURCE\n"
          "  and prefer the strongest one available.")
    sources = Counter(d6.weak_label(t)[1] for t in traces)
    passes = defaultdict(list)
    for t in traces:
        passed, source = d6.weak_label(t)
        passes[source].append(passed)
    print(f"\nLabel policy: {LABEL_POLICY}")
    d6.table([[src, sources[src], d6.pct(d6.rate(passes[src]))] for src in ("review", "csat", "edit")],
             ["label source", "traces", "pass rate"])


def step_dedup(traces: list[dict]) -> list[dict]:
    per_ticket = d6.by(traces, lambda t: t["ticket_id"])
    sizes = sorted(len(v) for v in per_ticket.values())
    distinct_text = len({t["reply"] for t in traces})
    print(f"Traces per ticket: min {sizes[0]}, median {sizes[len(sizes) // 2]}, max {sizes[-1]} - the same 62 requests "
          f"were answered again and again.")
    print(f"Distinct reply texts: {distinct_text} of {len(traces)}. Templated categories (safety, account access, "
          "other) produce the same\n  text every time, so a text-only key would also merge a v14 reply with its v15 twin.")
    print("De-duplication key: (ticket, arm, reply text) - one item per distinct answer OF EACH ARM, keeping the trace with "
          "the strongest label.")
    order = {"review": 0, "csat": 1, "edit": 2}
    best: dict[tuple, dict] = {}
    for t in sorted(traces, key=lambda t: (order[d6.weak_label(t)[1]], t["trace_id"])):
        best.setdefault((t["ticket_id"], d6.arm_of(t), t["reply"]), t)
    kept = sorted(best.values(), key=lambda t: t["trace_id"])
    print(f"Kept {len(kept)} traces; label sources now: "
          + ", ".join(f"{k}={v}" for k, v in sorted(Counter(d6.weak_label(t)[1] for t in kept).items())))
    return kept


def allocate_stratified(pool: list[dict], size: int, seed: int, floor: int = 2) -> list[dict]:
    """Proportional allocation per (category, prompt_version) cell with a floor, gold labels first inside a cell."""
    cells = d6.by(pool, lambda t: (t["category"], d6.version_of(t)))
    quota = {cell: min(len(items), max(floor, round(size * len(items) / len(pool)))) for cell, items in cells.items()}
    # every cell that exists in the pool keeps at least min(floor, its size) items
    # trim the largest cells until the total fits
    while sum(quota.values()) > size:
        biggest = max(quota, key=lambda c: (quota[c], c))
        quota[biggest] -= 1
    rng = random.Random(seed)
    order = {"review": 0, "csat": 1, "edit": 2}
    chosen: list[dict] = []
    for cell in sorted(cells):
        items = sorted(cells[cell], key=lambda t: (order[d6.weak_label(t)[1]], rng.random()))
        chosen.extend(items[:quota[cell]])
    return sorted(chosen, key=lambda t: t["trace_id"])


def step_sampling(pool: list[dict], size: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    random_draw = rng.sample(pool, size)
    stratified = allocate_stratified(pool, size, seed)
    print(f"Two ways to pick {size} of {len(pool)} de-duplicated traces:")
    print("  random     - each trace equally likely; rare cells get whatever falls out")
    print("  stratified - proportional per (category x prompt version) cell with a floor of 2, gold labels first")
    rows = []
    for cat in d6.CATEGORIES:
        rows.append([cat, sum(t["category"] == cat for t in pool),
                     sum(t["category"] == cat and d6.version_of(t) == "v14" for t in random_draw),
                     sum(t["category"] == cat and d6.version_of(t) == "v15" for t in random_draw),
                     sum(t["category"] == cat and d6.version_of(t) == "v14" for t in stratified),
                     sum(t["category"] == cat and d6.version_of(t) == "v15" for t in stratified)])
    d6.table(rows, ["category", "pool", "random v14", "random v15", "stratified v14", "stratified v15"])
    empty_random = sum(1 for cat in d6.CATEGORIES for v in ("v14", "v15")
                       if not any(t["category"] == cat and d6.version_of(t) == v for t in random_draw))
    empty_strat = sum(1 for cat in d6.CATEGORIES for v in ("v14", "v15")
                      if not any(t["category"] == cat and d6.version_of(t) == v for t in stratified))
    print(f"  Empty (category, version) cells: random {empty_random}, stratified {empty_strat}. A regression that lives "
          "in one cell is invisible when that cell is empty.")
    print("\nCoverage per arm (stratified draw):")
    per_arm = {arm: sum(d6.arm_of(t) == arm for t in stratified) for arm in d6.ARMS}
    d6.table([[arm, sum(d6.arm_of(t) == arm for t in pool), per_arm[arm]] for arm in d6.ARMS],
             ["arm", "pool", "in eval set"])
    thin = min(per_arm, key=per_arm.get)
    print(f"  The strata were (category, version), so arms got whatever fell out: {thin} has {per_arm[thin]} item(s).\n"
          "  Stratify on the units you will compare - if the question is 'which arm', the arm is a stratum.")
    return stratified


def step_splits(items: list[dict], seed: int) -> None:
    split = d6.split_of()
    print("Splitting by TRACE (the obvious thing) vs by TICKET (splits.json):")
    rng = random.Random(seed)
    shuffled = items[:]
    rng.shuffle(shuffled)
    cut = int(0.8 * len(shuffled))
    train_tickets = {t["ticket_id"] for t in shuffled[:cut]}
    held_tickets = {t["ticket_id"] for t in shuffled[cut:]}
    leaked = train_tickets & held_tickets
    print(f"  by trace : 80/20 at random -> {len(leaked)} tickets appear on BOTH sides "
          f"({len([t for t in shuffled[cut:] if t['ticket_id'] in leaked])} of the {len(shuffled) - cut} held-out items "
          "have a sibling in the training side)")
    counts = Counter(split[t["ticket_id"]] for t in items)
    print(f"  by ticket: train {counts['train']} / validation {counts['validation']} / test {counts['test']} items, "
          "0 tickets shared - a prompt tuned on the training side has never seen a held-out ticket.")
    print("  Why it matters: replies to the same ticket share the template and the facts. A judge, a prompt or a\n"
          "  classifier that memorises 'SO-10303 shipped with BlueRiver' scores on the held-out copy for free.")


def step_write(items: list[dict], size: int) -> Path:
    split = d6.split_of()
    out_dir = runs_dir("advanced", "day6", "evals")
    rows = []
    for t in items:
        passed, source = d6.weak_label(t)
        rows.append({"id": f"EV-{t['trace_id'][3:]}", "trace_id": t["trace_id"], "ticket_id": t["ticket_id"],
                     "category": t["category"], "arm": d6.arm_of(t), "prompt_version": d6.version_of(t),
                     "reply": t["reply"], "label": "pass" if passed else "fail", "label_source": source,
                     "split": split[t["ticket_id"]]})
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    path = out_dir / "eval_set.jsonl"
    path.write_text(payload, encoding="utf-8")
    manifest = {
        "name": "copilot-replies-eval", "version": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12],
        "source": "advanced/data/traces/traces.jsonl (production traces up to " + d6.TODAY + ")",
        "label_policy": LABEL_POLICY, "size": len(rows), "target_size": size,
        "by_split": dict(sorted(Counter(r["split"] for r in rows).items())),
        "by_label_source": dict(sorted(Counter(r["label_source"] for r in rows).items())),
        "by_category": {c: sum(r["category"] == c for r in rows) for c in d6.CATEGORIES},
        "by_arm": {a: sum(r["arm"] == a for r in rows) for a in d6.ARMS},
        "pass_rate": round(sum(r["label"] == "pass" for r in rows) / len(rows), 4),
        "known_gaps": ["labels are weak (see step 2); re-verify a sample by hand before using the set as a gate",
                       "only 62 distinct tickets: the set measures reply quality, not coverage of new requests"],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {path} ({len(rows)} items) and manifest.json")
    print(f"  version {manifest['version']}  pass rate {d6.pct(manifest['pass_rate'])}  "
          f"splits {manifest['by_split']}  labels {manifest['by_label_source']}")
    print("Coverage per category (v14 / v15):")
    print("  " + ", ".join(f"{c}={sum(r['category'] == c and r['prompt_version'] == 'v14' for r in rows)}/"
                           f"{sum(r['category'] == c and r['prompt_version'] == 'v15' for r in rows)}" for c in d6.CATEGORIES))
    print("The manifest is what you version and diff: a gate that quotes a pass rate must say which eval-set version\n"
          "  produced it, and a new version must say what changed (new failures mined, labels re-verified, size).")
    return path


def main() -> None:
    args = parse_args()
    header("Lab 01 - Mining evals from production traces")
    print("No API calls in this lab: it is pure Python over the trace files (labs never read the hidden `_truth`).")
    traces = d6.load_traces()

    step(1, "What the traces contain")
    step_inventory(traces)

    step(2, "Labels from reviews, CSAT and human edits - and how much they agree")
    step_labels(traces)

    step(3, "De-duplication")
    pool = step_dedup(traces)

    step(4, f"Sampling {args.size} items: random vs stratified")
    chosen = step_sampling(pool, args.size, args.seed)

    step(5, "Leakage: split by ticket, never by trace")
    step_splits(chosen, args.seed)

    step(6, "The eval set as a product: files and manifest")
    step_write(chosen, args.size)


if __name__ == "__main__":
    main()
