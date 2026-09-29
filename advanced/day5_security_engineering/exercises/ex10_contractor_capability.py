"""Exercise 10 starter - the capability policy for a new contractor tenant, as code.

Kestrel is onboarding "Delta Field Services", a field-service contractor that installs and services pumps for two
customers: Pacific Desalination Partners (C-1011) and Coastal Shipyards (C-1013). They work from a portal and a
mobile app. You must give them exactly the capability their job needs - no more.

Requirements (turn these into the tenant/role JSON):
  * two roles: `df_dispatcher` (books and manages field work) and `df_engineer` (reads what they need on site);
  * both are scoped to C-1011 and C-1013 only (row filter);
  * neither may ever read customer contact records or PII-bearing customer tools;
  * the dispatcher may create/schedule field work (write) but not move money or cancel orders;
  * the engineer is read-only;
  * both inherit the global always_denied_to_agents set.

Fill in `build_tenant()` so the assertions in `main()` pass. mint_capability and deny_reason (in _day5) take a
`tenants` dict shaped like data/security/tenants.json - build one in memory here.

Run: python advanced/day5_security_engineering/exercises/ex10_contractor_capability.py
"""
# test: expect=TODO

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day5 as d5  # noqa: E402


def build_tenant() -> dict:
    """Return a tenants dict (same shape as tenants.json) with the Delta Field Services tenant and its two roles.

    TODO: fill in the roles' domains, max_risk, row_filter and denied_tools, and the always_denied_to_agents and
    approval_roles sections, so that the checks in main() hold.
    """
    return {
        "tenants": [
            {"tenant_id": "delta_fs", "name": "Delta Field Services", "kind": "contractor",
             "customers": ["C-1011", "C-1013"], "channels": ["portal", "mobile"],
             "users": [{"user": "dana.fox", "role": "df_dispatcher"}, {"user": "fse.delta.3", "role": "df_engineer"}]},
        ],
        "roles": {
            # TODO: define df_dispatcher and df_engineer
            "df_dispatcher": {},
            "df_engineer": {},
        },
        "always_denied_to_agents": [],   # TODO
        "approval_roles": {},            # TODO
    }


def main() -> None:
    header("Exercise 10 - contractor capability policy (starter)")
    tenants = build_tenant()
    step(1, "Mint capabilities and check the scoping")
    try:
        disp = d5.mint_capability(tenants, "delta_fs", "dana.fox", "portal")
        eng = d5.mint_capability(tenants, "delta_fs", "fse.delta.3", "mobile")
    except Exception as exc:  # noqa: BLE001
        print(f"  TODO: build_tenant() is incomplete ({type(exc).__name__}: {exc})")
        return
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
        print(f"  [{'PASS' if ok else 'TODO'}] {label}")
    if all(ok for _, ok in checks):
        print("\n  All checks pass - see solutions/ for the reference policy and its justification.")
    else:
        print("\n  TODO: complete build_tenant() until every check passes.")


if __name__ == "__main__":
    main()
