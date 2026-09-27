"""kestrel - Kestrel Pumps & Controls' (fictional) internal systems library.

Plays the role of the company codebase your agents integrate with:
    policy         - business rules as deterministic code (returns, warranty, refund limits)
    kb             - BM25 search over policies and product manuals
    support_tools  - the reference customer-support toolset (SupportDesk + TOOLS schemas)
    support_agent  - the reference support agent (system prompt + agent loop)
"""
