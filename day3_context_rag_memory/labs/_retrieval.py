"""Retrieval building blocks for Day 3 (helper module, not a lab).

* Two chunkers that keep character spans, so any chunking can be scored against the same gold labels:
    - `section_chunks`  : one chunk per H2/H3 section (same boundaries as kestrel.kb)
    - `window_chunks`   : fixed-size windows of whole lines with overlap
* An optional *contextual header* ("<document title> > <section path>") prepended to the indexed text only -
  a deterministic, zero-cost cousin of Anthropic's "contextual retrieval" (where an LLM writes the context).
* `BM25`: the classic lexical ranker, pure Python (tokenizer shared with kestrel.kb).
* `evaluate`: hit@k and MRR against labelled questions.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kestrel.kb import tokenize

from _day3 import DAY_DIR, Doc, load_docs

QUESTIONS_FILE = DAY_DIR / "data" / "retrieval_questions.jsonl"


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str        # heading path, e.g. "7. Troubleshooting > 7.2 Cavitation"
    start: int          # character span in the source document
    end: int
    text: str           # the chunk body (what the model would read)

    @property
    def header(self) -> str:
        return f"{self.title} > {self.section}"

    def with_header(self) -> str:
        return f"{self.header}\n{self.text}"


def _lines_with_offsets(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, line))
        pos += len(line) + 1
    return out


def section_chunks(docs: list[Doc] | None = None) -> list[Chunk]:
    """One chunk per H2/H3 section; text before the first H2 becomes 'Introduction' (as in kestrel.kb)."""
    chunks: list[Chunk] = []
    for doc in docs or load_docs():
        h2 = h3 = ""
        buf: list[tuple[int, str]] = []
        n = 0

        def flush() -> None:
            nonlocal n
            body = [(p, l) for p, l in buf]
            while body and not body[0][1].strip():
                body.pop(0)
            while body and not body[-1][1].strip():
                body.pop()
            if body:
                start, end = body[0][0], body[-1][0] + len(body[-1][1])
                n += 1
                section = " > ".join(s for s in (h2, h3) if s) or "Introduction"
                chunks.append(Chunk(f"{doc.doc_id}#{n}", doc.doc_id, doc.title, section, start, end,
                                    doc.text[start:end]))
            buf.clear()

        for pos, line in _lines_with_offsets(doc.text):
            if line.startswith("## "):
                flush()
                h2, h3 = line[3:].strip(), ""
            elif line.startswith("### "):
                flush()
                h3 = line[4:].strip()
            elif line.startswith("# "):
                continue
            else:
                buf.append((pos, line))
        flush()
    return chunks


def window_chunks(docs: list[Doc] | None = None, *, size: int = 500, overlap: int = 100) -> list[Chunk]:
    """Fixed-size windows of whole lines (~`size` chars) that overlap by ~`overlap` chars.

    Headings are kept inside the body (they are part of the text); the `section` field records the heading
    path in effect where the window starts, which is what a contextual header would say.
    """
    chunks: list[Chunk] = []
    for doc in docs or load_docs():
        lines = [(p, l) for p, l in _lines_with_offsets(doc.text) if l.strip() and not l.startswith("# ")]
        # heading path in effect at each line
        paths, h2, h3 = [], "", ""
        for _, line in lines:
            if line.startswith("## "):
                h2, h3 = line[3:].strip(), ""
            elif line.startswith("### "):
                h3 = line[4:].strip()
            paths.append(" > ".join(s for s in (h2, h3) if s) or "Introduction")
        i, n = 0, 0
        while i < len(lines):
            j, length = i, 0
            while j < len(lines) and (length < size or j == i):
                length += len(lines[j][1]) + 1
                j += 1
            start, end = lines[i][0], lines[j - 1][0] + len(lines[j - 1][1])
            n += 1
            chunks.append(Chunk(f"{doc.doc_id}~w{n}", doc.doc_id, doc.title, paths[i], start, end,
                                doc.text[start:end]))
            if j >= len(lines):
                break
            # step back so consecutive windows share ~overlap characters
            back, k = 0, j
            while k - 1 > i and back < overlap:
                k -= 1
                back += len(lines[k][1]) + 1
            i = max(k, i + 1)
    return chunks


class BM25:
    """Okapi BM25 over arbitrary chunks. `index_text` decides what gets indexed (body only, or header + body)."""

    def __init__(self, chunks: list[Chunk], index_text: Callable[[Chunk], str] = Chunk.with_header, *,
                 k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(index_text(c))) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = sum(self._len) / max(len(self._len), 1)
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out = []
        for i, tf in enumerate(self._tf):
            s = 0.0
            for t in terms:
                f = tf.get(t)
                if f:
                    s += self._idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self._len[i] / self._avg))
            out.append(s)
        return out

    def search(self, query: str, k: int = 5) -> list[tuple[float, Chunk]]:
        ranked = sorted(zip(self.scores(query), self.chunks), key=lambda x: -x[0])
        return [(s, c) for s, c in ranked[:k] if s > 0]


# --------------------------------------------------------------------------------------------- evaluation
def load_questions(path: Path = QUESTIONS_FILE) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def gold_spans(question: dict, sections: list[Chunk]) -> list[tuple[str, int, int]]:
    """Resolve {"doc", "section"} gold labels to character spans (section prefix match, so a label may be an H2)."""
    spans = []
    for g in question["gold"]:
        found = [c for c in sections if c.doc_id == g["doc"] and
                 (c.section == g["section"] or c.section.startswith(g["section"] + " > "))]
        if not found:
            raise ValueError(f"{question['id']}: gold section not found: {g}")
        spans.append((g["doc"], min(c.start for c in found), max(c.end for c in found)))
    return spans


def is_relevant(chunk: Chunk, spans: list[tuple[str, int, int]]) -> bool:
    """A chunk counts as relevant if at least half of it (or half of the gold section, if that is shorter)
    lies inside a gold section. The same rule scores section chunks and windows, so variants are comparable."""
    for doc, s, e in spans:
        if chunk.doc_id != doc:
            continue
        overlap = max(0, min(e, chunk.end) - max(s, chunk.start))
        if overlap >= 0.5 * min(chunk.end - chunk.start, e - s):
            return True
    return False


@dataclass
class EvalResult:
    name: str
    hit: dict[int, float]
    mrr: float
    avg_chars_at_3: float
    ranks: dict[str, int | None]      # question id -> rank of first relevant chunk (1-based) or None


def evaluate(name: str, index: BM25, questions: list[dict], sections: list[Chunk], *, ks=(1, 3, 5),
             depth: int = 10, search: Callable[[str, int], list[tuple[float, Chunk]]] | None = None) -> EvalResult:
    search = search or index.search
    ranks: dict[str, int | None] = {}
    chars3 = []
    for q in questions:
        spans = gold_spans(q, sections)
        hits = search(q["question"], depth)
        rank = next((i + 1 for i, (_, c) in enumerate(hits) if is_relevant(c, spans)), None)
        ranks[q["id"]] = rank
        chars3.append(sum(len(c.text) for _, c in hits[:3]))
    n = len(questions)
    hit = {k: sum(1 for r in ranks.values() if r is not None and r <= k) / n for k in ks}
    mrr = sum(1 / r for r in ranks.values() if r) / n
    return EvalResult(name, hit, mrr, sum(chars3) / n, ranks)
