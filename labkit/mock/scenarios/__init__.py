"""Scenario policies, one module per course day.

Every module in this package is imported automatically by the registry.  A scenario
is a rule-based stand-in for Claude that produces plausible, deterministic behaviour
for one lab (which tools to call, in what order, and what to answer), so the labs can
run - and be tested - without an API key.  They are NOT models: in live mode Claude's
real behaviour will differ in wording and sometimes in strategy.
"""
