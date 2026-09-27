"""A small, dependency-free knowledge base: markdown documents chunked by section, ranked with BM25.

BM25 is the classic lexical ranking function behind most search engines.  It needs no
embedding model or vector database, is fast, explainable, and strong on exact terms such
as part numbers ("MS-250"), fault codes ("F05"), and numbers - exactly what technical
manuals are full of.  Day 3 compares it with other retrieval strategies.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from labkit.config import DATA_DIR

DEFAULT_SOURCES = ("company/policies", "manuals")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?")
_STOP = set("a an the of to in on for and or is are be by with at as it this that from not no do does "
            "what which when how can should i we you our your my".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass
class Chunk:
    chunk_id: str
    doc: str            # file stem, e.g. "kp250_pump_iom"
    title: str          # document title (first H1)
    section: str        # heading path, e.g. "7. Troubleshooting > 7.2 Cavitation"
    text: str

    def cite(self) -> str:
        return f"{self.title} — {self.section}"


def chunk_markdown(path: Path) -> list[Chunk]:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), path.stem)
    chunks: list[Chunk] = []
    h2 = h3 = ""
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            section = " > ".join(s for s in (h2, h3) if s) or "Introduction"
            chunks.append(Chunk(f"{path.stem}#{len(chunks) + 1}", path.stem, title, section, body))
        buf.clear()

    for line in lines:
        if line.startswith("## "):
            flush()
            h2, h3 = line[3:].strip(), ""
        elif line.startswith("### "):
            flush()
            h3 = line[4:].strip()
        elif line.startswith("# "):
            continue
        else:
            buf.append(line)
    flush()
    return chunks


class KnowledgeBase:
    def __init__(self, sources: tuple[str, ...] = DEFAULT_SOURCES, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks: list[Chunk] = []
        for source in sources:
            for path in sorted((DATA_DIR / source).glob("*.md")):
                self.chunks.extend(chunk_markdown(path))
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(f"{c.title} {c.section} {c.text}")) for c in self.chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = sum(self._len) / max(len(self._len), 1)
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def search(self, query: str, top_k: int = 4) -> list[tuple[float, Chunk]]:
        terms = tokenize(query)
        scores = []
        for i, tf in enumerate(self._tf):
            score = 0.0
            for term in terms:
                if term not in tf:
                    continue
                freq = tf[term]
                score += self._idf[term] * freq * (self.k1 + 1) / (
                    freq + self.k1 * (1 - self.b + self.b * self._len[i] / self._avg))
            if score > 0:
                scores.append((score, self.chunks[i]))
        scores.sort(key=lambda x: -x[0])
        return scores[:top_k]


_DEFAULT: KnowledgeBase | None = None


def default_kb() -> KnowledgeBase:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = KnowledgeBase()
    return _DEFAULT
