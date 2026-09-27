"""Lab 06 - Long-running agents: context growth, trimming, server-side tool-result clearing, compaction.

Objective
    Run the same tool-heavy session - a weekly fleet review that pulls 14 days of raw hourly telemetry for all 12
    pumps, site by site - four times: with no context management, with client-side trimming, with server-side
    tool-result clearing (context editing), and with server-side compaction. Compare peak context, billed tokens,
    cache behaviour, cost, and what the model still "knows" at the end.

Concepts
    context growth in agent loops; client-side pruning at natural boundaries (code-computed digests);
    clear_tool_uses_20250919 (trigger / keep / clear_at_least / exclude_tools) and context_management.applied_edits;
    compact_20260112 (trigger >= 50,000 input tokens, instructions) and the compaction block you must keep by appending
    the FULL response.content; usage.iterations; every edit rewrites the prefix, so it costs a cache write.

Run
    python day3_context_rag_memory/labs/06_context_editing_and_compaction.py [--strategies none,trim,clear,compact]
    Live mode sends ~0.5M input tokens across the four runs (mostly cache reads); pick fewer strategies to save.

What to observe
    * Baseline: every turn re-sends all raw rows fetched so far; the last request carries ~52K tokens (mock estimate).
    * Trimming: old raw results become one-line digests computed in code; peak context drops by more than half,
      at the cost of one cache miss per prune.
    * Clearing: the client keeps the full history, the API drops old tool results before the model reads them
      (applied_edits reports how many and how many tokens); the model keeps its own notes.
    * Compaction: once the prompt passes the trigger, the API summarises the history into a `compaction` block;
      the follow-up question works because we appended the full response.content. Whatever the summary dropped is
      gone - check what survived.
"""
# test: expect=What the model still knows

from __future__ import annotations

import argparse
import re

from labkit import MODEL, get_client, header, is_mock, step, text_of, wrap

import _day3 as d3
import _tools as tools

FOLLOW_UP = "Which assets need action or a work order this week?"
COMPACTION_INSTRUCTIONS = (
    "Summarize this fleet review so it can continue without the raw data. Keep verbatim: every per-asset note line "
    "(asset id, numbers, status, reason), which sites and assets are done and which remain, and the user's requests. "
    "Drop raw telemetry rows.")
ASSET_RE = re.compile(r"\b[A-Z]{2}-KP\d{3}X?-\d{2}\b")
CLEAR_TRIGGER, COMPACT_TRIGGER = 30_000, 50_000     # 50,000 is the documented minimum for compact_20260112


def trim_old_results(messages: list[dict]) -> int:
    """Replace raw telemetry the model has already seen (everything before the newest tool results) by digests."""
    trimmed = 0
    for m in messages[:-1]:
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for block in m["content"]:
            if (isinstance(block, dict) and block.get("type") == "tool_result"
                    and isinstance(block.get("content"), str) and block["content"].startswith("# asset ")):
                block["content"] = tools.digest_telemetry(block["content"])
                trimmed += 1
    return trimmed


def known_assets(text: str) -> set[str]:
    """Assets the final answer still reports with numbers (a crude but honest 'what does it still know')."""
    out = set()
    for line in text.splitlines():
        for a in ASSET_RE.findall(line):
            if re.search(r"\d+\.\d", line.replace(a, "")):
                out.add(a)
    return out


