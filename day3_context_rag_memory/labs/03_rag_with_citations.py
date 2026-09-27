"""Lab 03 - Retrieval-augmented answers with verifiable citations (and the long-context alternative).

Objective
    Retrieve the best manual sections with BM25 (kestrel.kb), pass them to Claude as `document` blocks with
    citations enabled, and print an answer whose every claim points at the exact source text - then check those
    pointers programmatically. Repeat with `search_result` blocks, and compare the same questions answered from the
    whole corpus in a cached long-context prompt (tokens, cost, citations).

Concepts
    lexical retrieval (BM25) over section chunks; top-k; document blocks (source type "text") with
    citations {"enabled": true} -> char_location citations; search_result blocks -> search_result_location;
    title/context fields (not citable); verifying cited_text against the source; citations are incompatible with
    structured outputs; long-context stuffing with a cached document prefix.

Run
    python day3_context_rag_memory/labs/03_rag_with_citations.py [--k 4]

What to observe
    * Retrieval puts the right section in the top 4 for all three questions, but not always at rank 1
      (the KP-400 question ranks "Installation notes" above "Operating limits").
    * Each cited span is an exact substring of a retrieved section - the check prints "verified".
    * search_result citations carry your own `source` identifier (kb://...), handy for linking back to the KB;
      a content block is the smallest citable unit, so a table sent as one block comes back whole.
    * Stuffing all 11 documents costs ~7x more than RAG on the first question (the cache write); later questions
      read the cached prefix and cost about the same as RAG - at this corpus size. Long context can also cite
      passages retrieval missed (recall), at the price of the model reading 11 documents to use 1-2.
"""
# test: expect=verified

from __future__ import annotations

import argparse

from kestrel.kb import Chunk

from labkit import MODEL, get_client, header, is_mock, step, wrap

import _day3 as d3
import _tools as tools

SYSTEM = """\
<day3_rag>
You are Kestrel's field-service assistant. Answer the technician's question using ONLY the documents in the user \
turn, and cite them. If the documents do not contain the answer, say so plainly. Be brief: 2-4 sentences.
</day3_rag>"""

QUESTIONS = [
    "How often should I regrease the KP-250 bearings, and how much grease goes in?",
    "Our KC-1 trips on F05 most hot afternoons. What should I check first?",
    "A KP-400 on a steel-frame skid reads 4.1 mm/s. Do we need to shut it down?",
]
CODES = {d.doc_id: d.code for d in d3.load_docs()}


def title_of(chunk: Chunk) -> str:
    # titles are length-limited by the API; keep them short and put longer metadata in `context`
    return f"{CODES.get(chunk.doc, chunk.doc)} §{chunk.section}"[:80]


def as_documents(chunks: list[Chunk]) -> list[dict]:
    """One plain-text document per retrieved section: Claude can cite individual sentences inside it."""
    return [{"type": "document",
             "source": {"type": "text", "media_type": "text/plain", "data": c.text},
             "title": title_of(c),
             "context": f"{c.title}; section '{c.section}'; retrieved by BM25 (chunk {c.chunk_id})",
             "citations": {"enabled": True}} for c in chunks]


def as_search_results(chunks: list[Chunk]) -> list[dict]:
    """Search results carry your own source id; each content block is the smallest citable unit."""
    out = []
    for c in chunks:
        blocks = [p.strip() for p in c.text.split("\n\n") if p.strip()]
        out.append({"type": "search_result", "source": f"kb://{c.chunk_id}", "title": title_of(c),
                    "content": [{"type": "text", "text": b} for b in blocks], "citations": {"enabled": True}})
    return out


def render(response, blocks: list[dict]) -> tuple[int, int]:
    """Print the answer with [n] markers and a source list; return (citations, verified)."""
    sources: dict[tuple, int] = {}
    text, notes = "", []
    n_cit = n_ok = 0
    for block in response.content:
        if block.type != "text":
            continue
        text += block.text
        for c in block.citations or []:
            key = (c.type, getattr(c, "document_index", getattr(c, "search_result_index", None)), c.cited_text)
            if key not in sources:
                sources[key] = len(sources) + 1
                ok = verify(c, blocks)
                n_cit += 1
                n_ok += ok
                if c.type == "char_location":
                    where = f"chars {c.start_char_index}-{c.end_char_index}"
                elif c.type == "search_result_location":
                    where = f"blocks {c.start_block_index}-{c.end_block_index} of {c.source}"
                else:                                       # page_location / content_block_location
                    where = c.type
                title = getattr(c, "document_title", None) or getattr(c, "title", None) or "?"
                notes.append(f"  [{sources[key]}] {title} ({c.type}, {where}) "
                             f"{'verified' if ok else 'NOT FOUND IN SOURCE'}\n"
                             f"      \"{' '.join(c.cited_text.split())[:150]}\"")
            text += f"[{sources[key]}]"
    print(wrap(text))
    print("\n".join(notes) if notes else "  (no citations returned)")
    return n_cit, n_ok


