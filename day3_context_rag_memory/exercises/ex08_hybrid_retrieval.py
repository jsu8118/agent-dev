"""Exercise 8 starter - hybrid retrieval (BM25 + a subword retriever, fused with reciprocal rank fusion).

Fill in the three TODOs, then compare your hybrid against BM25 on the lab 05 question set.
Run: python day3_context_rag_memory/exercises/ex08_hybrid_retrieval.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import header  # noqa: E402

import _day3 as d3  # noqa: E402
from _retrieval import BM25, Chunk, evaluate, load_docs, load_questions, section_chunks  # noqa: E402

Search = Callable[[str, int], list[tuple[float, Chunk]]]


def char_ngrams(text: str, n: int = 4) -> list[str]:
    """TODO 1: character n-grams inside each lower-cased word, with a space marking word boundaries."""
    return []


class NgramTfIdf:
    """TODO 2: TF-IDF vectors over char_ngrams for each chunk; search() ranks chunks by cosine similarity."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks

    def search(self, query: str, k: int = 5) -> list[tuple[float, Chunk]]:
        return []


def rrf(*searches: Search, k: int = 60) -> Search:
    """TODO 3: reciprocal rank fusion - score(chunk) = sum over retrievers of 1 / (k + rank)."""
    return searches[0]


def main() -> None:
    header("Exercise 8 - hybrid retrieval (starter)")
    sections = section_chunks(load_docs())
    questions = load_questions()
    bm25 = BM25(sections, Chunk.with_header)
    ngram = NgramTfIdf(sections)
    rows = []
    for name, search in [("BM25", bm25.search), ("char n-gram TF-IDF", ngram.search),
                         ("RRF(BM25, n-gram)", rrf(bm25.search, ngram.search))]:
        r = evaluate(name, bm25, questions, sections, search=search)
        rows.append([name, f"{r.hit[1]:.2f}", f"{r.hit[3]:.2f}", f"{r.hit[5]:.2f}", f"{r.mrr:.3f}"])
    d3.table(rows, ["retriever", "hit@1", "hit@3", "hit@5", "MRR@10"])
    print("\nTODO: implement char_ngrams, NgramTfIdf and rrf; then list the questions whose rank changed and explain "
          "why. Solution: day3_context_rag_memory/solutions/ex08_hybrid_retrieval.py")


if __name__ == "__main__":
    main()
