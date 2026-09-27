"""Solution to exercise 8 - hybrid retrieval: BM25 + a subword retriever, fused with reciprocal rank fusion.

Why these pieces:
* BM25 is exact-term matching on whole words: it misses "continuously" vs "continuous", "faults" vs "fault".
* A character n-gram TF-IDF retriever matches word *parts*, so morphology and part numbers split by the
  tokenizer ("KP-250" -> "kp", "250") still overlap. It is a pure-Python stand-in for the semantic side of a
  hybrid system (an embedding model would also bridge synonyms like "history" ~ "log", which n-grams cannot).
* Reciprocal rank fusion (RRF) combines rankings without calibrating scores: score(d) = sum_i w_i / (k + rank_i(d)).
* A light stemmer on the BM25 side is the cheapest fix for the morphology misses, shown for comparison.

Run: python day3_context_rag_memory/solutions/ex08_hybrid_retrieval.py
"""
# test: expect=RRF

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from kestrel.kb import tokenize  # noqa: E402
from labkit import header, step  # noqa: E402

import _day3 as d3  # noqa: E402
from _retrieval import BM25, Chunk, evaluate, load_docs, load_questions, section_chunks  # noqa: E402

Search = Callable[[str, int], list[tuple[float, Chunk]]]


def char_ngrams(text: str, n: int = 4) -> list[str]:
    """Character n-grams inside each word, with boundary markers (' pump ' -> ' pum', 'pump', 'ump ')."""
    grams: list[str] = []
    for word in re.findall(r"[a-z0-9][a-z0-9\-.]*", text.lower()):
        w = f" {word} "
        grams += [w[i:i + n] for i in range(max(1, len(w) - n + 1))]
    return grams


class NgramTfIdf:
    """Cosine similarity over sublinear-TF x IDF weighted character n-grams."""

    def __init__(self, chunks: list[Chunk], text: Callable[[Chunk], str] = Chunk.with_header, n: int = 4) -> None:
        self.chunks, self.n = chunks, n
        tfs = [Counter(char_ngrams(text(c), n)) for c in chunks]
        df: Counter = Counter()
        for tf in tfs:
            df.update(tf.keys())
        self.idf = {g: math.log((len(chunks) + 1) / (f + 1)) + 1 for g, f in df.items()}
        self.vectors = [self._unit({g: (1 + math.log(c)) * self.idf[g] for g, c in tf.items()}) for tf in tfs]

    @staticmethod
    def _unit(v: dict[str, float]) -> dict[str, float]:
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {g: x / norm for g, x in v.items()}

    def search(self, query: str, k: int = 5) -> list[tuple[float, Chunk]]:
        tf = Counter(char_ngrams(query, self.n))
        q = self._unit({g: (1 + math.log(c)) * self.idf.get(g, 0.0) for g, c in tf.items()})
        scored = [(sum(w * vec.get(g, 0.0) for g, w in q.items()), c) for vec, c in zip(self.vectors, self.chunks)]
        scored.sort(key=lambda x: -x[0])
        return scored[:k]


def rrf(*searches: Search, weights: tuple[float, ...] | None = None, k: int = 60, depth: int = 50) -> Search:
    """Reciprocal rank fusion. k=60 is the value from the original RRF paper; it damps the head of each list."""
    weights = weights or tuple(1.0 for _ in searches)

    def search(query: str, top: int) -> list[tuple[float, Chunk]]:
        scores: dict[str, float] = {}
        by_id: dict[str, Chunk] = {}
        for w, s in zip(weights, searches):
            for rank, (_, chunk) in enumerate(s(query, depth), 1):
                scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + w / (k + rank)
                by_id[chunk.chunk_id] = chunk
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top]
        return [(score, by_id[cid]) for cid, score in ranked]

    return search


def stem(token: str) -> str:
    """A deliberately tiny suffix stripper (strip -ly, then -ing/-ed/-es/-s, then a final -e)."""
    t = token
    if not t.isalpha():
        return t
    for suffixes in (("ly",), ("ing", "ed", "es", "s"), ("e",)):
        for suf in suffixes:
            if t.endswith(suf) and len(t) - len(suf) >= 3:
                t = t[:-len(suf)]
                break
    return t


class StemmedBM25(BM25):
    """BM25 whose query and documents go through the same stemmer."""

    def __init__(self, chunks: list[Chunk]) -> None:
        super().__init__(chunks, lambda c: " ".join(stem(t) for t in tokenize(c.with_header())))

    def scores(self, query: str) -> list[float]:
        return super().scores(" ".join(stem(t) for t in tokenize(query)))


def main() -> None:
    header("Solution 8 - hybrid retrieval with reciprocal rank fusion")
    docs = load_docs()
    sections = section_chunks(docs)
    questions = load_questions()
    bm25 = BM25(sections, Chunk.with_header)
    ngram = NgramTfIdf(sections)
    stemmed = StemmedBM25(sections)

    step(1, "Score the retrievers and their fusions on the lab 05 question set")
    variants: list[tuple[str, Search]] = [
        ("BM25 (lab 05 baseline)", bm25.search),
        ("char 4-gram TF-IDF", ngram.search),
        ("RRF(BM25, 4-gram)", rrf(bm25.search, ngram.search)),
        ("RRF(BM25, 4-gram) weights 1:2", rrf(bm25.search, ngram.search, weights=(1.0, 2.0))),
        ("BM25 + stemming", stemmed.search),
        ("RRF(BM25+stemming, 4-gram)", rrf(stemmed.search, ngram.search)),
    ]
    results = []
    rows = []
    for name, search in variants:
        r = evaluate(name, bm25, questions, sections, search=search)
        results.append(r)
        rows.append([name, f"{r.hit[1]:.2f}", f"{r.hit[3]:.2f}", f"{r.hit[5]:.2f}", f"{r.mrr:.3f}"])
    d3.table(rows, ["retriever", "hit@1", "hit@3", "hit@5", "MRR@10"])

    step(2, "Which questions moved? (rank of the gold section; '-' = not in top 10)")
    base = results[0]
    rows = []
    for q in questions:
        ranks = [r.ranks[q["id"]] for r in results]
        if len(set(ranks)) > 1:
            rows.append([q["id"], *(x or "-" for x in ranks), q["question"][:52]])
    d3.table(rows, ["id", "BM25", "4-gram", "RRF", "RRF 1:2", "stem", "RRF stem", "question"])
    print("\nReading the numbers:")
    print("* The n-gram retriever alone is strong here because this corpus is small and full of word variants; on a "
          "large corpus it is noisier (many chunks share common n-grams) and BM25's precision matters more.")
    print("* Fusion is not automatically better than its best component: RRF trades a little rank-1 precision for "
          "recall (hit@5). Tune weights on a held-out set, not on the questions you report.")
    print("* Every change has losers: stemming pushed q05 (F10) from rank 4 to 7. Read the per-question table, not "
          "only the averages - 20 questions is enough to find failure modes, not to prove small differences.")
    print("* Stemming fixes the morphology misses cheaply ('continuously' -> 'continuous', 'leaks' -> 'leak'). q06 "
          "improves only because 'fault' now also matches 'faults'; 'history' still matches nothing. Bridging a true "
          "synonym ('history' ~ 'log') needs an embedding model, query rewriting, or an agent that searches again "
          "with the manual's vocabulary.")


if __name__ == "__main__":
    main()
