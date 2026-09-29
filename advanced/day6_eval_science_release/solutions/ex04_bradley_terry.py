"""Solution to exercise 4 - reading a Bradley-Terry table, and buying a better one.

Refits lab 03's human (H1) table, turns strengths into head-to-head probabilities, then answers "what would a
better comparison design buy?" with a parametric bootstrap: simulate verdicts from the fitted model (ties at the
observed rate), refit, and read the spread of the canary's strength under (1) today's comparison graph and (2)
the same graph plus 60 canary-vs-baseline comparisons on the canary's own categories.

Run: python advanced/day6_eval_science_release/solutions/ex04_bradley_terry.py
"""
# test: expect=P(

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402


def simulate(design: list[tuple[str, str]], strengths: dict[str, float], tie_rate: float, rng: random.Random) -> list:
    out = []
    for a, b in design:
        if rng.random() < tie_rate:
            out.append((a, b, "tie"))
        else:
            out.append((a, b, "A" if rng.random() < d6.bt_win_probability(strengths, a, b) else "B"))
    return out


def spread(design: list[tuple[str, str]], strengths: dict[str, float], tie_rate: float, arm: str,
           reps: int = 200, seed: int = 6) -> tuple[float, float]:
    rng = random.Random(seed)
    values = sorted(d6.bradley_terry(simulate(design, strengths, tie_rate, rng), reference="baseline",
                                     iterations=150).get(arm, 0.0) for _ in range(reps))
    return values[int(0.025 * reps)], values[int(0.975 * reps) - 1]


def main() -> None:
    header("Exercise 4 - interpreting (and improving) a Bradley-Terry table")
    pairs = d6.load_pairs()
    comps = [(p["A"]["arm"], p["B"]["arm"], p["judgments"][0]["preferred"]) for p in pairs]
    strengths = d6.bradley_terry(comps, reference="baseline")

    step(1, "(a) Strengths into head-to-head probabilities: P(row beats column) = s_row / (s_row + s_col)")
    arms = sorted(strengths, key=lambda a: -strengths[a])
    d6.table([[a, f"{strengths[a]:.2f}"] + [d6.pct(d6.bt_win_probability(strengths, a, b), 0) if a != b else "-" for b in arms]
              for a in arms], ["arm", "strength"] + arms)
    s = strengths
    print(f"  P(haiku-fastpath beats opus-v15) = {s['haiku-fastpath']:.2f} / ({s['haiku-fastpath']:.2f} + {s['opus-v15']:.2f}) "
          f"= {d6.pct(d6.bt_win_probability(s, 'haiku-fastpath', 'opus-v15'))}")
    print(f"  P(opus-v15-low beats baseline) = {s['opus-v15-low']:.2f} / ({s['opus-v15-low']:.2f} + 1.00) "
          f"= {d6.pct(d6.bt_win_probability(s, 'opus-v15-low', 'baseline'))}")
    print("  These are probabilities of winning a decided comparison; ties were counted as half a win each.")

    step(2, "(b) Why the canary's 'first place' is weak")
    games = Counter()
    opponents: dict[str, Counter] = {}
    for a, b, _ in comps:
        games[a] += 1
        games[b] += 1
        opponents.setdefault(a, Counter())[b] += 1
        opponents.setdefault(b, Counter())[a] += 1
    cats = Counter(d6.ticket_labels()[p["ticket_id"]]["category"] for p in pairs
                   if "haiku-fastpath" in (p["A"]["arm"], p["B"]["arm"]))
    print(f"  haiku-fastpath: {games['haiku-fastpath']} comparisons, against {dict(opponents['haiku-fastpath'])}, on "
          f"{dict(cats)} only.")
    print("  Few comparisons (wide interval), easy tickets only (a different quality scale from arms that answer\n"
          "  warranty and safety tickets), and one annotator whose agreement with a second one is kappa 0.23.")

    step(3, "(d) What a better design buys: parametric bootstrap of the canary's strength")
    tie_rate = sum(o == "tie" for _, _, o in comps) / len(comps)
    today = [(a, b) for a, b, _ in comps]
    arm_names = sorted(strengths)
    every_pair = [(a, b) for i, a in enumerate(arm_names) for b in arm_names[i + 1:]]
    designs = [("today's graph", today),
               ("+ 60 spread over all 10 arm pairs", today + every_pair * (60 // len(every_pair))),
               ("+ 60 canary vs baseline, same tickets", today + [("baseline", "haiku-fastpath")] * 60)]
    rows = []
    for name, design in designs:
        lo, hi = spread(design, strengths, tie_rate, "haiku-fastpath")
        rows.append([name, sum("haiku-fastpath" in d for d in design), f"{lo:.2f}-{hi:.2f}", f"{hi / lo:.1f}x"])
    d6.table(rows, ["design", "canary comparisons", "95% range of its strength", "hi / lo"])
    print(f"  (simulated from the fitted strengths, ties at the observed {d6.pct(tie_rate, 0)}.) The same 60 judgments "
          "buy more when they\n  go where the decision is - canary against baseline, on the tickets both arms "
          "actually serve, judged in\n  both presentation orders - than spread evenly over pairings nobody is deciding.")


if __name__ == "__main__":
    main()
