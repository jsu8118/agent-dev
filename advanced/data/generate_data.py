"""Regenerate the advanced course's datasets (deterministic, seeded).

    python advanced/data/generate_data.py

Everything is derived from the base course's world (data/kestrel_ops.db, data/support/*.jsonl), so run
`python data/generate_data.py` first if you have changed that.  Hand-written documents under advanced/data
(none of the generated files) are committed as-is.  advanced/data/manifest.json records what was generated
so CI can detect drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))          # repo root: advanced.* is importable

from advanced.data.generators import recall, security, tools_catalog, traces  # noqa: E402
from advanced.data.generators.common import ADV_DATA_DIR, AS_OF, SEED  # noqa: E402


def main() -> None:
    summary = {
        "tools": {"count": tools_catalog.build(ADV_DATA_DIR / "tools")["count"]},
        "security": security.build(ADV_DATA_DIR / "security"),
        "traces": traces.build(ADV_DATA_DIR / "traces"),
        "recall": recall.build(ADV_DATA_DIR / "recall"),
    }
    manifest = {"as_of": AS_OF.isoformat(), "seed": SEED, "summary": summary,
                "files": sorted(str(p.relative_to(ADV_DATA_DIR)) for p in ADV_DATA_DIR.rglob("*")
                                if p.is_file() and p.suffix in (".json", ".jsonl") and p.name != "manifest.json")}
    (ADV_DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Advanced dataset regenerated (as-of {AS_OF}):")
    for section, counts in summary.items():
        print(f"  {section:<10} " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  files      {len(manifest['files'])} (see advanced/data/manifest.json)")


if __name__ == "__main__":
    main()
