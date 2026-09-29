"""Solution to exercise 11 - a rolling drift monitor with a measured noise floor and a change-log join.

Implements the starter's DriftMonitor (exercises/ex11_drift_monitor.py): PSI per feature between a fixed
reference week and the trailing 7 days, a noise floor from resampling the reference, and a daily loop that
records the first day each feature drifts and whether the change log explains it.

Run: python advanced/day6_eval_science_release/solutions/ex11_drift_monitor.py
"""
# test: expect=first drift
# test: expect=explained

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exercises"))

import _day6 as d6  # noqa: E402
import ex11_drift_monitor as starter  # noqa: E402
from labkit import header, step  # noqa: E402


class DriftMonitor(starter.DriftMonitor):
    def __init__(self, reference: list[dict], *, reps: int = 150, seed: int = 6) -> None:
        super().__init__(reference, reps=reps, seed=seed)
        self._floors: dict[str, float] = {}

    @staticmethod
    def _psi(name: str, a_traces: list[dict], b_traces: list[dict]) -> float:
        _, kind, fn = starter.FEATURES[name]
        a = [v for t in a_traces for v in fn(t)]
        b = [v for t in b_traces for v in fn(t)]
        return d6.psi_numeric(a, b) if kind == "num" else d6.psi(d6.shares(a), d6.shares(b))

    def psi(self, name: str, window: list[dict]) -> float:
        return self._psi(name, self.reference, window)

    def noise_floor(self, name: str, n_window: int) -> float:
        if name not in self._floors:                 # once per feature: window sizes differ by a handful of traces
            rng = random.Random(self.seed)
            ref = self.reference
            values = sorted(self._psi(name, [rng.choice(ref) for _ in ref], [rng.choice(ref) for _ in range(n_window)])
                            for _ in range(self.reps))
            self._floors[name] = values[int(0.95 * self.reps) - 1]
        return self._floors[name]

    def check(self, window: list[dict]) -> dict[str, tuple[float, float, bool]]:
        out = {}
        for name in starter.FEATURES:
            value, floor = self.psi(name, window), self.noise_floor(name, len(window))
            out[name] = (value, floor, value > floor)
        return out


def explanation(name: str, day: str) -> str:
    """Every change-log entry on or before `day` that is expected to move this feature (there can be several)."""
    events = [when[5:] for when, _, feats in starter.CHANGE_LOG if when <= day and name in feats]
    return ("explained by " + ", ".join(events)) if events else "UNEXPLAINED"


def main() -> None:
    header("Exercise 11 - a drift monitor (solution)")
    traces = d6.load_traces()
    reference = [t for t in traces if starter.REFERENCE[0] <= d6.day_of(t) <= starter.REFERENCE[1]]
    monitor = DriftMonitor(reference)
    windows = starter.daily_windows(traces)

    step(1, "Run the monitor every day on the trailing 7 days")
    first: dict[str, str] = {}
    last: dict[str, tuple[float, float, bool]] = {}
    flagged_days: dict[str, int] = {}
    run: dict[str, int] = {}
    longest: dict[str, int] = {}
    for day, window in windows:
        result = monitor.check(window)
        for name, (value, floor, drifted) in result.items():
            run[name] = run.get(name, 0) + 1 if drifted else 0
            longest[name] = max(longest.get(name, 0), run[name])
            if drifted:
                first.setdefault(name, day)
                flagged_days[name] = flagged_days.get(name, 0) + 1
        last = result
    rows = []
    for name, (family, _, _) in starter.FEATURES.items():
        value, floor, _ = last[name]
        verdict = explanation(name, first[name]) if name in first else "stable"
        rows.append([name, family, f"{monitor.noise_floor(name, 0):.3f}", first.get(name, "-"),
                     f"{flagged_days.get(name, 0)}/{len(windows)}", longest.get(name, 0), f"{value:.3f}", verdict])
    d6.table(rows, ["feature", "family", "noise floor", "first drift", "days flagged", "longest run",
                    f"PSI on {windows[-1][0]}", "verdict (change-log dates)"])
    unexplained = [name for name in first if explanation(name, first[name]) == "UNEXPLAINED"]
    alerts = [name for name in first if longest.get(name, 0) >= 2]

    step(2, "Reading it")
    inputs = [n for n, (fam, _, _) in starter.FEATURES.items() if fam == "input"]
    print(f"  Inputs ({', '.join(inputs)}) never drift: customers asked for the same things all month. The model mix\n"
          f"  flags on {first.get('model mix', '-')}, the day the Opus arm starts; latency and token volumes follow once "
          "enough of the\n  trailing week comes from the new arms - a trailing window delays detection by design, the "
          "price of a\n  stable comparison. Several change-log entries can explain one drift; list them all.")
    print(f"  Unexplained: {', '.join(f'{n} ({flagged_days[n]} day(s), longest run {longest[n]})' for n in unexplained) or 'none'}. "
          "A 95% floor raises about one false\n  alarm per twenty feature-days, so the monitor alerts only after two "
          f"consecutive days above the floor:\n  alerts under that rule: {', '.join(alerts) or 'none'} - each one explained "
          "or not, a human reads it.")
    print("  What the monitor cannot say: reply length never moved although v15 promised 'be brief'. A monitor that\n"
          "  only watches for movement never notices that a change failed to do what it said - check the expected\n"
          "  effect of every change-log entry explicitly.")


if __name__ == "__main__":
    main()
