"""Lab 01 - Threat model: a ranked threat register for the copilot, generated from the catalog and the channels.

Objective
    Build the threat model of Kestrel's service-desk copilot the way a security review would: list the assets and
    the channels (trust boundaries), enumerate every identity an agent can act for (from tenants.json) and what
    each can reach, then score all 120 catalog tools by impact (from their `meta`) x reachability (the most exposed
    principal that holds them). Print the top risks with the controls that address them, show what least
    privilege already removed, and let the model narrate a concrete attack path for the top risks.

Concepts
    assets, trust boundaries per channel, attacker capabilities, principals and exposure, impact x reachability,
    inherent vs residual risk, STRIDE, "the model is not a security boundary", control coverage (defence in depth)

Run
    python advanced/day5_security_engineering/labs/01_threat_model.py

What to observe
    * The channel table: only internal_chat is authenticated; five channels carry instructions an attacker writes.
    * The register's top rows are apply_order_discount and issue_credit_note: value-moving tools the anonymous-email
      copilot can reach. The approval gate is what has to hold for them.
    * "What least privilege already bought": cancel_order, update_contact and release_quality_hold fall to 0 (never
      available to an agent); issue_refund and send_email fall from 20 to 10 (internal staff only).
    * No control in the register is "the system prompt says so": every top risk has at least two layers that do not
      depend on the model.
"""
# test: expect=Threat register
# test: expect=never available to an agent
# test: expect=single-layer risks: none

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import get_client, header, is_mock, step, wrap

import _day5 as d5

TOP_N = 12


def step_boundaries() -> None:
    print("Trust boundaries: where content enters the copilot, and how much the harness may trust it.\n")
    d5.table([[c["channel"], c["trust"], "yes" if c["authenticated"] else "no",
               "yes" if c["carries_instructions"] else "no", c["note"]] for c in d5.CHANNELS],
             ["channel", "trust", "authenticated", "carries instructions", "why"])
    print("\nAssets an attacker is after:\n")
    d5.table([[a["asset"], a["example"], a["stride"]] for a in d5.ASSETS], ["asset", "reached through", "STRIDE"])
    carriers = [c["channel"] for c in d5.CHANNELS if c["carries_instructions"]]
    print(f"\n{len(carriers)} of {len(d5.CHANNELS)} channels can deliver text the model will read as if it were an "
          f"instruction: {', '.join(carriers)}. Attacker capability on each is 'write arbitrary text'; none of them "
          "proves who wrote it.")


def step_principals(tenants: dict) -> None:
    print("Every identity an agent can act for. Capabilities come from tenants.json and the channel - never from "
          "what a message claims.\n")
    rows = []
    for p in d5.principals(tenants):
        cap = p["cap"]
        held = d5.scoped_toolset(cap, tenants)
        risks = Counter(d5.CATALOG_BY_NAME[t["name"]]["meta"]["risk"] for t in held)
        customers = "all" if cap.customers == "*" else ",".join(cap.customers) or "none"
        rows.append([p["label"], cap.channel, p["exposure"], len(held), risks.get("write", 0), risks.get("irreversible", 0),
                     cap.max_risk, customers])
    d5.table(rows, ["principal", "channel", "exposure", "tools", "writes", "irreversible", "ceiling", "rows"])
    print("\nExposure: 4 = anyone who can send an email; 3 = an external portal user; 2 = internal staff "
          "(trusted message, but the records, documents and pages their agent reads can still be poisoned).")
    copilot = next(r for r in rows if r[0].startswith("copilot"))
    agent = next(r for r in rows if r[0].startswith("support_agent"))
    print(f"The copilot runs as the support_agent role but in the resolve phase: {copilot[3]} tools instead of "
          f"{agent[3]}, {copilot[4]} writes instead of {agent[4]}, and one customer's rows instead of all "
          "(least privilege per phase and per conversation, lab 03).")


def step_register(tenants: dict) -> list[dict]:
    register = d5.build_threat_register(tenants)
    print(f"Threat register: {len(register)} catalog tools scored impact (1-5, from meta) x reach (0-4, the most "
          f"exposed principal holding the tool). Top {TOP_N}:\n")
    rows = []
    for r in register[:TOP_N]:
        rows.append([r["tool"], r["impact"], r["reach"], r["score"], d5.short(r["reach_via"], 30), r["impact_why"]])
    d5.table(rows, ["tool", "impact", "reach", "score", "reachable by", "why it matters"])
    print("\nControls that address each top risk (most categorical first):")
    for r in register[:6]:
        print(f"  {r['tool']}:")
        for c in r["controls"]:
            print(f"      - {c}")
    return register


