"""Lab 04 - Agentic search: let the model retrieve (manuals AND data) through tools.

Objective
    Build a diagnostic agent whose context is assembled just in time by tools - search_manuals, read_section,
    query_telemetry (aggregations computed in code), list_work_orders - and have it diagnose the two pumps that the
    ground truth labels as bearing wear and cavitation. Compare it with one-shot RAG (retrieve once, answer once),
    with and without the raw telemetry pasted into the prompt, and grade every strategy against
    data/maintenance/ground_truth.json.

Concepts
    agentic search / progressive disclosure; retrieval as tools; lightweight identifiers then read on demand;
    structured data via tools (aggregations in code) vs dumping CSV into the prompt; parallel tool calls;
    structured final answers via a strict tool (submit_diagnosis); grading against ground truth; cost per task.

Run
    python day3_context_rag_memory/labs/04_agentic_search.py

What to observe
    * The agent's first turn fans out three tool calls in parallel (history + two summaries), then asks for exactly
      the extra view its hypothesis needs: daily trends for bearing wear, an hour-of-day profile for cavitation.
    * One-shot RAG retrieves with the question's words ("diagnose HF-KP250-03"), which say nothing about the
      symptoms - it gets generic sections and, without data, cannot diagnose.
    * Pasting 720 raw rows lets one-shot RAG diagnose too, for a similar price here - but its cost grows linearly
      with the history you paste (a year is ~12x more) and its numbers are the model's mental arithmetic. The
      agent's cost is flat in data volume and every number in its evidence was computed in code.
"""
# test: expect=Scoreboard

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from labkit import MODEL, get_client, header, is_mock, step, wrap

import _day3 as d3
import _tools as tools

ONESHOT_SYSTEM = """\
<day3_oneshot_diagnosis>
You are Kestrel's reliability assistant. Diagnose the asset using ONLY the material in the user turn. If it contains \
no measurements for the asset, return issue "undetermined" instead of guessing.
</day3_oneshot_diagnosis>"""


class Diagnosis(BaseModel):
    asset_id: str
    issue: Literal["bearing_wear", "cavitation", "misalignment", "sensor_fault", "overload_right_of_bep", "no_fault",
                   "undetermined"]
    confidence: Literal["low", "medium", "high"]
    evidence: list[str]
    recommended_actions: list[str]


def question_for(asset: str) -> str:
    return (f"Diagnose {asset}: what is wrong with it, how sure are you, and what should the technician do next? "
            "Use the condition-monitoring data, the maintenance history and the manuals.")


