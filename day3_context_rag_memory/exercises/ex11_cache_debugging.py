"""Exercise 11 starter - this prompt builder never gets a cache hit. Find out why and fix it.

Run it, look at cache_read_input_tokens, then write build_fixed() so that the second and third requests read the
manual library from the cache. Nothing errors when caching is broken - the bill is just higher.
Run: python day3_context_rag_memory/exercises/ex11_cache_debugging.py
"""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from labkit import MODEL, get_client, header  # noqa: E402

import _day3 as d3  # noqa: E402
import _tools as tools  # noqa: E402

HEAD = ("<day3_manual_library>\nYou are Kestrel's field-service assistant. Answer only from the manuals below and cite "
        "the manual code and section, e.g. [IOM-KP250 §6].\n</day3_manual_library>\n")
LIBRARY = d3.library_text(d3.load_docs(("manual",)))
SITE = {"site": "Harbor Foods Plant 2", "customer_id": "C-1005", "units": "metric", "language": "en", "shift": "day"}


def build_request(question: str) -> dict:
    settings = dict(random.sample(sorted(SITE.items()), len(SITE)))    # from a config service
    tool_list = random.sample([tools.SEARCH_MANUALS, tools.READ_SECTION, tools.QUERY_TELEMETRY], 3)  # from plugins
    system = f"{HEAD}Generated at {dt.datetime.now().isoformat()}\nSite settings: {json.dumps(settings)}\n\n{LIBRARY}"
    return dict(model=MODEL, max_tokens=2000, tools=tool_list,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": question}])


def main() -> None:
    client = get_client()
    header("Exercise 11 - a cache that never hits (starter)")
    rows = []
    for i in range(3):
        r = client.messages.create(**build_request("How often do I regrease the KP-250 bearings?"))
        rows.append([f"request #{i + 1}", r.usage.cache_creation_input_tokens or 0, r.usage.cache_read_input_tokens or 0])
    d3.table(rows, ["request", "cache write", "cache read"])
    print("\nTODO: find every silent invalidator in build_request (diff two rendered requests: tools -> system -> "
          "messages) and write build_fixed(). Solution: day3_context_rag_memory/solutions/ex11_cache_debugging.py")


if __name__ == "__main__":
    main()
