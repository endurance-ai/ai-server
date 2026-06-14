"""Per-turn LLM cost accumulator (ContextVar-based).

All LLM calls within a single webhook turn contribute to one shared
accumulator so the final turn_summary reflects the true USD cost —
including Vision (GPT-4o-mini via LLMProvider.chat) and Reflexion
evaluator, not just the ReAct loop.

Usage pattern:
  telegram.py _invoke_graph  → reset_turn()
  llm.py LLMProvider.chat()  → accumulate_raw(model, usage_dict)
  react_loop.py per-LLM-call → accumulate_lc(model, usage_metadata)
  react_loop.py finally      → get_turn_totals()

ContextVar semantics guarantee each asyncio task (= each concurrent
webhook) gets its own isolated accumulator.  Child tasks created via
asyncio.create_task() inherit the parent's context at spawn time, but
mutations (reset_turn) only affect the context that calls them, so
parallel requests cannot cross-contaminate each other.
"""

from __future__ import annotations

import contextvars
from typing import Any

# keys: input_tokens, output_tokens, cache_read_tokens, total_tokens, cost_usd
_TurnState = dict

_state: contextvars.ContextVar[_TurnState] = contextvars.ContextVar("kiko_turn_cost")

_ZERO: _TurnState = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "total_tokens": 0,
    "cost_usd": 0.0,
}

# Cost per million tokens (USD).  First substring match wins — order matters.
# Sources: AWS Bedrock / Anthropic / OpenAI pricing (2025-06).
_MODEL_COSTS: list[tuple[str, dict[str, float]]] = [
    ("nova-micro", {"input": 0.035, "output": 0.140, "cache_read": 0.0035, "cache_creation": 0.035}),
    ("nova-lite", {"input": 0.060, "output": 0.240, "cache_read": 0.0060, "cache_creation": 0.060}),
    ("nova-pro", {"input": 0.800, "output": 3.200, "cache_read": 0.0800, "cache_creation": 0.800}),
    ("claude-haiku-4-5", {"input": 1.000, "output": 5.000, "cache_read": 0.1000, "cache_creation": 1.250}),
    ("claude-3-5-haiku", {"input": 0.800, "output": 4.000, "cache_read": 0.0800, "cache_creation": 1.000}),
    ("claude-haiku", {"input": 0.800, "output": 4.000, "cache_read": 0.0800, "cache_creation": 1.000}),
    ("claude-3-5-sonnet", {"input": 3.000, "output": 15.000, "cache_read": 0.3000, "cache_creation": 3.750}),
    ("claude-sonnet", {"input": 3.000, "output": 15.000, "cache_read": 0.3000, "cache_creation": 3.750}),
    ("claude-3-opus", {"input": 15.00, "output": 75.000, "cache_read": 1.5000, "cache_creation": 18.75}),
    ("claude-opus", {"input": 15.00, "output": 75.000, "cache_read": 1.5000, "cache_creation": 18.75}),
    ("gpt-4o-mini", {"input": 0.150, "output": 0.600, "cache_read": 0.0750, "cache_creation": 0.0}),
    ("gpt-4o", {"input": 2.500, "output": 10.000, "cache_read": 1.2500, "cache_creation": 0.0}),
]


def _rates(model: str) -> dict[str, float] | None:
    lower = model.lower()
    for key, costs in _MODEL_COSTS:
        if key in lower:
            return costs
    return None


def _calc(inp: int, out: int, cr: int, cc: int, r: dict[str, float]) -> float:
    plain_inp = max(0, inp - cr - cc)
    return (plain_inp * r["input"] + out * r["output"] + cr * r["cache_read"] + cc * r["cache_creation"]) / 1_000_000


# ── Public API ────────────────────────────────────────────────────────────────


def reset_turn() -> None:
    """Initialize a fresh accumulator for the current async context.

    Must be called once per webhook turn before any LLM calls.
    """
    _state.set(dict(_ZERO))


def get_turn_totals() -> _TurnState:
    """Return a snapshot of accumulated cost for the current turn.

    Never raises — returns zeros if reset_turn() was never called.
    """
    try:
        return dict(_state.get())
    except LookupError:
        return dict(_ZERO)


def accumulate_raw(model: str, usage: dict[str, Any]) -> None:
    """Accumulate cost from a raw LiteLLM/OpenAI response ``usage`` dict.

    Handles both naming conventions:
    - OpenAI: prompt_tokens / completion_tokens
    - Anthropic: input_tokens / output_tokens
    - OpenAI cached: prompt_tokens_details.cached_tokens
    - Anthropic cached: cache_read_input_tokens / cache_creation_input_tokens
    """
    try:
        s = _state.get()
    except LookupError:
        return  # reset_turn() not called — skip silently (e.g. /recommend endpoint)

    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cr = int(usage.get("cache_read_input_tokens") or details.get("cached_tokens") or 0)
    cc = int(usage.get("cache_creation_input_tokens") or 0)

    s["input_tokens"] += inp
    s["output_tokens"] += out
    s["cache_read_tokens"] += cr
    s["total_tokens"] += inp + out

    r = _rates(model)
    if r:
        s["cost_usd"] += _calc(inp, out, cr, cc, r)


def accumulate_lc(model: str, usage_metadata: dict[str, Any]) -> None:
    """Accumulate cost from a LangChain ``AIMessage.usage_metadata`` dict.

    LangChain normalises Bedrock/Anthropic responses to use
    ``input_tokens`` / ``output_tokens`` / ``total_tokens`` plus the
    Anthropic cache fields.
    """
    try:
        s = _state.get()
    except LookupError:
        return

    inp = int(usage_metadata.get("input_tokens") or 0)
    out = int(usage_metadata.get("output_tokens") or 0)
    cr = int(usage_metadata.get("cache_read_input_tokens") or 0)
    cc = int(usage_metadata.get("cache_creation_input_tokens") or 0)
    total = int(usage_metadata.get("total_tokens") or inp + out)

    s["input_tokens"] += inp
    s["output_tokens"] += out
    s["cache_read_tokens"] += cr
    s["total_tokens"] += total

    r = _rates(model)
    if r:
        s["cost_usd"] += _calc(inp, out, cr, cc, r)