def step_least_privilege(register: list[dict]) -> None:
    print("Inherent risk assumes the copilot held every tool (impact x 4); residual is under today's capability "
          "design.\n")
    inherent = sorted(register, key=lambda r: (-r["inherent"], r["tool"]))[:10]
    d5.table([[r["tool"], r["impact"], r["inherent"], r["score"], r["reach_via"]] for r in inherent],
             ["tool", "impact", "inherent", "residual", "reachable by"])
    removed = [r["tool"] for r in register if r["reach"] == 0]
    moved = [r["tool"] for r in inherent if 0 < r["score"] < r["inherent"]]
    print(f"\nRemoved from every agent: {', '.join(removed)} (always_denied_to_agents).")
    print(f"Pushed behind an authenticated channel or a narrower role: {', '.join(moved)}.")
    dist = Counter(r["reach"] for r in register)
    print("Reach distribution over the catalog: " + ", ".join(f"reach {k}: {dist[k]}" for k in sorted(dist, reverse=True)))


def step_attack_paths(client, register: list[dict]) -> None:
    top = [d5.CATALOG_BY_NAME[r["tool"]] for r in register[:6]]
    cases, cost = d5.narrate_abuse_cases(client, top)
    if is_mock():
        print("[mock] attack paths below are templated from each tool's risk, PII and approval flags; live mode asks the model "
              "to write them.\n")
    for c in cases.cases:
        print(f"  {c.tool}  (via {c.attacker_channel})")
        print(wrap("path: " + c.path.replace("[mock] ", ""), "      "))
        print(wrap("stop it with: " + c.control, "      "))
    print(f"\n  narration cost: {d5.money(cost)}")


def step_coverage(register: list[dict]) -> None:
    print("Defence in depth, checked: a risk covered only by the input classifier depends on the model's cousin "
          "behaving. Every top risk needs at least one control that does not read the text.\n")
    classifier = d5.CONTROLS["tagging_classifier"]
    single = [r["tool"] for r in register[:TOP_N] if [c for c in r["controls"] if c != classifier] == []]
    rows = []
    for r in register[:TOP_N]:
        hard = [c for c in r["controls"] if c != classifier]
        rows.append([r["tool"], len(r["controls"]), len(hard), hard[0].split(" (")[0] if hard else "-"])
    d5.table(rows, ["tool", "controls", "not model-dependent", "strongest layer"])
    print("\nNone of these controls is 'the system prompt tells the model not to': the model is not a security "
          "boundary. Prompt rules lower how often the layers above have to fire; they are never the layer.")
    print(f"\nsingle-layer risks: {', '.join(single) if single else 'none'}")
    stride = Counter()
    for r in register:
        if r["score"] == 0:
            continue
        if r["tool"] in d5.MOVES_VALUE or r["approval"]:
            stride["Elevation/Tampering"] += 1
        elif r["pii"]:
            stride["Information disclosure"] += 1
        elif r["risk"] != "read":
            stride["Tampering"] += 1
        else:
            stride["Information disclosure (minor)"] += 1
    print("STRIDE mix of the reachable surface: " + ", ".join(f"{k} {v}" for k, v in stride.most_common()))
    print("Repudiation is covered outside the register: every CapabilityDesk decision is audited (lab 03), and "
          "lab 07 rebuilds an incident from that log.")


def main() -> None:
    client = get_client()
    tenants = d5.load_tenants()
    header("Lab 01 - Threat model for the service-desk copilot")

    step(1, "Assets and trust boundaries")
    step_boundaries()

    step(2, "Principals: who an agent can act for, and what each can reach")
    step_principals(tenants)

    step(3, "The threat register: impact x reachability")
    register = step_register(tenants)

    step(4, "What least privilege already bought")
    step_least_privilege(register)

    step(5, "Attack paths for the top risks")
    step_attack_paths(client, register)

    step(6, "Control coverage and STRIDE")
    step_coverage(register)


if __name__ == "__main__":
    main()
