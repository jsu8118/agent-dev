"""Lab 01 - The context window as a budget: measure what fills it before you spend it.

Objective
    Use the token-counting endpoint to measure what each part of a field-service assistant's context costs -
    system prompt, tool definitions, every manual and policy, the whole corpus, raw telemetry and aggregated
    telemetry - then price "stuff everything into the prompt" against "retrieve what the question needs", and see
    how an agent loop re-sends its history on every turn.

Concepts
    context window as a budget; client.messages.count_tokens (never tiktoken); tool-definition overhead;
    chars-per-token varies by content; raw data vs aggregates; agent loops re-send history (cost ~ turns²);
    a 1M-token window is capacity, not a recommendation (context rot).

Run
    python day3_context_rag_memory/labs/01_token_budget.py [--questions-per-day 1000]

What to observe
    * Tool definitions are paid on every request, before the user says anything.
    * The whole manual + policy corpus is ~1% of Claude Opus 5's 1M window; one month of raw telemetry for the
      fleet is ~14% - and ~70% of Claude Haiku 4.5's 200K window.
    * Aggregating one asset's month of telemetry in code shrinks it by two orders of magnitude
      (720 raw rows -> one ~100-token JSON summary per metric).
    * Cost per question: cached long context and RAG are close for a small corpus; the gap opens as it grows.
    * Mock mode estimates tokens at ~3.8 chars/token; live counts come from Claude's tokenizer and differ.
"""
# test: expect=Budget table
# test: expect=Agent loop re-sending

from __future__ import annotations

import argparse
import json

from labkit import DATA_DIR, MODEL, get_client, header, is_mock, step, text_of

import _day3 as d3
import _tools as tools

QUESTION = "How often should the KP-250 bearings be regreased, and with how much grease?"
OUTPUT_TOKENS = 400          # a typical short technical answer, including adaptive thinking


def count(client, **kwargs) -> int:
    """Tokens for a request shape, from the API itself (model-specific tokenizer)."""
    return client.messages.count_tokens(model=MODEL, **kwargs).input_tokens


def measure(client) -> list[tuple[str, int, int]]:
    """(label, chars, tokens) for every context component."""
    base = count(client, messages=[{"role": "user", "content": "."}])
    rows: list[tuple[str, int, int]] = []

    def add(label: str, text: str, tokens: int) -> None:
        rows.append((label, len(text), tokens))

    system = tools.DIAGNOSTIC_SYSTEM
    add("system prompt (diagnostic assistant)", system,
        count(client, system=system, messages=[{"role": "user", "content": "."}]) - base)
    tools_json = json.dumps(tools.DIAGNOSTIC_TOOLS)
    add("tool definitions (5 tools + tool-use preamble)", tools_json,
        count(client, tools=tools.DIAGNOSTIC_TOOLS, messages=[{"role": "user", "content": "."}]) - base)
    docs = d3.load_docs()
    for doc in docs:
        add(f"  {doc.kind}: {doc.code} ({doc.doc_id})", doc.text,
            count(client, messages=[{"role": "user", "content": doc.text}]) - base)
    corpus = d3.library_text(docs)
    add("corpus: all 11 manuals + policies", corpus, count(client, messages=[{"role": "user", "content": corpus}]) - base)

    one_asset = d3.telemetry_csv("HF-KP250-03", with_asset_column=True)
    add("telemetry: 1 asset x 30 days, raw CSV (720 rows)", one_asset,
        count(client, messages=[{"role": "user", "content": one_asset}]) - base)
    fleet = (DATA_DIR / "maintenance" / "telemetry.csv").read_text(encoding="utf-8")
    add("telemetry: 12 assets x 30 days, raw CSV (8,640 rows)", fleet,
        count(client, messages=[{"role": "user", "content": fleet}]) - base)
    summary = tools.query_telemetry({"asset_id": "HF-KP250-03", "metric": "vibration_mm_s"})
    add("telemetry: 1 asset x 30 days, summary JSON (in code)", summary,
        count(client, messages=[{"role": "user", "content": summary}]) - base)
    chunks = "\n\n".join(f"[{c.cite()}]\n{c.text}" for _, c in tools.KB.search(QUESTION, 4))
    add("retrieval: top-4 BM25 sections for one question", chunks,
        count(client, messages=[{"role": "user", "content": chunks}]) - base)
    add("the question itself", QUESTION, count(client, messages=[{"role": "user", "content": QUESTION}]))
    return rows


