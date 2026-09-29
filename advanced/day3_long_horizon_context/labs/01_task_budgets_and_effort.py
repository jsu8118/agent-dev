"""Lab 01 - Task budgets and per-message effort: pace a 40-turn day instead of capping every answer.

Objective
    Replay the technician's day (six sites, 40 messages) on Claude Opus 5 four ways: a fixed max_tokens cap, fixed
    effort with a generous cap, and a task budget with per-message effort changes at two budget sizes. Compare what
    each does to the answers (truncations, a degraded end-of-day report), to output tokens and to cost. Then run one
    site visit on Claude Fable 5.1 with all three surfaces at once - task budget, per-message effort and progress
    updates - and ask the API which model accepts which surface.

Concepts
    max_tokens (a per-response ceiling the model cannot see) vs output_config.task_budget (beta task-budgets-2026-03-13,
    total >= 20,000: a countdown the model paces against across the turns you keep sending) vs output_config.effort
    (thinking depth) vs per-message effort (beta mid-conversation-output-config-2026-07-01: a system message with empty
    content and output_config.effort, latest wins, no cache reset), stop_reason "max_tokens", progress updates
    (thinking.display "updates", beta thinking-display-updates-2026-08-18), what a task budget counts.

Run
    python advanced/day3_long_horizon_context/labs/01_task_budgets_and_effort.py [--turns 40]
    Live, the four full-day arms cost about $9 on Claude Opus 5; --turns 14 replays the first two sites for ~$3.

What to observe
    * The 512-token cap cuts the longest answers mid-sentence (stop_reason max_tokens) - two arrival summaries
      and the end-of-day report - while the budget arms finish every turn.
    * Per-message effort lowers output on routine turns; the day's bill barely moves because input dominates it.
    * The budget arms print their pacing: output per site falls as the countdown runs down, and the tight budget
      runs out at Westfield - its end-of-day report is one line. The 'output + tool results' column is what a task
      budget counts, site by site (exercise 7 sizes a budget from it).
    * On Fable 5.1 the progress updates arrive as thinking blocks with text, one before each tool call.
    * The support matrix: Opus 5 rejects display "updates"; Fable 5 rejects per-message effort.
"""
# test: expect=Pacing
# test: expect=progress update
# test: expect=essentials only

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic  # noqa: E402

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day3 as d3  # noqa: E402

FABLE = "claude-fable-5-1"
BUDGET_BETA = "task-budgets-2026-03-13"
EFFORT_BETA = "mid-conversation-output-config-2026-07-01"
UPDATES_BETA = "thinking-display-updates-2026-08-18"
SYSTEM = [{"type": "text", "text": d3.FIELD_SYSTEM, "cache_control": {"type": "ephemeral"}}]

# How hard each kind of turn deserves to think: a diagnosis from telemetry is worth high effort; "noted" is not.
EFFORT_BY_KIND = {"arrive": "medium", "lookup": "medium", "telemetry": "high", "log": "low", "depart": "low",
                  "report": "medium", "recall": "low"}
LETTER = {"low": "L", "medium": "M", "high": "H", "xhigh": "X", "max": "!"}
CAP = 512                     # a common "safe" max_tokens for chat-sized answers


def params(model: str = MODEL, **extra) -> dict:
    return dict(model=model, tools=d3.FIELD_TOOLS, system=SYSTEM, cache_control={"type": "ephemeral"}, **extra)


def cold_start(label: str, steps: list[d3.Step]) -> list[d3.Step]:
    """A distinct first line per arm gives each run its own conversation cache (the system + tools prefix is still
    shared, as it is across conversations in production) - so the arms' costs are comparable."""
    first = steps[0]
    return [d3.Step(first.number, first.site, first.kind, f"({label}) {first.text}")] + steps[1:]


def effort_hook(levels: list[str]):
    """before_turn hook: append a per-message effort change when the turn's kind calls for another level."""
    state = {"current": None}

    def before_turn(s: d3.Step, messages: list[dict]) -> None:
        level = EFFORT_BY_KIND[s.kind]
        if messages and level != state["current"]:          # turn 1 takes the request's top-level effort
            messages.append({"role": "system", "content": [], "output_config": {"effort": level}})
            state["current"] = level
        elif not messages:
            state["current"] = level
        levels.append(state["current"])
    return before_turn


def run_arm(client, label: str, steps: list[d3.Step], *, max_tokens: int, budget: int | None = None,
            per_message_effort: bool = False) -> tuple[d3.DayRun, list[str]]:
    betas, config = [], {"effort": "high"}
    if budget:
        betas.append(BUDGET_BETA)
        config["task_budget"] = {"type": "tokens", "total": budget}
    levels: list[str] = []
    hooks = {}
    if per_message_effort:
        betas.append(EFFORT_BETA)
        config["effort"] = EFFORT_BY_KIND[steps[0].kind]
        hooks["before_turn"] = effort_hook(levels)
    create = client.beta.messages.create if betas else client.messages.create
    extra = {"betas": betas} if betas else {}
    day = d3.run_day(create, params(max_tokens=max_tokens, output_config=config, **extra),
                     steps=cold_start(label, steps), **hooks)
    return day, levels


