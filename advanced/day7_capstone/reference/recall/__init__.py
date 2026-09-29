"""Reference solution for the advanced capstone: the Recall Campaign Orchestrator.

    config        settings, paths, roles, SLAs, budgets
    store         the campaign's durable state (SQLite): units, customers, messages, slots, parts, credits, audit
    tools         the campaign toolset and its executor (capability-scoped, idempotent, approval-gated)
    security      inbound screening and outbound sanitisation (deterministic)
    agents        the outreach and inbound agents, each a DurableRunner run
    budget        swarm budget, circuit breaker and loop detection
    orchestrator  the durable coordinator: plan, dispatch, process replies, reminders, stop conditions
    evals         the acceptance suite and its gates
"""
