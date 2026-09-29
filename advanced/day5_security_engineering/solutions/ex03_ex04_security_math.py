"""Solutions to exercises 3 and 4 - the false-positive cost of a classifier threshold, and defence-in-depth math.

Ex 3: what a screening threshold costs at Kestrel's volume, and whether the extra detection is worth it once the
tool layer is counted. Ex 4: the residual-risk arithmetic of stacked independent layers, and why independence is
the optimistic assumption. Prices: Claude Haiku 4.5 $1 / $5 per million input / output tokens; a loaded support
minute at $40/hour.

Run: python advanced/day5_security_engineering/solutions/ex03_ex04_security_math.py
"""
# test: expect=break-even
# test: expect=residual risk

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header, step  # noqa: E402

import _day5 as d5  # noqa: E402

TICKETS = d5.TICKETS_PER_MONTH            # 1,900 / month
ATTACK_RATE = 0.01                        # assume 1% of inbound is hostile
IN_TOK, OUT_TOK = 520, 130                # per-screen token budget (message+system in, verdict out)
HAIKU_IN, HAIKU_OUT = 1.0, 5.0            # $/MTok
REVIEW_MIN, MIN_COST = 4.0, 40.0 / 60.0   # 4 minutes per human review, $40/hour loaded


def money(x: float) -> str:
    return f"${x:,.2f}"


def ex3() -> None:
    header("Exercise 3 - false-positive cost of a classifier threshold")
    attacks = TICKETS * ATTACK_RATE
    screen_cost = (IN_TOK * HAIKU_IN + OUT_TOK * HAIKU_OUT) / 1e6
    step(1, "Fixed costs")
    print(f"  tickets/month: {TICKETS:,}; assumed hostile: {ATTACK_RATE:.0%} = {attacks:.0f} attacks/month")
    print(f"  classifier: {IN_TOK} in + {OUT_TOK} out tok/screen -> {money(screen_cost)}/screen = "
          f"{money(screen_cost * 1000)}/1,000; monthly {money(screen_cost * TICKETS)} (negligible)")

    step(2, "Two operating points (detection from lab 02's threshold sweep)")
    # A: block-only at conf >= 0.6 -> 63% of attacks blocked, ~0% benign flagged
    # B: block-or-review -> 100% of attacks caught, but a share R of ALL tickets goes to a human
    det_A, det_B = 0.63, 1.00
    caught_A, caught_B = attacks * det_A, attacks * det_B
    print(f"  A block-only: catches {caught_A:.0f}/{attacks:.0f} attacks, sends 0 benign to review -> review cost $0")
    for R in (0.08, 0.20):
        reviews = TICKETS * R
        cost = reviews * REVIEW_MIN * MIN_COST
        print(f"  B block-or-review at review rate {R:.0%}: catches {caught_B:.0f}/{attacks:.0f}; "
              f"{reviews:.0f} reviews x {REVIEW_MIN:.0f} min = {reviews * REVIEW_MIN / 60:.1f} h -> {money(cost)}/month")

    step(3, "Is B worth it? Count the tool layer")
    # The 7 extra attacks/month B catches would otherwise hit the tool layer, which stops ~96% (lab 02: 25/26).
    extra = caught_B - caught_A
    q = 0.96
    for L in (5000.0, 18400.0):
        avoided = extra * (1 - q) * L                      # expected loss B avoids over A per month
        print(f"  extra attacks B catches over A: {extra:.0f}/month; if the tool layer stops {q:.0%}, expected loss "
              f"avoided at ${L:,.0f}/incident = {money(avoided)}/month")
    review_cost_8 = TICKETS * 0.08 * REVIEW_MIN * MIN_COST
    breakeven_L = review_cost_8 / (extra * (1 - q))
    print(f"  break-even: at an 8% review rate ({money(review_cost_8)}/month), B pays for itself once an escaped "
          f"incident costs more than {money(breakeven_L)}.")
    print("  Reading: the classifier is nearly free; the cost of a low threshold is human review minutes, not tokens.\n"
          "  B catches more, but MOST of its protection is really the tool layer (it stops 96% regardless). Tune the\n"
          "  threshold for the review budget you can staff, and rely on the tool layer - not the classifier - as the\n"
          "  backstop. Chasing the last few percent of classifier recall buys little once a strong tool layer exists.")


def ex4() -> None:
    header("Exercise 4 - defence-in-depth residual risk")
    p_keyword, p_classifier, p_tagging = 0.50, 0.85, 0.25   # assumed independent per-attack detection rates
    q = 0.96                                                # tool layer stops a compromised call
    step(1, "Stated inputs (assumed independent)")
    print(f"  keyword p1={p_keyword}, classifier p2={p_classifier}, tagging p3={p_tagging}, tool layer q={q}")

    step(2, "Residual risk")
    evade_inputs = (1 - p_keyword) * (1 - p_classifier) * (1 - p_tagging)
    evade_all = evade_inputs * (1 - q)
    print(f"  (a) P(evade all three input layers) = {1-p_keyword:.2f} x {1-p_classifier:.2f} x {1-p_tagging:.2f} = "
          f"{evade_inputs:.4f} ({evade_inputs:.1%})")
    print(f"  (b) P(evade input AND tool layer fails) = {evade_inputs:.4f} x {1-q:.2f} = {evade_all:.5f} ({evade_all:.2%})")
    attacks_yr = TICKETS * ATTACK_RATE * 12
    print(f"  (c) at {attacks_yr:.0f} attacks/year, expected successful abuses = {attacks_yr:.0f} x {evade_all:.5f} = "
          f"{attacks_yr * evade_all:.2f}/year (about one every {1 / (attacks_yr * evade_all):.1f} years) - IF independent")

    step(3, "Why independence is optimistic")
    corr = (1 - p_keyword) * (1 - 0.60) * (1 - p_tagging)   # a paraphrase drops the classifier from .85 to ~.60 too
    print(f"  A single technique can defeat correlated layers at once: a fluent paraphrase beats the keyword filter\n"
          f"  AND lowers the classifier (both partly key off surface wording). If the classifier's rate on paraphrased\n"
          f"  attacks is ~0.60 instead of 0.85, P(evade inputs) rises to {corr:.3f} ({corr:.1%}) - {corr/evade_inputs:.1f}x higher.\n"
          "  Independence is a modelling convenience; measure the stack on ADVERSARIAL variants (lab 07), not only on\n"
          "  the seed corpus, and prefer layers that fail independently: the tool layer and row filters do not read\n"
          "  the text at all, so a paraphrase cannot move them. That is why the last, model-independent layer carries\n"
          "  the residual risk - and why the design target is 'every attack stopped by >= 2 independent layers, at\n"
          "  least one of which does not depend on the model'.")


def main() -> None:
    ex3()
    ex4()


if __name__ == "__main__":
    main()
