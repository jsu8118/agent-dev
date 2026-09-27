"""Turn `usage` blocks into dollars.

Every Messages API response carries a `usage` object:

    input_tokens                  uncached input tokens (full price)
    cache_creation_input_tokens   input tokens written to the prompt cache (1.25x for 5-min TTL, 2x for 1-hour TTL)
    cache_read_input_tokens       input tokens served from the cache (~0.1x; cheaper on some models)
    output_tokens                 generated tokens, INCLUDING thinking tokens

Practitioners should be able to do this arithmetic in their head: agent loops re-send
the whole conversation on every turn, so input tokens (and therefore caching) usually
dominate the bill, not output.
"""

from __future__ import annotations

from typing import Any

from .models import BATCH_DISCOUNT, get_spec, known_model


def _get(obj: Any, name: str, default: Any = 0) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def cost_breakdown(usage: Any, model: str, *, batch: bool = False) -> dict[str, float]:
    """Return a per-component cost breakdown (USD) for one response's usage."""
    if not known_model(model):
        return {"input": 0.0, "cache_write": 0.0, "cache_read": 0.0, "output": 0.0, "total": 0.0}
    spec = get_spec(model)
    per_in = spec.input_price / 1_000_000
    per_out = spec.output_price / 1_000_000

    uncached = _get(usage, "input_tokens")
    cache_read = _get(usage, "cache_read_input_tokens")
    cache_write_total = _get(usage, "cache_creation_input_tokens")
    breakdown = _get(usage, "cache_creation", None)
    write_1h = _get(breakdown, "ephemeral_1h_input_tokens") if breakdown is not None else 0
    write_5m = max(cache_write_total - write_1h, 0)
    output = _get(usage, "output_tokens")

    parts = {
        "input": uncached * per_in,
        "cache_write": write_5m * per_in * 1.25 + write_1h * per_in * 2.0,
        "cache_read": cache_read * per_in * spec.cache_read_multiplier,
        "output": output * per_out,
    }
    factor = BATCH_DISCOUNT if batch else 1.0
    parts = {k: v * factor for k, v in parts.items()}
    parts["total"] = sum(parts.values())
    return parts


def cost_usd(usage: Any, model: str, *, batch: bool = False) -> float:
    return cost_breakdown(usage, model, batch=batch)["total"]


def usage_summary(message: Any, *, batch: bool = False) -> str:
    """One-line usage + cost summary for a Message (SDK object or dict)."""
    usage = _get(message, "usage", None)
    model = _get(message, "model", "")
    cost = cost_usd(usage, model, batch=batch)
    return (
        f"in={_get(usage, 'input_tokens'):,} "
        f"cache_write={_get(usage, 'cache_creation_input_tokens'):,} "
        f"cache_read={_get(usage, 'cache_read_input_tokens'):,} "
        f"out={_get(usage, 'output_tokens'):,} "
        f"~${cost:.5f}"
    )
