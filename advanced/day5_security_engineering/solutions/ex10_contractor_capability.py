"""Solution to exercise 10 - the capability policy for the Delta Field Services contractor tenant.

The job (install and service pumps for C-1011 and C-1013) fixes the capability: field_service, products and
knowledge domains; the dispatcher may write field work but move no money and cancel nothing; the engineer is
read-only; neither ever reads customer PII; both are scoped to the two customers and inherit the global
always_denied set. The row_filter and denied_tools are what make "no more than the job needs" true even if the
model is talked into trying.

Run: python advanced/day5_security_engineering/solutions/ex10_contractor_capability.py
"""
# test: expect=every check passes

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day5 as d5  # noqa: E402


def build_tenant() -> dict:
    return {
        "tenants": [
            {"tenant_id": "delta_fs", "name": "Delta Field Services", "kind": "contractor",
             "customers": ["C-1011", "C-1013"], "channels": ["portal", "mobile"],
             "users": [{"user": "dana.fox", "role": "df_dispatcher"}, {"user": "fse.delta.3", "role": "df_engineer"}]},
        ],
        "roles": {
            # Books and manages field work: write, but only in the field-service domain, and never PII/customer tools.
            "df_dispatcher": {
                "domains": ["field_service", "products", "knowledge", "fleet"],
                "max_risk": "write",
                "row_filter": "customer_id in tenant.customers",
                "denied_tools": ["get_customer", "list_contacts", "list_customer_sites", "get_account_manager", "get_site"],
            },
            # Reads what they need on site; changes nothing.
            "df_engineer": {
                "domains": ["field_service", "products", "knowledge"],
                "max_risk": "read",
                "row_filter": "customer_id in tenant.customers",
                "denied_tools": ["get_customer", "list_contacts", "list_customer_sites", "get_account_manager", "get_site"],
            },
        },
        # Inherit Kestrel's global set: no agent, whatever the role, may do these.
        "always_denied_to_agents": ["update_contact", "cancel_order", "release_quality_hold"],
        "approval_roles": {"cancel_order": ["support_manager"], "issue_refund": ["support_manager", "finance_director"]},
    }


def main() -> None:
    header("Exercise 10 - contractor capability policy")
    tenants = build_tenant()
    disp = d5.mint_capability(tenants, "delta_fs", "dana.fox", "portal")
    eng = d5.mint_capability(tenants, "delta_fs", "fse.delta.3", "mobile")

    step(1, "Scoped toolsets")
    for label, cap in (("df_dispatcher", disp), ("df_engineer", eng)):
        tools = d5.scoped_toolset(cap, tenants)
        print(f"  {label}: sees {len(tools)} tools, ceiling={cap.max_risk}, rows={list(cap.customers)}")

    step(2, "The checks the starter defined")
    checks = [
        ("dispatcher can schedule a field visit", d5.deny_reason(disp, "schedule_field_visit", tenants) is None),
        ("dispatcher cannot read contacts", d5.deny_reason(disp, "list_contacts", tenants) is not None),
        ("dispatcher cannot issue a refund", d5.deny_reason(disp, "issue_refund", tenants) is not None),
        ("dispatcher cannot cancel an order", d5.deny_reason(disp, "cancel_order", tenants) is not None),
        ("engineer can read a service ticket", d5.deny_reason(eng, "get_service_ticket", tenants) is None),
        ("engineer cannot schedule (read-only)", d5.deny_reason(eng, "schedule_field_visit", tenants) is not None),
        ("engineer cannot read customer PII", d5.deny_reason(eng, "get_customer", tenants) is not None),
        ("row filter is C-1011/C-1013", set(disp.customers) == {"C-1011", "C-1013"}),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\n  every check passes: {all(ok for _, ok in checks)}")

    step(3, "Why these choices")
    print("  - domains, not tool lists: the role is described by what it does (field service), so a new field tool is\n"
          "    covered without editing the role. products+knowledge let engineers read manuals and part data.\n"
          "  - max_risk caps the blast radius: the dispatcher may write field work but the 'write' ceiling already\n"
          "    excludes irreversible tools (issue_refund, cancel_order); the engineer's 'read' ceiling excludes all\n"
          "    writes. issue_refund is refused by the ceiling before any amount check.\n"
          "  - denied_tools removes the PII-bearing customer tools even though 'customers' is not in their domains -\n"
          "    defence in depth: if products/knowledge ever gained a customer-lookup tool, it would still be denied.\n"
          "  - row_filter scopes every call to the two customers; the desk resolves the owner from identifiers, so a\n"
          "    request about C-1005 is refused no matter what the message claims.\n"
          "  - always_denied_to_agents is inherited verbatim: update_contact / cancel_order / release_quality_hold are\n"
          "    off-limits to a contractor exactly as to Kestrel's own agent.")
    print("  Rejected alternative: a single 'contractor' role reused from Keystone. It grants more than a dispatcher\n"
          "  needs and less than nothing an engineer should write; two roles keep least privilege per job.")


if __name__ == "__main__":
    main()