def budget_table(rows: list[tuple[str, int, int]]) -> None:
    opus_window, haiku_window = 1_000_000, 200_000
    p_in = d3.input_price(MODEL)
    out = []
    for label, chars, tokens in rows:
        out.append([label, tokens, f"{chars / max(tokens, 1):.1f}", f"{tokens / opus_window:.2%}",
                    f"{tokens / haiku_window:.1%}", d3.money(tokens * p_in), d3.money(tokens * p_in * 0.1)])
    print("Budget table (tokens from count_tokens; $ = one request carrying this component on", MODEL + ")")
    d3.table(out, ["component", "tokens", "chars/tok", "% of 1M", "% of 200K", "$ uncached", "$ cache read"])


def per_question_costs(rows: list[tuple[str, int, int]], questions_per_day: int) -> dict[str, float]:
    tok = {label.strip(): t for label, _, t in rows}
    system = tok["system prompt (diagnostic assistant)"]
    corpus = tok["corpus: all 11 manuals + policies"]
    chunks = tok["retrieval: top-4 BM25 sections for one question"]
    question = tok["the question itself"]
    p_in, p_out = d3.input_price(MODEL), d3.output_price(MODEL)
    out = OUTPUT_TOKENS * p_out
    strategies = {
        "stuff corpus, no caching": (system + corpus + question) * p_in + out,
        "stuff corpus, cached prefix (steady state)": (system + corpus) * p_in * 0.1 + question * p_in + out,
        "RAG: top-4 sections, no caching": (system + chunks + question) * p_in + out,
        "RAG: cached system + top-4 sections": system * p_in * 0.1 + (chunks + question) * p_in + out,
    }
    table = [[name, d3.money(c), d3.money(c * questions_per_day), f"${c * questions_per_day * 30:,.0f}"]
             for name, c in strategies.items()]
    print(f"Assumptions: {OUTPUT_TOKENS} output tokens per answer; {questions_per_day:,} questions/day; 30 days/month.")
    d3.table(table, ["strategy", "$/question", "$/day", "$/month"])
    return strategies


def loop_math(rows: list[tuple[str, int, int]]) -> None:
    """Each agent turn re-sends system + tools + the whole conversation so far."""
    tok = {label.strip(): t for label, _, t in rows}
    prefix = tok["system prompt (diagnostic assistant)"] + tok["tool definitions (5 tools + tool-use preamble)"]
    delta = tok["telemetry: 1 asset x 30 days, summary JSON (in code)"] + 250   # one tool result + model text
    p_in = d3.input_price(MODEL)
    print(f"Agent loop re-sending: prefix P = {prefix:,} tokens (system + tools), each turn adds about "
          f"D = {delta:,} tokens (a tool result + the model's text).")
    print("Turn t sends P + (t-1)*D input tokens, so T turns send T*P + D*T*(T-1)/2 in total - quadratic in T.")
    rows_out = []
    for turns in (1, 5, 10, 20, 40):
        total = turns * prefix + delta * turns * (turns - 1) // 2
        # with caching: turn t reads what turn t-1 sent and writes only what is new
        cached = prefix * 1.25 + sum((prefix + (t - 2) * delta) * 0.1 + delta * 1.25 for t in range(2, turns + 1))
        last = prefix + (turns - 1) * delta
        rows_out.append([turns, last, total, d3.money(total * p_in), d3.money(cached * p_in),
                         f"{1 - cached / total:.0%}"])
    d3.table(rows_out, ["turns", "last prompt", "total input", "$ no cache", "$ cached", "saved"])


def billed_vs_counted(client) -> None:
    corpus = d3.library_text(d3.load_docs())
    system = f"<day3_budget_probe>\nAnswer from the documents below.\n\n{corpus}"
    messages = [{"role": "user", "content": QUESTION}]
    predicted = count(client, system=system, messages=messages)
    response = client.messages.create(model=MODEL, max_tokens=2000, system=system, messages=messages)
    billed = d3.prompt_size(response.usage)
    print(f"count_tokens predicted {predicted:,} input tokens; the request was billed {billed:,} "
          f"(input {response.usage.input_tokens:,} + cache write {response.usage.cache_creation_input_tokens or 0:,} "
          f"+ cache read {response.usage.cache_read_input_tokens or 0:,}).")
    print("Answer:", d3.money(d3.response_cost(response)), "-", " ".join(text_of(response).split())[:240])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions-per-day", type=int, default=1000)
    args = parser.parse_args()
    client = get_client()
    header("Lab 01 - The context window as a budget")
    if is_mock():
        print("(mock mode: token counts are deterministic estimates at ~3.8 chars/token, not Claude's tokenizer)")

    step(1, "Count every component with client.messages.count_tokens")
    rows = measure(client)
    budget_table(rows)

    step(2, "Price one technician question: stuff the corpus vs retrieve four sections")
    per_question_costs(rows, args.questions_per_day)

    step(3, "Agent loops re-send their history on every turn")
    loop_math(rows)

    step(4, "Check the estimate against a real bill")
    billed_vs_counted(client)


if __name__ == "__main__":
    main()