def run_strategy(client, strategy: str) -> dict:
    params: dict = dict(model=MODEL, max_tokens=12000, tools=tools.FLEET_TOOLS,
                        system=[{"type": "text", "text": tools.FLEET_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                        cache_control={"type": "ephemeral"})
    create = client.messages.create
    if strategy == "clear":
        create = client.beta.messages.create
        params.update(betas=["context-management-2025-06-27"], context_management={"edits": [{
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": CLEAR_TRIGGER},
            "keep": {"type": "tool_uses", "value": 4},                  # one site's worth of results
            "clear_at_least": {"type": "input_tokens", "value": 10_000},  # make each cache break worth it
            "exclude_tools": ["list_assets"]}]})                        # small and needed until the end
    elif strategy == "compact":
        create = client.beta.messages.create
        params.update(betas=["compact-2026-01-12"], context_management={"edits": [{
            "type": "compact_20260112", "trigger": {"type": "input_tokens", "value": COMPACT_TRIGGER},
            "instructions": COMPACTION_INSTRUCTIONS}]})

    history: list[int] = []
    trims: list[int] = []

    # beta strategies count with the beta endpoint: the history may hold beta content (a compaction block), and
    # count_tokens honours an existing compaction block (it counts from the block on - what the API will read)
    counter = client.beta.messages.count_tokens if "betas" in params else client.messages.count_tokens
    count_kwargs = {"betas": params["betas"]} if "betas" in params else {}

    def before(messages: list[dict], turn: int) -> None:
        if strategy == "trim":
            trims.append(trim_old_results(messages))
        history.append(counter(model=MODEL, system=params["system"], tools=params["tools"], messages=messages,
                               **count_kwargs).input_tokens)

    rows: list[list] = []
    extra: list[str] = []

    def on_turn(turn: d3.Turn, response) -> None:
        note = ""
        if turn.applied_edits:
            e = turn.applied_edits[0]
            note = f"cleared {e.get('cleared_tool_uses')} results ({e.get('cleared_input_tokens'):,} tok)"
        if turn.compacted:
            block = next(b for b in response.content if b.type == "compaction")
            iters = [(it.type, it.input_tokens, it.output_tokens) for it in response.usage.iterations or []]
            note = f"COMPACTED: iterations {iters}"
            extra.append(block.content or "")
        if strategy == "trim" and trims and trims[-1]:
            note = f"trimmed {trims[-1]} old results to digests"
        calls = len(turn.tool_calls)
        rows.append([turn.number, history[-1], turn.prompt_tokens, turn.cache_read, turn.cache_write,
                     f"{calls} tool calls" if calls else "text", note])

    # a distinct first line per strategy gives each run a cold conversation cache (the system + tools prefix is still
    # shared across runs, as it would be across conversations in production) - so the costs are comparable
    messages = [{"role": "user", "content": f"(review run: {strategy})\n{tools.FLEET_REQUEST}"}]
    run = d3.run_agent(create, params=params, messages=messages, tools=tools.FLEET_TOOL_FUNCS, max_turns=10,
                       before_request=before, on_turn=on_turn)
    report = run.final_text
    # a follow-up question in the same conversation (the compaction block, if any, is already in `messages`)
    messages.append({"role": "user", "content": FOLLOW_UP})
    before(messages, len(run.turns) + 1)
    follow = create(messages=messages, **params)
    messages.append({"role": "assistant", "content": follow.content})
    follow_turn = d3.turn_from(len(run.turns) + 1, follow, MODEL)
    on_turn(follow_turn, follow)
    run.turns.append(follow_turn)
    d3.table(rows, ["turn", "history sent", "model read", "cache read", "cache write", "output", "context management"])
    follow_text = text_of(follow)
    return {"strategy": strategy, "run": run, "report": report, "follow": follow_text, "summaries": extra,
            "final_history": history[-1], "known": known_assets(report)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategies", default="none,trim,clear,compact")
    args = parser.parse_args()
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    client = get_client()
    header("Lab 06 - Context growth, trimming, tool-result clearing and compaction")
    if is_mock():
        print("(mock mode: token counts are estimates; the API-side clearing and compaction are simulated with the "
              "documented semantics. The simulated compaction summary keeps only the user requests and the list of "
              "tool calls - a real Claude summary follows your `instructions`.)")
    titles = {"none": "No context management (baseline)", "trim": "Client-side trimming at natural boundaries",
              "clear": "Server-side tool-result clearing (clear_tool_uses_20250919)",
              "compact": "Server-side compaction (compact_20260112)"}
    results = []
    for i, strategy in enumerate(strategies, 1):
        step(i, titles[strategy])
        res = run_strategy(client, strategy)
        results.append(res)
        lines = res["report"].splitlines()
        summary = next((line for line in lines if "fleet report" in line.lower()), "(no fleet report line)")
        print(f"Final report: {summary}")
        for line in [line for line in lines if "-> ACTION" in line and line.startswith("- ")][:3]:
            m = re.match(r"- (\S+) .*?-> (ACTION.*)$", line)
            print(f"   {m.group(1)} -> {m.group(2)}"[:160] if m else f"   {line[:160]}")
        if res["summaries"]:
            print("Compaction block content (all the model keeps from before the compaction point):\n"
                  + wrap(res["summaries"][0][:700]))
        flagged = sorted(set(ASSET_RE.findall(res["follow"])))
        print(f"Follow-up {FOLLOW_UP!r} -> flags {len(flagged)}: {', '.join(flagged) or 'none'}")

    step(len(strategies) + 1, "What the model still knows - comparison")
    rows = []
    p_in = d3.input_price(MODEL)
    for res in results:
        run: d3.AgentRun = res["run"]
        billed = sum(t.billed_prompt for t in run.turns)
        read_share = sum(t.cache_read for t in run.turns) / max(billed, 1)
        urgent = len(set(ASSET_RE.findall(res["follow"])))
        rows.append([res["strategy"], len(run.turns), run.peak_prompt_tokens, res["final_history"],
                     billed, f"{read_share:.0%}", d3.money(run.cost),
                     f"{len(res['known'])}/{len(d3.assets())}", urgent,
                     d3.money(run.turns[-1].prompt_tokens * p_in * 0.1)])
    d3.table(rows, ["strategy", "calls", "peak read", "history sent", "total prompt", "cache-read share",
                    "cost", "assets w/ numbers", "urgent flagged", "next turn in"])
    print("\n'history sent' = count_tokens of the messages list your code sends at the end (server-side edits happen "
          "after that; live, count_tokens already skips content before a compaction block); 'peak read' = largest "
          "prompt the model processed to answer; 'total prompt' sums every request, compaction's summarisation pass "
          "included; 'next turn in' = input cost of one more turn at the final context size, read from cache.")
    base = next((r for r in results if r["strategy"] == "none"), None)
    for res in results:
        if base is None or res is base:
            continue
        extra = res["run"].cost - base["run"].cost
        saving = (base["run"].turns[-1].prompt_tokens - res["run"].turns[-1].prompt_tokens) * p_in * 0.1
        if extra > 0 and saving > 0:
            print(f"{res['strategy']}: costs {d3.money(extra)} more so far; it pays for itself after ~"
                  f"{extra / saving:.0f} more turns of conversation (and it is what keeps a long session inside the "
                  "window at all).")
        elif saving > 0:
            print(f"{res['strategy']}: already cheaper by {d3.money(-extra)}, and each further turn saves "
                  f"{d3.money(saving)} of cached input.")


if __name__ == "__main__":
    main()
