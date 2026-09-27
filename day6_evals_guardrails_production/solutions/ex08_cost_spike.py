"""Solution to exercise 8 - find the root cause of a cost spike from two traces.

exercises/data/cost_spike_traces.jsonl holds two traces exported by the support agent's tracer: T-2177 from
the day before the spike, T-2203 from the day after deploy 2026.09.16-2.  The script does what an on-call
engineer should do: price every token by type, compare the two traces, test hypotheses, and quantify each
cause with a counterfactual.

Run
    python day6_evals_guardrails_production/solutions/ex08_cost_spike.py
"""

# test: expect=Root cause

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from _evalkit import load_jsonl  # noqa: E402
from labkit import header, step  # noqa: E402
from labkit.pricing import cost_breakdown  # noqa: E402

TRACES = Path(__file__).resolve().parents[1] / "exercises" / "data" / "cost_spike_traces.jsonl"
MODEL = "claude-opus-5"


def usage(span: dict) -> dict:
    a = span["attributes"]
    return {"input_tokens": a["gen_ai.usage.input_tokens"], "output_tokens": a["gen_ai.usage.output_tokens"],
            "cache_read_input_tokens": a["gen_ai.usage.cache_read_input_tokens"],
            "cache_creation_input_tokens": a["gen_ai.usage.cache_creation_input_tokens"]}


