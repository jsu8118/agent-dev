"""Solution to exercise 9 - add graders to the harness, and prove they work before trusting them.

Two new code graders for _evalkit:
  * grade_references_quoted  - every reference the run CREATED in the system of record (RMA, escalation, refund)
                               appears in the reply: the customer needs it, and a reply without it usually means
                               the model summarised away the one fact that matters.
  * grade_facts              - the numeric facts a scenario lists under "facts" (E04: line value, fee, refund)
                               appear in the reply, formatted as money.

A grader is code that decides what "correct" means - so it gets tests of its own: known-good replies must
pass, deliberately broken ("mutated") replies must fail.  Then it is run on a real candidate.

Run
    python day6_evals_guardrails_production/solutions/ex09_reference_grader.py
"""

# test: expect=mutation tests

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _evalkit as ek  # noqa: E402
from kestrel.support_agent import SYSTEM_PROMPT  # noqa: E402
from labkit import get_client, header, is_mock, step  # noqa: E402

CONCISE_PROMPT = SYSTEM_PROMPT + "\nPrompt version: support-v2-concise. Keep every reply to at most two sentences."


def grade_references_quoted(obs: ek.Observation) -> list[ek.Check]:
    # A suspected attacker is not owed an internal reference, so security escalations are exempt.
    created = ([r["rma_id"] for r in obs.state.rmas_created] + [r["refund_id"] for r in obs.state.refunds]
               + [e["escalation_id"] for e in obs.state.escalations if e["queue"] != "security"])
    if not created:
        return []
    missing = [ref for ref in created if ref not in obs.reply]
    return [ek.Check("quotes the references it created", not missing, f"missing {missing}" if missing else "")]


def _money_forms(value: float) -> list[str]:
    return [f"{value:,.2f}", f"{value:.2f}"]


def grade_facts(obs: ek.Observation) -> list[ek.Check]:
    checks = []
    for name, value in obs.scenario.facts.items():
        if isinstance(value, (int, float)):
            ok = any(form in obs.reply for form in _money_forms(float(value)))
            checks.append(ek.Check(f"states {name}", ok, "" if ok else f"{value:,.2f} not in reply"))
    return checks


GRADERS = ek.DEFAULT_GRADERS + [grade_references_quoted, grade_facts]


def mutation_tests() -> list[tuple[str, bool]]:
    """Feed the graders hand-made observations whose correct verdict we KNOW."""
    e04 = next(s for s in ek.load_scenarios(ids=["E04"]))
    state = ek.EndState(refunds=[], escalations=[], audit_rows=[],
                        rmas_created=[{"rma_id": "RMA-7023", "order_id": "SO-10283", "sku": "MS-250"}])
    good = ("I've approved your return under RMA-7023 for 4 x MS-250 from order SO-10283. A 15% restocking fee "
            "applies, so the refund after inspection will be $1,901.82 (line value $2,237.44, fee $335.62).")
    cases = [("good reply passes", good, True),
             ("RMA number removed", good.replace("RMA-7023", "your RMA"), False),
             ("refund amount changed", good.replace("1,901.82", "1,910.82"), False),
             ("amount without thousands separator still passes", good.replace("2,237.44", "2237.44"), True),
             ("empty reply", "", False)]
    results = []
    for label, reply, should_pass in cases:
        obs = ek.Observation(e04, reply, [], state, "rma_created")
        checks = grade_references_quoted(obs) + grade_facts(obs)
        results.append((label, all(c.passed for c in checks) == should_pass))
    return results


def main() -> None:
    header("Exercise 9 - two new graders, tested before use")
    step(1, "Grader mutation tests (known-good must pass, known-bad must fail)")
    results = mutation_tests()
    for label, ok in results:
        print(f"  {'ok  ' if ok else 'BAD '} {label}")
    assert all(ok for _, ok in results), "a grader misjudged a known case - fix the grader before using it"
    print(f"  {len(results)}/{len(results)} mutation tests behave as expected")

    client = get_client()
    step(2, "Baseline prompt with the extended grader set")
    baseline = ek.run_eval(client, ek.load_scenarios(), run_name="ex09-baseline", graders=GRADERS)
    ek.print_summary(baseline)

    step(3, "A 'concise' candidate prompt: what do the new graders see that the old ones missed?")
    concise_default = ek.run_eval(client, ek.load_scenarios(), run_name="ex09-concise-default",
                                  system_prompt=CONCISE_PROMPT)
    concise_new = ek.run_eval(client, ek.load_scenarios(), run_name="ex09-concise-new",
                              system_prompt=CONCISE_PROMPT, graders=GRADERS)
    old_fail = {c.scenario_id for c in concise_default.cases if not c.passed}
    new_fail = {c.scenario_id for c in concise_new.cases if not c.passed}
    print(f"  default graders fail {len(old_fail)}: {sorted(old_fail)}")
    print(f"  with the new graders {len(new_fail)}: {sorted(new_fail)}")
    for case in concise_new.cases:
        if case.scenario_id in new_fail - old_fail:
            print(f"  newly caught {case.scenario_id}: {'; '.join(f'{c.name}: {c.detail}' for c in case.failed_checks)}")
            reply = " ".join(case.reply.split())
            print(f"      reply: {reply[:110]}")
    if is_mock():
        print("  [mock] the stand-in obeys the 'two sentences' instruction literally, so it drops whatever came third.")


if __name__ == "__main__":
    main()
