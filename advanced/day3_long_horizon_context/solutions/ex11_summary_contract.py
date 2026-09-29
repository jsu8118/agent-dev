"""Solution to exercise 11 - a summary contract that keeps what the probes need, and what it costs.

Objective
    Keep lab 02's harness exactly as it is and change only the contract: summary instructions that also ask for the
    latest controller-log event per fault code and the operators' dated notes, and a scratchpad schema with the same
    two fields (rendered for the model). Add a ninth probe - the fan module's hour counter at Riverbend, said only in an
    operator note - and compare both strategies before and after.

Concepts
    the summary contract as the thing you design (not the summariser); instructions and schemas as two forms of the
    same contract; probes written from what a summary is likely to drop; the token and dollar cost of a richer
    contract.

Run
    python advanced/day3_long_horizon_context/solutions/ex11_summary_contract.py

What to observe
    * With lab 02's contract both strategies miss the heatsink and the fan-counter probes.
    * With the richer contract both answer all nine; the summaries grow by a few hundred tokens.
"""
# test: expect=9/9

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ex11_starter", HERE.parents[0] / "exercises" / "ex11_summary_contract.py")
starter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(starter)

from labkit import get_client  # noqa: E402

d3 = starter.d3

RICHER_INSTRUCTIONS = starter.SUMMARY_INSTRUCTIONS.replace(
    "Drop raw readings and log exports.",
    "Also keep, per site, the latest controller-log entry for each fault code (with its measurements) and every dated "
    "operator note from the shift logs. Drop the hourly readings.")
RICHER_SCHEMA = copy.deepcopy(starter.SCRATCHPAD_SCHEMA)
RICHER_SCHEMA["properties"]["fault_events"] = {"type": "array", "items": {"type": "string"},
                                               "description": "latest controller-log entry per fault code, verbatim"}
RICHER_SCHEMA["properties"]["operator_notes"] = {"type": "array", "items": {"type": "string"},
                                                 "description": "dated operator notes from the log export, verbatim"}
RICHER_SCHEMA["required"] += ["fault_events", "operator_notes"]
FAN = d3.Probe("fan_hours", f"{d3.PROBE_PREFIX} what did Riverbend's fan module hour counter read?", (r"34,120",),
               "an operator note in the controller log (turn 15)")


def render_richer(pad: dict[str, dict]) -> str:
    lines = starter.render_scratchpad(pad).splitlines()[:-1]
    for site, e in pad.items():
        lines += [f"fault event ({site}): {ev}" for ev in e.get("fault_events", [])]
        lines += [f"operator note ({site}): {n}" for n in e.get("operator_notes", [])]
    return "\n".join(lines + ["</scratchpad>"])


def main() -> None:
    client = get_client()
    print("Exercise 11 (solution) - the contract, before and after")
    rows = []
    for strategy in ("summary", "scratchpad"):
        before = starter.run(client, strategy, instructions=starter.SUMMARY_INSTRUCTIONS,
                             schema=copy.deepcopy(starter.SCRATCHPAD_SCHEMA), render=starter.render_scratchpad,
                             probes=[FAN])
        after = starter.run(client, strategy, instructions=RICHER_INSTRUCTIONS, schema=copy.deepcopy(RICHER_SCHEMA),
                            render=render_richer, probes=[FAN])
        rows.append(starter.report(f"{strategy}, lab 02 contract", before))
        rows.append(starter.report(f"{strategy}, richer contract", after))
    d3.table(rows, ["strategy and contract", "probes", "missed", "last summary (tokens)", "day cost"])
    print("The harness did not change - only what the contract asks the model to carry forward. Every field you add "
          "is paid for on every later turn (the summary is re-read), so add fields for facts a probe proves you need.")


if __name__ == "__main__":
    main()