def verify(citation, blocks: list[dict]) -> bool:
    """A citation is a pointer; check that it points at text that really is in the source we sent."""
    norm = lambda s: " ".join(s.split())
    if citation.type == "char_location":
        docs = [b for b in blocks if b["type"] == "document"]
        source = docs[citation.document_index]["source"]["data"]
        return (source[citation.start_char_index:citation.end_char_index] == citation.cited_text
                or norm(citation.cited_text) in norm(source))
    if citation.type == "search_result_location":
        results = [b for b in blocks if b["type"] == "search_result"]
        chunk = results[citation.search_result_index]["content"][citation.start_block_index:citation.end_block_index]
        return norm(citation.cited_text) in norm(" ".join(b["text"] for b in chunk))
    return False


def ask(client, blocks: list[dict], question: str):
    return client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM,
                                  messages=[{"role": "user", "content": [*blocks, {"type": "text", "text": question}]}])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--k", type=int, default=4, help="sections retrieved per question")
    args = parser.parse_args()
    client = get_client()
    header("Lab 03 - RAG with citations vs long-context stuffing")
    if is_mock():
        print("(mock mode: the stand-in model quotes sentences it finds in the documents; citations are real "
              "API-shaped objects, so the verification code is the same you would run live)")

    step(1, "Index: BM25 over section chunks (kestrel.kb)")
    kb = tools.KB
    sizes = [len(c.text) for c in kb.chunks]
    print(f"{len(kb.chunks)} sections from {len({c.doc for c in kb.chunks})} documents; median section "
          f"{sorted(sizes)[len(sizes) // 2]} chars, largest {max(sizes)} chars ({max(kb.chunks, key=lambda c: len(c.text)).cite()}).")

    step(2, f"Retrieve top-{args.k} sections, answer with document blocks + citations")
    rag_costs, totals = [], [0, 0]
    for q in QUESTIONS:
        hits = kb.search(q, args.k)
        print(f"\nQ: {q}")
        for rank, (score, c) in enumerate(hits, 1):
            print(f"   {rank}. {score:5.2f}  {c.chunk_id:<28} {c.section}")
        blocks = as_documents([c for _, c in hits])
        r = ask(client, blocks, q)
        n, ok = render(r, blocks)
        totals[0] += n
        totals[1] += ok
        rag_costs.append((d3.prompt_size(r.usage), d3.response_cost(r), n))
    print(f"\nCitations verified against the retrieved text: {totals[1]}/{totals[0]}")

    step(3, "The same retrieval as search_result blocks (search_result_location citations)")
    q = QUESTIONS[1]
    hits = kb.search(q, args.k)
    blocks = as_search_results([c for _, c in hits])
    r = ask(client, blocks, q)
    print(f"Q: {q}")
    n, ok = render(r, blocks)
    print(f"search_result citations verified: {ok}/{n}")
    print("Note: cited_text is the whole content block - the fault table was sent as one block, so it comes back "
          "whole. Split tables into one block per row when you need row-level citations.")

    step(4, "Long-context alternative: all 11 documents, cached, citations still on")
    docs = d3.load_docs()
    full = [{"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": d.text},
             "title": f"{d.code} - {d.title}"[:80], "citations": {"enabled": True}} for d in docs]
    full[-1]["cache_control"] = {"type": "ephemeral"}      # breakpoint at the end of the shared documents
    rows = []
    for i, q in enumerate(QUESTIONS):
        r = ask(client, full, q)
        u = r.usage
        n_cit = sum(len(b.citations or []) for b in r.content if b.type == "text")
        rows.append([f"long context Q{i + 1}", d3.prompt_size(u), u.cache_read_input_tokens or 0,
                     u.cache_creation_input_tokens or 0, n_cit, d3.money(d3.response_cost(r))])
    for i, (prompt, cost, n_cit) in enumerate(rag_costs):
        rows.append([f"RAG top-{args.k} Q{i + 1}", prompt, 0, 0, n_cit, d3.money(cost)])
    d3.table(rows, ["strategy", "prompt tokens", "cache read", "cache write", "citations", "cost"])
    print("At ~10K tokens of corpus, cached long context is a legitimate choice; at 25M tokens (50,000 pages) it is "
          "impossible and retrieval is mandatory. Precision matters too: the model reads 11 documents to use 1-2.")


if __name__ == "__main__":
    main()
