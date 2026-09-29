"""Lab 07 - Automated red-teaming and forensics: mutate the corpus, regress the stack, reconstruct an incident.

Objective
    Turn ten known attacks into adversarial variants with the model (mock: deterministic templated mutations),
    re-run the screening stack over them, and measure detection per layer per technique - the keyword filter rots
    under paraphrase and encoding while the classifier and structural tagging hold. Run the whole corpus as a
    regression suite, ship a config change that weakens a layer, and watch the suite name the cases that newly slip.
    Then reconstruct two incidents from durable logs and a tracer export: who told the agent what, and which layer
    stopped it.

Concepts
    automated red-teaming, mutation techniques (paraphrase, homoglyph, zero-width, base64, wrappers), per-layer
    coverage, regression suites and release gates, detecting a security regression, forensics from durable event
    logs, traces as the performance view, "who told the agent what"

Run
    python advanced/day5_security_engineering/labs/07_red_teaming_and_forensics.py

What to observe
    * Variants keep the attack's intent but change the surface; a couple are printed so you can read them.
    * Per-layer detection: the keyword filter drops sharply on homoglyph/base64/zero-width variants; the classifier
      and tagging stay high; the stacked detector stays at 100% on these templated mutations.
    * The regression suite: a baseline over 42 cases, then a change that turns off tagging and raises the classifier
      to block-only at 0.6 - detection falls and the suite lists exactly which cases regressed.
    * Forensics: the poisoned order note (ATK-007) and the lookalike IT audit (ATK-004) reconstructed from the run
      log - the inbound message and its source, the screen verdict, each tool call and the layer that refused it.
"""
# test: expect=Per-layer detection
# test: expect=regressed cases
# test: expect=who told the agent what

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advanced.lib.durable import RunStore
from labkit import FAST_MODEL, get_client, header, is_mock, step, wrap
from labkit.data import memory_db
from labkit.tracing import Tracer

import _day5 as d5

SEEDS = ["ATK-001", "ATK-002", "ATK-004", "ATK-007", "ATK-013", "ATK-018", "ATK-023", "ATK-024", "ATK-026", "ATK-028"]
TECHNIQUES = ["paraphrase", "homoglyph", "zero_width", "base64", "polite_wrapper", "authority_wrapper"]


def layers_hit(screen: dict) -> set[str]:
    hit = set()
    if screen["keywords"]:
        hit.add("keyword")
    if screen["action"] in ("block", "review"):
        hit.add("classifier")
    if screen["findings"]:
        hit.add("tagging")
    return hit


def step_idea() -> None:
    print("A regression suite for security: known attacks the system must keep catching, run on every change. "
          "Automated red-teaming grows that suite - take each attack and have the model generate variants that keep "
          "the intent but change the surface, so a defence tuned to yesterday's wording is tested against tomorrow's.\n")
    print("  Mutation techniques used here: " + ", ".join(TECHNIQUES))
    print("  (paraphrase = reword; homoglyph = lookalike letters; zero_width = invisible separators; base64 = encode; "
          "polite/authority wrapper = wrap the payload in innocuous or authoritative framing.)")


def step_mutate(client, cases: dict):
    print(f"Mutating {len(SEEDS)} seed attacks x {len(TECHNIQUES)} techniques into variants.\n")
    variants = {}
    total_cost = 0.0
    for sid in SEEDS:
        v, cost = d5.mutate_attack(client, cases[sid]["payload"], TECHNIQUES)
        variants[sid] = v.variants
        total_cost += cost
    if is_mock():
        print("[mock] variants are deterministic templated rewrites from advanced/mock_scenarios; live mode asks the "
              "model for genuinely novel paraphrases (the point of using a model to red-team).")
    print("  Examples (seed ATK-001 -> variant); homoglyph/zero-width variants look identical in a terminal, which "
          "is the point, so paraphrase and base64 are shown:")
    ex = {v.technique: v.text for v in variants["ATK-001"]}
    for tech in ("paraphrase", "base64"):
        print(f"    [{tech}] {d5.short(ex[tech], 92)}")
    n = sum(len(v) for v in variants.values())
    print(f"  Generated {n} variants for {total_cost and d5.money(total_cost) or '$0.0000'}"
          + (" (simulated)" if is_mock() else "") + ".")
    return variants