def profile(spans: list[dict]) -> dict:
    root = next(s for s in spans if s["name"] == "agent.run")
    llm = sorted((s for s in spans if s["name"] == "llm.call"), key=lambda s: s["start"])
    parts = Counter()
    for s in llm:
        parts.update(cost_breakdown(usage(s), MODEL))
    prompt = [sum(usage(s)[k] for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
              for s in llm]
    tools = [s for s in spans if s["name"].startswith("tool.")]
    empty = [s for s in tools if any("No matching documents" in e.get("summary", "") for e in s["events"])]
    return {"root": root, "llm": llm, "parts": parts, "prompt": prompt, "tools": tools, "empty_results": empty,
            "system_hashes": [s["attributes"].get("prompt.system_sha256") for s in llm],
            "handover": any("turn limit" in s["attributes"].get("tool.input", "") for s in tools)}


def cost_with_cache(prompts: list[int], outputs: list[int]) -> float:
    """Counterfactual: a stable prefix -> each call reads the previous prompt from cache and writes the delta."""
    total, previous = 0.0, 0
    for p, out in zip(prompts, outputs):
        total += cost_breakdown({"input_tokens": 0, "cache_read_input_tokens": previous,
                                 "cache_creation_input_tokens": p - previous, "output_tokens": out}, MODEL)["total"]
        previous = p
    return total


def main() -> None:
    header("Exercise 8 - root-causing a cost spike from traces")
    spans = load_jsonl(TRACES)
    by_trace: dict[str, list[dict]] = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)
    base, spike = (profile(v) for v in sorted(by_trace.values(), key=lambda v: v[0]["start"]))

    step(1, "Side by side: price every token by type")
    print(f"  {'':<24}{'baseline':>20}{'spike':>20}")
    rows = [("ticket", base["root"]["attributes"]["ticket"], spike["root"]["attributes"]["ticket"]),
            ("deploy", base["root"]["attributes"]["deploy.version"], spike["root"]["attributes"]["deploy.version"]),
            ("prompt version", base["root"]["attributes"]["prompt.version"],
             spike["root"]["attributes"]["prompt.version"]),
            ("LLM calls (turns)", len(base["llm"]), len(spike["llm"])),
            ("tool calls", len(base["tools"]), len(spike["tools"])),
            ("wall time", f"{base['root']['end'] - base['root']['start']:.1f}s",
             f"{spike['root']['end'] - spike['root']['start']:.1f}s")]
    for label, b, s in rows:
        print(f"  {label:<24}{str(b):>20}{str(s):>20}")
    for part in ("input", "cache_write", "cache_read", "output", "total"):
        print(f"  {'cost: ' + part:<24}{'$' + format(base['parts'][part], '.4f'):>20}"
              f"{'$' + format(spike['parts'][part], '.4f'):>20}")
    print(f"  -> the spike ticket costs {spike['parts']['total'] / base['parts']['total']:.1f}x the baseline; "
          f"{spike['parts']['cache_write'] / spike['parts']['total']:.0%} of it is cache WRITES.")

    step(2, "Hypothesis 1 - the prompt cache stopped working")
    for name, p in (("baseline", base), ("spike", spike)):
        reads = sum(usage(s)["cache_read_input_tokens"] for s in p["llm"])
        print(f"  {name:<9} cache-read share {reads / sum(p['prompt']):6.1%}; distinct system-prompt hashes across "
              f"{len(p['llm'])} calls: {len(set(p['system_hashes']))}")
    print("  Every call in the spike trace sends a DIFFERENT system prompt (12 hashes for 12 calls). The cache is a\n"
          "  prefix match (tools -> system -> messages), so a byte change in the system prompt invalidates the whole\n"
          "  conversation after it: each call re-writes ~3-6k tokens at 1.25x instead of reading them at 0.1x.\n"
          "  Deploy 2026.09.16-2 changed the prompt version; the per-call hash change points at a value rendered into\n"
          "  the system prompt on every call - a timestamp ('Current time: ...') is the classic culprit.")

    step(3, "Hypothesis 2 - the conversation got longer")
    searches = [s for s in spike["tools"] if s["name"] == "tool.search_knowledge_base"]
    print(f"  spike: {len(searches)} knowledge-base searches, {len(spike['empty_results'])} returned 'No matching "
          f"documents'; the agent re-phrased and searched again until the {len(spike['llm'])}-turn limit"
          + (" and handed over to a human." if spike["handover"] else "."))
    print("  Same question type as the baseline, which answered after 2 searches: the knowledge base returned nothing\n"
          "  at all after the deploy (an empty or unmounted index) - the model kept trying, as instructed.")

    step(4, "Quantify each cause with counterfactuals (same token counts, re-priced)")
    outputs = [usage(s)["output_tokens"] for s in spike["llm"]]
    actual = spike["parts"]["total"]
    cache_fixed = cost_with_cache(spike["prompt"], outputs)
    turns = len(base["llm"])
    kb_fixed = sum(cost_breakdown(usage(s), MODEL)["total"] for s in spike["llm"][:turns])
    both = cost_with_cache(spike["prompt"][:turns], outputs[:turns])
    print(f"  actual                                   ${actual:.4f}")
    print(f"  fix the cache only (still 12 turns)      ${cache_fixed:.4f}  ({1 - cache_fixed / actual:.0%} saved)")
    print(f"  fix the knowledge base only ({turns} turns)    ${kb_fixed:.4f}  ({1 - kb_fixed / actual:.0%} saved)")
    print(f"  fix both                                 ${both:.4f}  (baseline ticket: ${base['parts']['total']:.4f})")
    print("  The last gap is the cold first call in the counterfactual: in steady state every ticket also READS the\n"
          "  shared tools + system prefix that earlier tickets cached, as the baseline trace does.")

    step(5, "Root cause and fixes")
    print("  Root cause 1 (cost x3-4): a per-call timestamp in the system prompt broke prompt caching. Fix: keep the\n"
          "    system prompt byte-stable (date, not time; or send volatile context in the user turn after the cached\n"
          "    prefix); verify cache_read_input_tokens on the first calls after every deploy.\n"
          "  Root cause 2 (turns x2.4, and an unresolved ticket): the knowledge base returned no results after the\n"
          "    deploy. Fix: health-check the index at startup (/healthz readiness), and cap repeated identical tool\n"
          "    failures (e.g. 3 empty searches -> answer 'I'll follow up' or escalate) instead of burning 12 turns.\n"
          "  Guards so it cannot happen silently again: alert on cache-read share < 50% per deploy, on mean turns per\n"
          "    ticket > baseline + 50%, and on cost per ticket > $0.30; add a cost-per-ticket assertion to the CI eval.\n"
          "  Also noticed: 'requester' holds raw email addresses in both traces - PII in logs (exercise 10).")


if __name__ == "__main__":
    main()