def arm_row(label: str, day: d3.DayRun) -> list:
    report = [t for t in day.turns if t.number == 39]
    report_note = "-" if not report else ("TRUNCATED" if report[0].stop_reason == "max_tokens" else
                                          "one line" if "essentials only" in report[0].reply else "complete")
    return [label, len(day.turns), len(day.truncated), day.output_tokens, d3.money(day.cost), report_note]


def results_per_turn(day: d3.DayRun) -> list[int]:
    """Tool-result tokens read in each turn: the history split at the technician's messages."""
    starts = [i for i, m in enumerate(day.messages) if m["role"] == "user" and not (
        isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"]))]
    bounds = starts[1:] + [len(day.messages)]
    return [d3.tool_result_tokens(day.messages[a:b]) for a, b in zip(starts, bounds)]


def pacing_table(day: d3.DayRun, levels: list[str], budget: int) -> None:
    rows, results = [], results_per_turn(day)
    for site in d3.SITE_ORDER:
        turns = [t for t in day.turns if t.site == site]
        if not turns:
            continue
        idx = [day.turns.index(t) for t in turns]
        out = sum(t.output for t in turns)
        rows.append([site, len(turns), " ".join(LETTER.get(levels[i], "?") for i in idx) if levels else "-",
                     out, round(out / len(turns)), out + sum(results[i] for i in idx)])
    d3.table(rows, ["site", "turns", "effort per turn", "output", "output/turn", "output + tool results"])
    output, results = sum(t.output for t in day.turns), d3.tool_result_tokens(day.messages)
    spent = output + results
    print(f"  Spend the harness can observe: {output:,} output + {results:,} tool-result tokens = {spent:,} of "
          f"{budget:,} ({max(budget - spent, 0) / budget:.0%} left).")


def step_arms(client, steps: list[d3.Step]) -> None:
    print("Same 40 messages, same tools, four request policies (Claude Opus 5, effort high unless changed):")
    cap, _ = run_arm(client, "cap", steps, max_tokens=CAP)
    fixed, _ = run_arm(client, "fixed", steps, max_tokens=16000)
    roomy_budget, tight_budget = 100_000, 64_000
    roomy, roomy_levels = run_arm(client, "budget-100k", steps, max_tokens=16000, budget=roomy_budget,
                                  per_message_effort=True)
    tight, tight_levels = run_arm(client, "budget-64k", steps, max_tokens=16000, budget=tight_budget,
                                  per_message_effort=True)
    d3.table([arm_row(f"max_tokens={CAP}, effort high", cap), arm_row("max_tokens=16000, effort high", fixed),
              arm_row("task budget 100k + per-message effort", roomy),
              arm_row("task budget 64k + per-message effort", tight)],
             ["policy", "turns", "truncated", "output tokens", "cost", "day report"])

    cut = cap.truncated
    if cut:
        kinds = sorted({d3.SCRIPT[t.number - 1].kind for t in cut})
        print(f"\nThe cap cut {len(cut)} answers mid-sentence (stop_reason=max_tokens), turns "
              f"{', '.join(str(t.number) for t in cut)} ({', '.join(kinds)}). The model never knew the limit was "
              "there. The end-of-day report, as the technician received it:")
        report = next((t for t in cut if t.number == 39), cut[-1])
        print(wrap("..." + report.reply[-300:] + " [cut]"))
    print(f"\nPer-message effort saved {fixed.output_tokens - roomy.output_tokens:,} output tokens "
          f"({1 - roomy.output_tokens / fixed.output_tokens:.0%}) but only {d3.money(fixed.cost - roomy.cost)} of a "
          f"{d3.money(fixed.cost)} day: input is {1 - _output_share(fixed):.0%} of this bill - output is not where a "
          "long session's money goes.")

    step("2b", "Pacing: what the budget did, site by site")
    print(f"Task budget {roomy_budget:,} tokens (effort per turn: L low, M medium, H high):")
    pacing_table(roomy, roomy_levels, roomy_budget)
    print(f"\nTask budget {tight_budget:,} tokens - sized below what the day needs:")
    pacing_table(tight, tight_levels, tight_budget)
    last = tight.turns[-2] if len(tight.turns) >= 2 else tight.turns[-1]
    print(f"\nTurn {last.number} under the tight budget:")
    print(wrap(last.reply[:400]))
    if is_mock():
        print("[mock] The stand-in paces against its own reading of the countdown (what it generated plus the tool "
              "results it read): below half it thinks less, below a quarter less still, and at zero it answers with "
              "essentials only. Live Claude paces with judgement - the shape, not the thresholds, is the lesson.")


def _output_share(day: d3.DayRun) -> float:
    out_cost = day.output_tokens * d3.price(MODEL, "output")
    return out_cost / day.cost


def step_fable(client, steps: list[d3.Step]) -> None:
    site = [s for s in steps if s.site == "gbwd"]
    betas = [BUDGET_BETA, EFFORT_BETA, UPDATES_BETA]
    levels: list[str] = []
    printed = {"n": 0}

    def show(s: d3.Step, stats: d3.TurnStats) -> None:
        for r in stats.responses:
            for b in r.content:
                if b.type == "thinking" and b.thinking:
                    printed["n"] += 1
                    print(f"  turn {s.number:>2}  progress update: {b.thinking}")

    day = d3.run_day(client.beta.messages.create,
                     params(FABLE, max_tokens=16000, betas=betas,
                            thinking={"type": "adaptive", "display": "updates"},
                            output_config={"effort": EFFORT_BY_KIND[site[0].kind],
                                           "task_budget": {"type": "tokens", "total": 40_000}}),
                     steps=cold_start("fable", site), before_turn=effort_hook(levels), on_turn=show)
    print(f"\n{printed['n']} progress updates across {len(day.turns)} turns; each is a thinking block with text, placed "
          "right before the tool call it announces. Reasoning stays hidden (display 'updates' shows only these). "
          f"The visit cost {d3.money(day.cost)} on Fable 5.1 ($10/$50 per MTok, cache reads 0.025x).")


def probe(client, model: str, surface: str) -> str:
    msgs = [{"role": "user", "content": "Plan the visit."}, {"role": "assistant", "content": "Plan: brief, log, checks."}]
    kwargs: dict = dict(model=model, max_tokens=300)
    if surface == "per-message effort":
        kwargs.update(betas=[EFFORT_BETA], messages=msgs + [
            {"role": "system", "content": [], "output_config": {"effort": "low"}},
            {"role": "user", "content": "Now just confirm."}])
    elif surface == "task budget":
        kwargs.update(betas=[BUDGET_BETA], messages=msgs[:1],
                      output_config={"task_budget": {"type": "tokens", "total": 20_000}})
    else:
        kwargs.update(betas=[UPDATES_BETA], messages=msgs[:1], thinking={"type": "adaptive", "display": "updates"})
    try:
        client.beta.messages.create(**kwargs)
        return "ok"
    except anthropic.BadRequestError as exc:
        return "400: " + d3.api_error(exc)


def step_matrix(client) -> None:
    surfaces = ["task budget", "per-message effort", "display updates"]
    rows, notes = [], []
    for m in (MODEL, "claude-fable-5", FABLE):
        row = [m]
        for surface in surfaces:
            verdict = probe(client, m, surface)
            if verdict != "ok":
                notes.append(verdict[5:])
                verdict = f"400 ({len(notes)})"
            row.append(verdict)
        rows.append(row)
    d3.table(rows, ["model"] + surfaces)
    for i, note in enumerate(notes, 1):
        print(f"  ({i}) {note}")
    print("Ask the API, not your memory: the same request shape is accepted by one model and rejected by the next. "
          "Opus 5 has no display 'updates' - its between-tool narration is plain text; Fable 5 has no per-turn effort.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=40, help="replay only the first N messages of the day")
    args = parser.parse_args()
    steps = d3.SCRIPT[: args.turns]
    client = get_client()
    header("Lab 01 - Task budgets and per-message effort")
    if is_mock():
        print("[mock] The stand-in's thinking volume follows the effort in force and its pacing follows the "
              "task budget; token counts are estimates.")

    step(1, "The day, and the three levers")
    kinds = {}
    for s in steps:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    print(f"{len(steps)} technician messages across {len({s.site for s in steps})} sites: "
          + ", ".join(f"{n} {k}" for k, n in kinds.items()) + ".")
    d3.table([["max_tokens", "one response", "no", "hard cut: stop_reason max_tokens"],
              ["output_config.effort", "one request (or per message, beta)", "yes", "thinks less, shorter answers"],
              ["output_config.task_budget", "every turn you keep sending", "yes (countdown)", "paces; wraps up"]],
             ["lever", "scope", "model sees it", "when it binds"])

    step(2, "Four request policies over the same day")
    step_arms(client, steps)

    step(3, "One site visit on Claude Fable 5.1: budget + per-message effort + progress updates")
    step_fable(client, steps)

    step(4, "Which model accepts which surface")
    step_matrix(client)


if __name__ == "__main__":
    main()
