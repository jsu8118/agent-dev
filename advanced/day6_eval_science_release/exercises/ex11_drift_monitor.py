"""Exercise 11 starter - a drift monitor for the production trace stream.

Build a monitor that, for every day from 2026-08-31 to 2026-09-14, compares the LAST 7 DAYS of traces with a fixed
reference week (2026-08-24..2026-08-30) on the features below, flags a feature when its PSI exceeds a noise floor
you measure by resampling the reference, and reports the first day each feature drifted and whether an entry of
the change log explains it (an event on or before that day that lists the feature).

Given: the features, the change log, the daily windows, and the statistics in _day6 (psi, psi_numeric, shares).
TODO: the three methods of DriftMonitor. Keep it fast: compute each feature's noise floor once, at the typical
window size (the windows differ by a handful of traces).

Run: python advanced/day6_eval_science_release/exercises/ex11_drift_monitor.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

import _day6 as d6  # noqa: E402
from labkit import header, step  # noqa: E402

REFERENCE = ("2026-08-24", "2026-08-30")
FIRST_DAY, LAST_DAY, WINDOW_DAYS = "2026-08-31", "2026-09-14", 7

# name -> (family, kind, values of one trace); "cat" features use d6.psi on shares, "num" features d6.psi_numeric
FEATURES: dict[str, tuple[str, str, Callable[[dict], list]]] = {
    "category mix": ("input", "cat", lambda t: [t["category"]]),
    "priority mix": ("input", "cat", lambda t: [t["priority"]]),
    "model mix": ("system", "cat", lambda t: [t["deployment"]["model"]]),
    "tool-call mix": ("output", "cat", lambda t: list(t["tools"])),
    "reply length": ("output", "num", lambda t: [len(t["reply"])]),
    "latency": ("output", "num", lambda t: [t["latency_ms"]]),
    "output+thinking tokens": ("output", "num", lambda t: [t["usage"]["output_tokens"] + t["usage"]["thinking_tokens"]]),
    "cache-read share": ("system", "num", lambda t: [t["usage"]["cache_read_input_tokens"] / t["usage"]["input_tokens"]]),
}
CHANGE_LOG = [("2026-08-31", "v15 on claude-opus-5 at 50% of traffic", {"model mix", "latency", "output+thinking tokens",
                                                                         "reply length"}),
              ("2026-09-07", "effort staircase on the v15 arm", {"latency", "output+thinking tokens"}),
              ("2026-09-09", "haiku fast-path canary", {"model mix", "latency", "output+thinking tokens"})]


class DriftMonitor:
    def __init__(self, reference: list[dict], *, reps: int = 150, seed: int = 6) -> None:
        self.reference = reference
        self.reps, self.seed = reps, seed

    def psi(self, name: str, window: list[dict]) -> float | None:
        """TODO: PSI of feature `name` between the reference and `window` (d6.psi on shares, or d6.psi_numeric)."""
        return None

    def noise_floor(self, name: str, n_window: int) -> float | None:
        """TODO: the 95th percentile of PSI between two resamples of the REFERENCE (sizes len(reference) and n_window)."""
        return None

    def check(self, window: list[dict]) -> dict[str, tuple[float, float, bool]] | None:
        """TODO: feature -> (psi, noise floor, drifted?) for one window."""
        return None


def daily_windows(traces: list[dict]) -> list[tuple[str, list[dict]]]:
    """(day, the traces of the 7 days ending that day) for every day from FIRST_DAY to LAST_DAY."""
    out = []
    day = dt.date.fromisoformat(FIRST_DAY)
    while day.isoformat() <= LAST_DAY:
        start = (day - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
        out.append((day.isoformat(), [t for t in traces if start <= d6.day_of(t) <= day.isoformat()]))
        day += dt.timedelta(days=1)
    return out


def main() -> None:
    header("Exercise 11 - a drift monitor (starter)")
    traces = d6.load_traces()
    reference = [t for t in traces if REFERENCE[0] <= d6.day_of(t) <= REFERENCE[1]]
    windows = daily_windows(traces)
    print(f"Reference {REFERENCE[0]}..{REFERENCE[1]}: {len(reference)} traces. {len(windows)} daily windows of "
          f"{WINDOW_DAYS} days, {min(len(w) for _, w in windows)}-{max(len(w) for _, w in windows)} traces each.")
    monitor = DriftMonitor(reference)
    step(1, "Check the first window")
    result = monitor.check(windows[0][1])
    if result is None:
        print("TODO: implement DriftMonitor.psi, noise_floor and check; then loop over the windows, record the first\n"
              "      day each feature drifts, and say whether the change log explains it.")
    else:
        for name, (value, floor, drifted) in result.items():
            print(f"  {name:<24} PSI {value:.3f}  floor {floor:.3f}  {'DRIFT' if drifted else ''}")
    print("\nSolution: advanced/day6_eval_science_release/solutions/ex11_drift_monitor.py")


if __name__ == "__main__":
    main()