def step_per_layer(client, cases: dict, variants) -> None:
    print("Per-layer detection under mutation - detection per layer per technique over the variants. A single "
          "layer is brittle to the mutation aimed at it; the stack is what stays up.\n")
    caught = defaultdict(Counter)
    total = Counter()
    stacked = Counter()
    for sid, vs in variants.items():
        for v in vs:
            screen = d5.screen_stack(client, v.text, channel=cases[sid]["channel"])
            total[v.technique] += 1
            for layer in layers_hit(screen):
                caught[layer][v.technique] += 1
            if screen["caught"]:
                stacked[v.technique] += 1
    rows = []
    for tech in TECHNIQUES:
        t = total[tech]
        rows.append([tech, f"{caught['keyword'][tech]}/{t}", f"{caught['classifier'][tech]}/{t}",
                     f"{caught['tagging'][tech]}/{t}", f"{stacked[tech]}/{t}"])
    d5.table(rows, ["technique", "keyword", "classifier", "tagging", "stacked"])
    kw = sum(caught["keyword"].values())
    n = sum(total.values())
    print(f"\n  The keyword filter alone catches {kw}/{n} variants: homoglyphs, zero-width splits and base64 walk "
          "straight past it. The classifier keys off intent and the tagger off structure, so they survive rewording; "
          "stacked, they hold at 100% on these mutations. A real model generates harder variants - that is why you "
          "keep the generated set in the suite and watch the stacked number over time.")


def step_regression(client, cases: dict) -> None:
    print("The whole corpus is the regression suite: 42 cases run on every change. Baseline, then a change that "
          "turns OFF structural tagging and raises the classifier to block-only at confidence >= 0.6.\n")
    corpus = list(cases.values())
    attacks = [c for c in corpus if c["label"] == "attack"]
    base, cand = {}, {}
    for c in corpus:
        screen = d5.screen_stack(client, c["payload"], channel=c["channel"], sender=c.get("context", {}).get("from", ""))
        base[c["id"]] = screen["caught"]
        # candidate build: no tagging, classifier must be 'block' AND confidence >= 0.6, no keyword filter contribution
        cand[c["id"]] = (screen["action"] == "block" and screen["confidence"] >= 0.6)
    base_det = sum(base[c["id"]] for c in attacks)
    cand_det = sum(cand[c["id"]] for c in attacks)
    print(f"  baseline stacked detection: {base_det}/{len(attacks)}")
    print(f"  candidate  detection:        {cand_det}/{len(attacks)}")
    regressed = [c["id"] for c in attacks if base[c["id"]] and not cand[c["id"]]]
    print(f"  regressed cases (caught before, missed now): {len(regressed)}")
    for cid in regressed:
        print(f"      {cid} ({cases[cid]['family']}, {cases[cid]['channel']}): {d5.short(cases[cid]['payload'], 60)}")
    print("\n  The suite blocks the release: a config change nobody thought was security-relevant (dropping the "
          "structural tagger, tightening the classifier) opened a hole in a dozen cases. This is why security checks "
          "live in CI next to the eval gate (Day 6), not in a wiki.")


