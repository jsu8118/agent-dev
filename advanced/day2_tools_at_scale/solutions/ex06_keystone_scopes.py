"""Solution to exercise 6 - capability-scoped toolsets for Keystone Mechanical's mobile app.

Objective
    Derive the toolsets of Keystone's two roles from tenants.json, decide what is loaded and what is deferred, add the
    harness-level grants the design needs (escalation), and check the invariants a scope must keep before any session
    starts. Then prove the executor refuses what the toolset does not offer.

Concepts
    role -> domains, max risk, denied tools; the set denied to every agent; harness grants; loaded vs deferred inside a
    scope; row filters enforced in the executor (Day 5); invariants as tests

Run
    python advanced/day2_tools_at_scale/solutions/ex06_keystone_scopes.py

What to observe
    * The two toolsets, their size and the prompt tokens of each session's tools array.
    * Every invariant holds; the one that would fail without the harness grant is escalation.
    * An out-of-scope call gets `not_available` from the executor.
"""
# test: expect=invariants hold

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import get_client, header, step  # noqa: E402

import _day2 as d2  # noqa: E402

GRANTS = ("escalate_to_human",)          # the harness gives every session a way to hand over to a person
ALWAYS_LOADED = ("check_warranty", "get_service_ticket", "search_knowledge_base", "escalate_to_human")


def keystone_toolset(role: str) -> tuple[list[str], list[dict]]:
    names = d2.scoped_names(role, plus=GRANTS)
    loaded = [n for n in names if n in ALWAYS_LOADED]
    return names, d2.wide_toolset(d2.SEARCH_BM25, loaded=loaded, names=names)


def invariants(role: str, names: list[str]) -> list[tuple[str, bool]]:
    spec = d2.tenants()["roles"][role]
    denied = set(spec.get("denied_tools", [])) | set(d2.tenants()["always_denied_to_agents"])
    rank = d2.RISK_RANK[spec["max_risk"]]
    return [
        ("no denied tool is declared (not even deferred)", not (denied & set(names))),
        ("no tool above the role's max risk (grants excepted)",
         all(d2.RISK_RANK[d2.meta(n)["risk"]] <= rank for n in names if n not in GRANTS)),
        ("every domain is granted to the role", all(d2.meta(n)["domain"] in spec["domains"] for n in names if n not in GRANTS)),
        ("a person is always reachable (escalate_to_human declared)", "escalate_to_human" in names),
        ("no approval-required tool for a read-only role", spec["max_risk"] != "read" or
         not any(d2.meta(n)["approval_required"] for n in names)),
    ]


def main() -> None:
    client = get_client()
    header("Exercise 6 - Keystone's scoped toolsets")
    tenant = next(t for t in d2.tenants()["tenants"] if t["tenant_id"] == "keystone")
    print(f"tenant {tenant['name']}: customers {tenant['customers']}, channels {tenant['channels']}")
    for role in ("contractor_lead", "contractor_engineer"):
        step(role, "toolset")
        names, tools = keystone_toolset(role)
        tokens = d2.count_prompt(client, tools, [{"role": "user", "content": "x"}])
        loaded = [t["name"] for t in tools if t.get("name") and not t.get("defer_loading") and not t.get("type")]
        print(f"  {len(names)} tools ({len(loaded)} loaded: {', '.join(loaded)}; the rest deferred behind BM25 search); "
              f"first-request prompt {tokens:,} tokens")
        by_domain: dict[str, list[str]] = {}
        for n in names:
            by_domain.setdefault(d2.meta(n)["domain"], []).append(n)
        for domain, members in by_domain.items():
            print(f"    {domain:<15} {', '.join(members)}")
        checks = invariants(role, names)
        for label, ok in checks:
            print(f"  [{'ok' if ok else 'FAIL'}] {label}")
        print(f"  all invariants hold: {all(ok for _, ok in checks)}")
    step("executor", "the other half of the scope")
    names, _ = keystone_toolset("contractor_engineer")
    ops = d2.KestrelOps(allowed=names)
    for call, args in (("list_contacts", {"customer_id": "C-1016"}), ("check_warranty", {"serial_number": "KP250-2608-0005"})):
        content, is_error = ops.run(call, args)
        print(f"  {call}: is_error={is_error} {d2.clip(content, 110)}")
    print("  The row filter (customer_id in ['C-1016', 'C-1019']) is the next check the executor needs: the warranty call\n"
          "  above is for KP250-2608-0005, a Keystone unit; a serial of another customer must be refused the same way (Day 5).")


if __name__ == "__main__":
    main()
