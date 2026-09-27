"""Lab 05 - Measure retrieval before you tune prompts: hit@k and MRR on a labelled question set.

Objective
    Score four BM25 variants on 20 technician questions with gold (document, section) labels stored in
    day3_context_rag_memory/data/retrieval_questions.jsonl: section chunks with and without contextual headers,
    and fixed-size windows with and without headers. Sweep k to see the recall / context-cost trade-off, and read
    the failures - they tell you what to fix next. No LLM calls: retrieval quality is measurable on its own.

Concepts
    chunking by document structure vs fixed windows; contextual chunk headers (title + section path); BM25 length
    normalisation; hit@k (recall of the gold section in the top k), MRR (how high it ranks), precision@k;
    context cost per k; failure analysis (vocabulary mismatch, missing stemming, long chunks).

Run
    python day3_context_rag_memory/labs/05_retrieval_eval.py [--size 500 --overlap 100]

What to observe
    * Headers help both chunkings: the words that identify a section ("KP-400", "Operating limits") often live in
      the heading, not in the body.
    * Windows with headers rescue the long fault-code table (F10) that section-level BM25 buries (length
      normalisation penalises long chunks), but windows lose section context on other questions.
    * Some questions fail for every variant: "fault history" vs "diagnostic log", "lowest flow" vs "minimum
      continuous flow", "continuously" vs "continuous". Lexical retrieval needs the user's words - that is the
      case for hybrid (subword/semantic) retrieval, query rewriting, or agentic search (exercise 7).
"""
# test: expect=hit@1
# test: expect=Failure analysis

from __future__ import annotations

import argparse

from labkit import header, step

import _day3 as d3
from _retrieval import BM25, Chunk, evaluate, gold_spans, is_relevant, load_docs, load_questions, section_chunks, \
    window_chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=500, help="window size in characters")
    parser.add_argument("--overlap", type=int, default=100, help="window overlap in characters")
    args = parser.parse_args()
    header("Lab 05 - Retrieval evaluation: hit@k and MRR")

    step(1, "Corpus, chunkings and the labelled question set")
    docs = load_docs()
    sections = section_chunks(docs)
    windows = window_chunks(docs, size=args.size, overlap=args.overlap)
    questions = load_questions()
    kinds: dict[str, int] = {}
    for q in questions:
        kinds[q["type"]] = kinds.get(q["type"], 0) + 1
    avg = lambda cs: sum(len(c.text) for c in cs) / len(cs)
    print(f"{len(docs)} documents -> {len(sections)} section chunks (avg {avg(sections):.0f} chars) or "
          f"{len(windows)} windows of ~{args.size} chars with ~{args.overlap} overlap (avg {avg(windows):.0f} chars).")
    print(f"{len(questions)} questions: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))
    print(f"Example: {questions[0]['id']} {questions[0]['question']!r} -> gold {questions[0]['gold']}")
    print("A retrieved chunk counts as relevant if at least half of it (or half of the gold section, if shorter) "
          "lies inside a gold section - so sections and windows are scored by the same rule.")

    step(2, "Score four variants")
    variants = [
        ("sections + headers", BM25(sections, Chunk.with_header), sections),
        ("sections, body only", BM25(sections, lambda c: c.text), sections),
        ("windows, body only", BM25(windows, lambda c: c.text), windows),
        ("windows + headers", BM25(windows, Chunk.with_header), windows),
    ]
    results = []
    rows = []
    for name, index, _ in variants:
        r = evaluate(name, index, questions, sections)
        results.append(r)
        rows.append([name, f"{r.hit[1]:.2f}", f"{r.hit[3]:.2f}", f"{r.hit[5]:.2f}", f"{r.mrr:.3f}",
                     f"{r.avg_chars_at_3 / 3.8:,.0f}"])
    d3.table(rows, ["variant", "hit@1", "hit@3", "hit@5", "MRR@10", "~tokens in top-3"])

    step(3, "The k trade-off: more chunks = more recall, more tokens, lower precision")
    name, index, _ = variants[0]
    rows = []
    for k in (1, 2, 3, 4, 5, 8):
        hits = prec = chars = 0
        for q in questions:
            spans = gold_spans(q, sections)
            top = index.search(q["question"], k)
            rel = [is_relevant(c, spans) for _, c in top]
            hits += any(rel)
            prec += sum(rel) / k
            chars += sum(len(c.text) for _, c in top)
        n = len(questions)
        rows.append([k, f"{hits / n:.2f}", f"{prec / n:.2f}", f"{chars / n / 3.8:,.0f}"])
    print(f"variant: {name}")
    d3.table(rows, ["k", "hit@k (recall)", "precision@k", "~tokens retrieved"])

    step(4, "Failure analysis: where did the gold section rank?")
    ids = [q["id"] for q in questions]
    rows = []
    for q in questions:
        ranks = [r.ranks[q["id"]] for r in results]
        if all(x == 1 for x in ranks):
            continue
        rows.append([q["id"], q["type"], *[x if x else "-" for x in ranks], q["question"][:58]])
    print(f"{len(ids) - len(rows)} of {len(ids)} questions are rank 1 for every variant; the rest ('-' = not in top 10):")
    d3.table(rows, ["id", "type", "sec+hdr", "sec", "win", "win+hdr", "question"])
    bad = next(q for q in questions if q["id"] == "q06")
    top = variants[0][1].search(bad["question"], 3)
    print(f"\n{bad['id']} {bad['question']!r} - top 3 for 'sections + headers':")
    for score, c in top:
        print(f"   {score:5.2f}  {c.doc_id} / {c.section}")
    print("   gold: kc1_controller_manual / 6. Diagnostic log - its text says 'stores the last 32 faults' and "
          "'Modbus registers', never 'history' or 'remotely'.")


if __name__ == "__main__":
    main()