def _incident_run(store: RunStore, tracer: Tracer, case: dict, tenants: dict, db) -> str:
    """Replay one attack through the copilot's tool layer, recording a durable event log AND a trace, the way the
    production runtime would. Returns the run id."""
    run_id = f"inc-{case['id']}"
    cap, who = d5.principal_for_case(case, tenants, db)
    sender = case.get("context", {}).get("from") or case.get("context", {}).get("tool") or case["channel"]
    store.create("inbound", run_id=run_id,
                 input={"sender": sender, "channel": case["channel"], "message": case["payload"]})
    with tracer.span("agent.run", run=run_id, channel=case["channel"]):
        with tracer.span("screen"):
            screen = d5.screen_stack(get_client(), case["payload"], channel=case["channel"], sender=str(sender))
        action = "block" if screen["blocked"] else screen["action"]
        store.append(run_id, "screen.verdict",
                     {"detail": f"action={action} confidence={screen['confidence']} "
                                f"keywords={screen['keywords']} findings={screen['findings']}"})
        desk = d5.CapabilityDesk(cap, tenants, d5.CatalogBackend(db))
        for name, args in d5.ASSUME_BREACH_CALLS.get(case["id"], []):
            store.append(run_id, "tool.started", {"name": name, "input": args})
            with tracer.span(f"tool.{name}"):
                content, is_error = desk.run(name, dict(args))
            store.append(run_id, "tool.result", {"name": name, "content": content, "is_error": is_error})
    store.set_status(run_id, "completed")
    return run_id


def step_forensics(tenants: dict) -> None:
    print("Forensics: after the near-miss, reconstruct 'who told the agent what' from durable logs - not from memory "
          "or a screenshot. The runtime wrote an event per step; we replay two incidents from the case study.\n")
    store = RunStore(":memory:")
    tracer = Tracer("day5-forensics")
    db = memory_db()
    cases = {c["id"]: c for c in d5.load_cases()}
    for cid, headline in (("ATK-007", "a poisoned order note nearly issued a 20% refund"),
                          ("ATK-004", "a lookalike-domain 'IT audit' asked for the customer table")):
        run_id = _incident_run(store, tracer, cases[cid], tenants, db)
        print(f"  Incident {run_id}: {headline}")
        for row in d5.forensic_timeline(store, run_id):
            print(f"    {row['seq']:>2} {row['what']:8s} | {d5.short(row['detail'], 92)}")
        print()
    print("  The log is the system of record: append-only, one row per step, readable by any process (an approval UI, "
          "an on-call engineer, this script). The tracer is the performance view of the same run:\n")
    print(wrap(tracer.render_tree(), "    "))
    path = tracer.export()
    print(f"\n  trace exported to {path.relative_to(d5.REPO_ROOT)} (spans in OTel-style JSON Lines).")
    print("  Answering 'who told the agent what': the inbound row names the channel and sender; the screen row shows "
          "what the classifier and tagger saw; the tool rows show every call and the layer that refused it. No step "
          "depends on the worker that ran it still being alive.")


def step_gate() -> None:
    print("The red-team + regression loop as a release gate:\n")
    d5.table([
        ["grow the suite", "mutate new incidents into the corpus", "coverage tracked by family and channel"],
        ["run on every change", "the whole corpus through the stack in CI", "stacked detection must not drop"],
        ["fail closed", "a regression blocks the release", "the suite names the regressed cases"],
        ["reconstruct", "durable logs + traces per run", "any incident answerable after the fact"],
    ], ["step", "what", "gate"])
    print("\n  Day 6 gates on quality with paired tests and CIs; this is the same machinery pointed at security: a "
          "labelled corpus, a stacked detector, and a number that is not allowed to go down.")


def main() -> None:
    client = get_client()
    tenants = d5.load_tenants()
    cases = {c["id"]: c for c in d5.load_cases()}
    header("Lab 07 - Automated red-teaming and forensics")

    step(1, "The idea: a security regression suite that grows itself")
    step_idea()

    step(2, "Mutate ten attacks into variants")
    variants = step_mutate(client, cases)

    step(3, "Per-layer detection under mutation")
    step_per_layer(client, cases, variants)

    step(4, "The corpus as a regression suite; a weakening change")
    step_regression(client, cases)

    step(5, "Forensics: reconstruct the incident from durable logs")
    step_forensics(tenants)

    step(6, "The release gate")
    step_gate()


if __name__ == "__main__":
    main()
