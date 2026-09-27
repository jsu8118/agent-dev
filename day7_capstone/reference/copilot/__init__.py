"""Kestrel Service Desk Copilot - the capstone reference solution.

    screen.py        M1  deterministic input screen: security signals, safety backstop, identifiers
    triage.py        --  LLM triage with structured outputs (provided in the starter: Day 1 material)
    gate.py          M2  routing decision + deterministic safety / security / human handlers
    pipeline.py      M3  handle_email(): one email in, one auditable Outcome out
    quality.py       M4  quality-hold detector (build records x held supplier lots)
    output_guard.py  M5  send-time checks on the agent's reply
    evals.py         M6  acceptance suite: 40 scenarios through the whole pipeline + go/no-go gates
    service.py       M7  FastAPI service (idempotent, authenticated, metered)
    mcp_server.py    M7  read-only MCP server for staff (Claude Code / Desktop)

Design rationale: ../DESIGN.md. Build walkthrough: ../WALKTHROUGH.md.
"""
