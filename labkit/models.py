"""Model catalog: capabilities, limits, and prices for the Claude models used in the course.

Why a catalog?  Real agent systems route work across several models (a flagship for
planning, a cheap fast model for classification or judging).  The models do NOT share
one request surface - e.g. `temperature` is rejected by Claude Opus 5 but accepted by
Claude Haiku 4.5, and `output_config.effort` is the other way round.  Keeping these
facts in one table lets the rest of the code ask "can I send X to model Y?" instead of
scattering `if model == ...` checks everywhere.

The values mirror Anthropic's published model documentation (cached 2026).  For live
data use the Models API: `client.models.retrieve("claude-opus-5")` returns
`max_input_tokens`, `max_tokens` and a `capabilities` tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EFFORT_LEVELS_ALL = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    tier: str                      # "frontier" | "flagship" | "balanced" | "fast"
    context_window: int            # max input tokens
    max_output: int                # max output tokens (thinking + text)
    input_price: float             # USD per million input tokens
    output_price: float            # USD per million output tokens
    cache_read_multiplier: float = 0.1   # cache reads cost this x base input price
    # --- thinking -------------------------------------------------------------
    thinking_default: str = "off"        # what happens when `thinking` is omitted: "adaptive" | "off"
    supports_adaptive: bool = True       # thinking={"type": "adaptive"}
    budget_tokens: str = "removed"       # "removed" (400) | "deprecated" (works) | "required" (only way to think)
    disable_thinking: str = "yes"        # "yes" | "effort<=high" | "no"
    # --- other request surface -----------------------------------------------
    effort_levels: tuple[str, ...] = EFFORT_LEVELS_ALL   # () means `effort` is rejected
    default_effort: str | None = "high"
    sampling_params: str = "no"          # temperature/top_p/top_k: "yes" | "default-only" | "no"
    prefill: bool = False                # may the last message be an assistant turn?
    forced_tool_choice: bool = True      # tool_choice {"type": "any"} / {"type": "tool"}
    mid_conversation_system: bool = False  # {"role": "system"} entries inside `messages`
    cache_min_tokens: int = 1024         # shortest cacheable prefix
    refusal_classifiers: bool = False    # may return stop_reason "refusal" from safety classifiers
    server_fallbacks: bool = False       # supports `fallbacks="default"` (beta)
    notes: tuple[str, ...] = field(default_factory=tuple)


CATALOG: dict[str, ModelSpec] = {
    spec.id: spec
    for spec in [
        ModelSpec(
            id="claude-fable-5-1", display_name="Claude Fable 5.1", tier="frontier",
            context_window=1_000_000, max_output=128_000, input_price=10.0, output_price=50.0,
            cache_read_multiplier=0.025, thinking_default="adaptive", disable_thinking="no",
            forced_tool_choice=False, mid_conversation_system=True, cache_min_tokens=512,
            refusal_classifiers=True, server_fallbacks=True,
            notes=("thinking always on; control depth with effort", "forced tool_choice returns 400"),
        ),
        ModelSpec(
            id="claude-fable-5", display_name="Claude Fable 5", tier="frontier",
            context_window=1_000_000, max_output=128_000, input_price=10.0, output_price=50.0,
            thinking_default="adaptive", disable_thinking="no", mid_conversation_system=True,
            cache_min_tokens=512, refusal_classifiers=True, server_fallbacks=True,
        ),
        ModelSpec(
            id="claude-opus-5-5", display_name="Claude Opus 5.5", tier="flagship",
            context_window=1_000_000, max_output=128_000, input_price=4.0, output_price=20.0,
            cache_read_multiplier=0.05, thinking_default="adaptive", disable_thinking="no",
            default_effort="medium", forced_tool_choice=False, mid_conversation_system=True,
            cache_min_tokens=512, refusal_classifiers=True, server_fallbacks=True,
            notes=("effort defaults to medium", "forced tool_choice returns 400"),
        ),
        ModelSpec(
            id="claude-opus-5", display_name="Claude Opus 5", tier="flagship",
            context_window=1_000_000, max_output=128_000, input_price=5.0, output_price=25.0,
            thinking_default="adaptive", disable_thinking="effort<=high", mid_conversation_system=True,
            cache_min_tokens=512, refusal_classifiers=True, server_fallbacks=True,
            notes=("thinking on by default (adaptive)", "temperature/top_p/top_k rejected"),
        ),
        ModelSpec(
            id="claude-opus-4-8", display_name="Claude Opus 4.8", tier="flagship",
            context_window=1_000_000, max_output=128_000, input_price=5.0, output_price=25.0,
            thinking_default="off", mid_conversation_system=True, cache_min_tokens=1024,
        ),
        ModelSpec(
            id="claude-opus-4-7", display_name="Claude Opus 4.7", tier="flagship",
            context_window=1_000_000, max_output=128_000, input_price=5.0, output_price=25.0,
            thinking_default="off", cache_min_tokens=2048,
        ),
        ModelSpec(
            id="claude-opus-4-6", display_name="Claude Opus 4.6", tier="flagship",
            context_window=1_000_000, max_output=128_000, input_price=5.0, output_price=25.0,
            thinking_default="off", budget_tokens="deprecated",
            effort_levels=("low", "medium", "high", "max"), sampling_params="yes", cache_min_tokens=4096,
        ),
        ModelSpec(
            id="claude-sonnet-5", display_name="Claude Sonnet 5", tier="balanced",
            context_window=1_000_000, max_output=128_000, input_price=2.0, output_price=10.0,
            thinking_default="adaptive", sampling_params="default-only", cache_min_tokens=1024,
            notes=("new tokenizer: ~30% more tokens than Sonnet 4.6 for the same text",),
        ),
        ModelSpec(
            id="claude-sonnet-4-6", display_name="Claude Sonnet 4.6", tier="balanced",
            context_window=1_000_000, max_output=128_000, input_price=3.0, output_price=15.0,
            thinking_default="off", budget_tokens="deprecated",
            effort_levels=("low", "medium", "high", "max"), sampling_params="yes", cache_min_tokens=1024,
        ),
        ModelSpec(
            id="claude-haiku-4-5", display_name="Claude Haiku 4.5", tier="fast",
            context_window=200_000, max_output=64_000, input_price=1.0, output_price=5.0,
            thinking_default="off", supports_adaptive=False, budget_tokens="required",
            effort_levels=(), default_effort=None, sampling_params="yes", prefill=True,
            cache_min_tokens=4096,
            notes=("thinking only via budget_tokens", "no effort parameter"),
        ),
    ]
}

# Dated alias accepted by the API for Haiku 4.5.
ALIASES = {"claude-haiku-4-5-20251001": "claude-haiku-4-5"}

BATCH_DISCOUNT = 0.5  # Message Batches API bills all tokens at 50%


def get_spec(model: str) -> ModelSpec:
    """Return the spec for a model id (raises KeyError for unknown ids)."""
    model = ALIASES.get(model, model)
    return CATALOG[model]


def known_model(model: str) -> bool:
    return ALIASES.get(model, model) in CATALOG


def supports_effort(model: str, level: str = "high") -> bool:
    spec = get_spec(model)
    return level in spec.effort_levels


def thinking_active(model: str, thinking: dict | None) -> bool:
    """Will this request think?  (Omitting `thinking` means different things per model.)"""
    spec = get_spec(model)
    if thinking is None:
        return spec.thinking_default == "adaptive"
    return thinking.get("type") in ("adaptive", "enabled")


def fallback_kwargs(model: str) -> dict:
    """Keyword arguments that opt a `client.beta.messages.*` call into server-side refusal fallbacks.

    Models with safety classifiers (Claude Opus 5, Opus 5.5, Fable 5/5.1) can decline a
    benign request with `stop_reason == "refusal"`.  With `fallbacks="default"` the API
    re-runs a declined request on Anthropic's recommended fallback model inside the same
    call.  For other models this returns {} so the same code works everywhere.
    """
    if known_model(model) and get_spec(model).server_fallbacks:
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}
