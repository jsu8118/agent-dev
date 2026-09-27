"""Solution to exercise 10 - add a labelled question set for the policies and measure it.

The lab 05 set is mostly about manuals. Here are 10 policy questions (solutions/policy_questions.jsonl), written
the way support staff and technicians actually ask - paraphrases, dollar amounts, symptoms - with gold
(document, section) labels. We score the same four variants plus the stemmed BM25 and the fusion from solution 8,
and compare with the manual questions: a single averaged number would hide that the best chunking differs.

Run: python day3_context_rag_memory/solutions/ex10_policy_questions.py
"""
# test: expect=policy questions

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "labs"))

from labkit import header, step  # noqa: E402

import _day3 as d3  # noqa: E402
from _retrieval import BM25, Chunk, evaluate, load_docs, load_questions, section_chunks, window_chunks  # noqa: E402
from ex08_hybrid_retrieval import NgramTfIdf, StemmedBM25, rrf  # noqa: E402


def main() -> None:
    header("Solution 10 - a new question set: company policies")
    docs = load_docs()
    sections, windows = section_chunks(docs), window_chunks(docs)
    policy_qs = load_questions(HERE / "policy_questions.jsonl")
    manual_qs = load_questions()
    stemmed, ngram = StemmedBM25(sections), NgramTfIdf(sections)
    variants = [
        ("sections + headers", BM25(sections, Chunk.with_header).search),
        ("sections, body only", BM25(sections, lambda c: c.text).search),
        ("windows, body only", BM25(windows, lambda c: c.text).search),
        ("windows + headers", BM25(windows, Chunk.with_header).search),
        ("sections + stemming", stemmed.search),
        ("RRF(stemmed, 4-gram)", rrf(stemmed.search, ngram.search)),
    ]

    step(1, f"{len(policy_qs)} policy questions vs the {len(manual_qs)} lab 05 questions")
    anchor = BM25(sections, Chunk.with_header)
    rows, policy_results = [], []
    for name, search in variants:
        p = evaluate(name, anchor, policy_qs, sections, search=search)
        m = evaluate(name, anchor, manual_qs, sections, search=search)
        policy_results.append(p)
        rows.append([name, f"{p.hit[1]:.2f}", f"{p.hit[3]:.2f}", f"{p.mrr:.3f}", f"{m.hit[1]:.2f}", f"{m.mrr:.3f}"])
    d3.table(rows, ["variant", "policy hit@1", "policy hit@3", "policy MRR", "lab05 hit@1", "lab05 MRR"])

    step(2, "Failures on the policy questions ('-' = not in top 10)")
    rows = []
    for q in policy_qs:
        ranks = [r.ranks[q["id"]] for r in policy_results]
        if any(x != 1 for x in ranks):
            rows.append([q["id"], *(x or "-" for x in ranks), q["question"][:50]])
    d3.table(rows, ["id", "sec+hdr", "sec", "win", "win+hdr", "stem", "RRF", "question"])
    print("\nWhat the numbers say:")
    print("* Policies are short, list-shaped sections whose headings add few new words, so headers barely matter here;"
          " windows win at rank 1 because they isolate the one list item that answers the question.")
    print("* p05 ('How long is a pump covered after it ships?') fails for plain BM25: the table says 'Pumps', "
          "'Warranty period' and 'ship date' - no stemming, no match. Stemming recovers it.")
    print("* Keep separate question sets per content type (manuals, policies, and later tickets) and report them "
          "separately - an average over a set that is 80% manuals would have hidden all of this.")


if __name__ == "__main__":
    main()
