"""labkit - the shared toolkit for the "Agent Development with Claude" course.

    from labkit import get_client, MODEL, FAST_MODEL, is_mock
    client = get_client()          # live Claude if ANTHROPIC_API_KEY is set, else the offline mock

Modules:
    config    - models, mode (live/mock), paths
    client    - get_client() / get_async_client()
    models    - model catalog: prices, limits, which parameters each model accepts
    pricing   - usage -> dollars
    metering  - SDK middleware that meters every call; prints a cost summary at exit
    display   - printing helpers (show_message, text_of, header, step)
    data      - dataset access (SQLite ops DB, JSON/JSONL/markdown files)
    tracing   - a tiny OpenTelemetry-style tracer for agent runs
    mock      - the offline mock Claude API (used automatically in mock mode)
"""

from .client import get_async_client, get_client, mock_api
from .config import DATA_DIR, FAST_MODEL, MID_MODEL, MODEL, REPO_ROOT, is_mock, mode, runs_dir
from .display import header, print_json, show_message, step, text_of, wrap
from .metering import LEDGER
from .models import fallback_kwargs, get_spec, supports_effort
from .pricing import cost_usd, usage_summary

__all__ = [
    "get_client", "get_async_client", "mock_api",
    "MODEL", "MID_MODEL", "FAST_MODEL", "DATA_DIR", "REPO_ROOT", "is_mock", "mode", "runs_dir",
    "header", "step", "show_message", "text_of", "print_json", "wrap",
    "LEDGER", "fallback_kwargs", "get_spec", "supports_effort", "cost_usd", "usage_summary",
]
