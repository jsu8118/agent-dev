"""Exercise 10 starter - write a labelled question set for the company policies and measure retrieval on it.

1. Create day3_context_rag_memory/exercises/my_policy_questions.jsonl with 8-10 lines like the examples below
   (gold "section" must match a heading path printed by --list-sections).
2. Run this script: it scores the four lab 05 variants on your set.
Run: python day3_context_rag_memory/exercises/ex10_policy_questions.py [--list-sections]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "labs"))

from labkit import header  # noqa: E402

import _day3 as d3  # noqa: E402
from _retrieval import BM25, Chunk, evaluate, load_docs, load_questions, section_chunks, window_chunks  # noqa: E402

EXAMPLES = [
    {"id": "x01", "question": "Within how many days of delivery can a standard item be returned?",
     "gold": [{"doc": "returns_rma_policy", "section": "1. Returns of non-defective items"}], "type": "exact_terms"},
    {"id": "x02", "question": "Who has to sign off a $7,500 refund?",
     "gold": [{"doc": "returns_rma_policy", "section": "6. Refunds"}], "type": "paraphrase"},
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list-sections", action="store_true")
    args = parser.parse_args()
    header("Exercise 10 - a new question set (starter)")
    docs = load_docs(("policy",)) if args.list_sections else load_docs()
    sections = section_chunks(docs)
    if args.list_sections:
        for c in sections:
            print(f"{c.doc_id:28s} {c.section}")
        return
    path = HERE / "my_policy_questions.jsonl"
    if path.exists():
        questions = load_questions(path)
    else:
        questions = EXAMPLES
        print(f"TODO: create {path.name} (one JSON object per line). Using the 2 built-in examples for now:")
        for q in EXAMPLES:
            print("  " + json.dumps(q))
    windows = window_chunks(docs)
    rows = []
    for name, index in [("sections + headers", BM25(sections, Chunk.with_header)),
                        ("sections, body only", BM25(sections, lambda c: c.text)),
                        ("windows, body only", BM25(windows, lambda c: c.text)),
                        ("windows + headers", BM25(windows, Chunk.with_header))]:
        r = evaluate(name, index, questions, sections)
        rows.append([name, f"{r.hit[1]:.2f}", f"{r.hit[3]:.2f}", f"{r.mrr:.3f}"])
    d3.table(rows, ["variant", "hit@1", "hit@3", "MRR@10"])
    print("\nTODO: which variant wins on policies, and is it the same one that wins on manuals (lab 05)? Why? "
          "Solution: day3_context_rag_memory/solutions/ex10_policy_questions.py")


if __name__ == "__main__":
    main()