def run_agentic(client, asset: str) -> tuple[d3.AgentRun, dict | None]:
    desk = tools.DiagnosisDesk()
    params = dict(model=MODEL, max_tokens=8000, tools=tools.DIAGNOSTIC_TOOLS,
                  system=[{"type": "text", "text": tools.DIAGNOSTIC_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                  cache_control={"type": "ephemeral"})            # static prefix + moving tail, as in lab 02

    def on_turn(turn: d3.Turn, response) -> None:
        calls = ", ".join(f"{name}({', '.join(f'{k}={v}' for k, v in args.items() if k != 'evidence')})"[:95]
                          for name, args in turn.tool_calls) or "(final answer)"
        print(f"  turn {turn.number}: prompt {turn.prompt_tokens:>6,} tok (cache read {turn.cache_read:>6,})  "
              f"{d3.money(turn.cost)}  -> {calls}")

    run = d3.run_agent(client.messages.create, params=params,
                       messages=[{"role": "user", "content": question_for(asset)}],
                       tools=desk.tools(), max_turns=12, on_turn=on_turn)
    return run, (desk.submitted[-1] if desk.submitted else None)


def run_oneshot(client, asset: str, *, with_telemetry: bool) -> tuple[Diagnosis, object]:
    question = question_for(asset)
    hits = tools.KB.search(question, 4)                 # one retrieval, using only the question's words
    excerpts = "\n\n".join(f'<excerpt source="{c.cite()}">\n{c.text}\n</excerpt>' for _, c in hits)
    parts = [f"<manual_excerpts>\n{excerpts}\n</manual_excerpts>"]
    if with_telemetry:
        csv = d3.telemetry_csv(asset, with_asset_column=True)
        parts.append(f'<telemetry asset="{asset}" rows="{csv.count(chr(10)) - 1}">\n{csv}</telemetry>')
    parts.append(question)
    response = client.messages.parse(model=MODEL, max_tokens=4000, system=ONESHOT_SYSTEM, output_format=Diagnosis,
                                     messages=[{"role": "user", "content": "\n\n".join(parts)}])
    print(f"  retrieved: {', '.join(c.chunk_id for _, c in hits)}"
          + (" + 720 raw telemetry rows" if with_telemetry else ""))
    return response.parsed_output, response


def main() -> None:
    client = get_client()
    header("Lab 04 - Agentic search vs one-shot RAG")
    if is_mock():
        print("(mock mode: a rule-based stand-in plays the model; it decides from tool results only. Live Claude will "
              "choose its own tool sequence - the scoreboard, not the exact path, is what to compare)")
    truth = d3.ground_truth()
    targets = [next(a for a, g in truth.items() if g["issue"] == issue) for issue in ("bearing_wear", "cavitation")]
    board: list[list] = []

    for n, asset in enumerate(targets, 1):
        step(f"{n}a", f"Agentic search on {asset}")
        run, dx = run_agentic(client, asset)
        issue = dx["issue"] if dx else "(none submitted)"
        board.append([asset, "agentic (tools)", issue, "yes" if issue == truth[asset]["issue"] else "NO",
                      len(run.turns), run.total_prompt_tokens, sum(t.output_tokens for t in run.turns),
                      d3.money(run.cost)])
        print("  tool calls:", ", ".join(t["name"] for t in run.tool_log))
        if dx:
            print("  submitted evidence:")
            for e in dx["evidence"]:
                print(wrap(f"- {e}", "    "))
            print(f"  manual references: {'; '.join(dx['manual_references'])}")
        print(f"  ground truth: {truth[asset]['issue']} - {truth[asset]['evidence']}")
        print("  technician summary:\n" + wrap(run.final_text, "    "))

        for with_data in (False, True):
            label = "one-shot RAG + raw CSV" if with_data else "one-shot RAG"
            step(f"{n}{'c' if with_data else 'b'}", f"{label} on {asset}")
            dx1, response = run_oneshot(client, asset, with_telemetry=with_data)
            print(f"  -> {dx1.issue} ({dx1.confidence}): {dx1.evidence[0][:160]}")
            board.append([asset, label, dx1.issue, "yes" if dx1.issue == truth[asset]["issue"] else "NO", 1,
                          d3.prompt_size(response.usage), response.usage.output_tokens,
                          d3.money(d3.response_cost(response))])

    step(3, "Scoreboard (graded against data/maintenance/ground_truth.json)")
    d3.table(board, ["asset", "strategy", "issue", "correct", "LLM calls", "input tok", "output tok", "cost"])
    raw = [r for r in board if r[1] == "one-shot RAG + raw CSV"]
    per_q = sum(r[5] for r in raw) / len(raw)
    print(f"\nProjection: the raw-CSV prompt averaged {per_q:,.0f} tokens for 30 days of one asset; a year of history "
          f"would be ~{per_q * 365 / 30:,.0f} tokens (~{d3.money(per_q * 365 / 30 * d3.input_price(MODEL))} of input per "
          "question), while the agent's tool results stay the same size.")
    print("\nRead it as cost per *correct* diagnosis: one-shot RAG without data is cheap and useless here; raw rows "
          "work but scale with the data (a year of hourly data is ~12x more) and the arithmetic is unauditable; "
          "the agent pays for a few round trips and gets evidence it can cite.")
    print("Tool results are small because the tools aggregate in code, e.g.:")
    print(wrap(json.dumps(d3.query_telemetry(targets[0], "vibration_mm_s", agg="summary"), ensure_ascii=False)[:400]))


if __name__ == "__main__":
    main()
