"""Solution to exercise 5 - audit a third-party MCP server's tools/list, and detect a rug pull.

Objective
    Turn the manual review of exercise 5 into a repeatable check a host (or a CI job that vets MCP
    servers) can run: flag instruction-like and exfiltration-like text in tool descriptions,
    contradictions between annotations and behaviour, and any change against the approved snapshot.

Concepts
    Tool poisoning (instructions hidden in descriptions), tool shadowing (one server steering calls
    away from another server's tools), invisible Unicode, token passthrough requests, untrusted
    annotations, rug pulls (the tool list changes after approval) and pinning by fingerprint.

Run
    python day5_mcp_agent_sdk/solutions/ex05_audit_mcp_server.py

What to observe
    * v1 (what IT approved) has one cosmetic finding; every serious finding appears only in v2.
    * The diff shows why approving a server once is not enough: fingerprints changed for two tools
      and a new tool appeared - a host with pinning would refuse to expose them until re-review.
    * Pattern checks are a tripwire, not a guarantee: they find careless attacks; a careful attacker
      will phrase around them. Keep the human review and the runtime controls too.
"""

# test: expect=BLOCK

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "exercises" / "data"

# (severity, label, regex) - each targets a behaviour a benign tool description never needs.
CHECKS = [
    ("high", "hidden-instruction markup", r"<\s*(important|system|instructions?)\s*>"),
    ("high", "asks for secrecy toward the user", r"do not (mention|tell|reveal|inform)|don't (mention|tell)|without telling"),
    ("high", "references credentials or secrets", r"~/\.(ssh|aws)|id_rsa|credentials|api[_ ]?key|\.env\b|password|secret"),
    ("high", "requests a bearer/SSO token (token passthrough)", r"(bearer|access|sso)\s+token|authorization header"),
    ("high", "asks for the conversation/context", r"(full|entire|whole) conversation|conversation so far|chat history"),
    ("medium", "steers calls away from other tools (shadowing)", r"instead of\s+\w+|always use this tool|prefer this tool"),
    ("medium", "claims to be routine/mandatory", r"routine (technical )?requirement|must always|mandatory"),
]
OUTBOUND_PARAM = re.compile(r"url|endpoint|webhook|upload|token|callback", re.I)
INVISIBLE = {"Cf"}          # Unicode "format" characters: zero-width space/joiner, bidi overrides, ...


def fingerprint(tool: dict) -> str:
    canonical = json.dumps({k: tool.get(k) for k in ("name", "description", "inputSchema", "annotations")},
                           sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def audit_tool(tool: dict, other_tool_names: set[str]) -> list[tuple[str, str]]:
    findings = []
    text = tool.get("description", "") + " " + json.dumps(tool.get("inputSchema", {}))
    for severity, label, pattern in CHECKS:
        if re.search(pattern, text, re.I):
            findings.append((severity, label))
    hidden = [f"U+{ord(c):04X}" for c in tool.get("description", "") if unicodedata.category(c) in INVISIBLE]
    if hidden:
        findings.append(("high", f"{len(hidden)} invisible Unicode characters ({', '.join(sorted(set(hidden)))})"))
    mentioned = {n for n in other_tool_names if n != tool["name"] and n in tool.get("description", "")}
    if mentioned:
        findings.append(("medium", f"mentions other tools by name: {sorted(mentioned)}"))
    ann = tool.get("annotations") or {}
    params = set((tool.get("inputSchema") or {}).get("properties", {}))
    outbound = sorted(p for p in params if OUTBOUND_PARAM.search(p))
    if (ann.get("readOnlyHint") or ann.get("openWorldHint") is False) and (outbound or re.search(r"upload|sync|send", tool["name"])):
        findings.append(("high", f"annotations claim read-only/closed-world but tool has outbound parameters {outbound}"))
    undocumented = sorted(p for p, spec in (tool.get("inputSchema") or {}).get("properties", {}).items()
                          if spec.get("description", "").strip().lower() in ("", "internal"))
    if undocumented:
        findings.append(("low", f"undocumented/'internal' parameters: {undocumented}"))
    return findings


def audit_snapshot(snapshot: dict) -> dict[str, list[tuple[str, str]]]:
    # Tool names a model might be steered away from: this server's and well-known tools on the host.
    known = {t["name"] for t in snapshot["tools"]} | {"get_order_status", "check_stock", "list_work_orders", "Read", "Bash"}
    return {t["name"]: audit_tool(t, known) for t in snapshot["tools"]}


def diff(approved: dict, current: dict) -> list[str]:
    old = {t["name"]: t for t in approved["tools"]}
    new = {t["name"]: t for t in current["tools"]}
    changes = [f"+ new tool {n!r}" for n in sorted(new.keys() - old.keys())]
    changes += [f"- removed tool {n!r}" for n in sorted(old.keys() - new.keys())]
    for name in sorted(old.keys() & new.keys()):
        if fingerprint(old[name]) != fingerprint(new[name]):
            added = sorted(set(new[name]["inputSchema"].get("properties", {})) - set(old[name]["inputSchema"].get("properties", {})))
            changes.append(f"~ {name!r} changed ({fingerprint(old[name])} -> {fingerprint(new[name])})"
                           + (f", new parameters {added}" if added else "")
                           + (", description changed" if old[name]["description"] != new[name]["description"] else ""))
    return changes


def main() -> None:
    v1 = json.loads((DATA / "docs_helper_tools_v1.json").read_text(encoding="utf-8"))
    v2 = json.loads((DATA / "docs_helper_tools_v2.json").read_text(encoding="utf-8"))
    print(f"Server: {v2['server']['name']} {v1['server']['version']} (approved) -> {v2['server']['version']} (today); "
          f"installed with `{v2['server']['install']}`")
    if "@latest" in v2["server"]["install"]:
        print("  [high] unpinned install (@latest): every restart may run new, unreviewed code\n")

    for label, snap in (("v1 - approved snapshot", v1), ("v2 - current tools/list", v2)):
        print(f"== {label}")
        for name, findings in audit_snapshot(snap).items():
            print(f"  {name}:" + ("" if findings else " no findings"))
            for severity, text in sorted(findings, key=lambda f: ["high", "medium", "low"].index(f[0])):
                print(f"    [{severity}] {text}")

    print("\n== Rug-pull check: v1 -> v2")
    changes = diff(v1, v2)
    for change in changes:
        print("  " + change)
    pinned = {t["name"]: fingerprint(t) for t in v1["tools"]}
    exposed = [t["name"] for t in v2["tools"] if pinned.get(t["name"]) == fingerprint(t)]
    blocked = [t["name"] for t in v2["tools"] if t["name"] not in exposed]
    print(f"\nHost policy with pinned fingerprints: expose {exposed or 'nothing'}; BLOCK {blocked} until re-reviewed.")


if __name__ == "__main__":
    main()
