"""Per-turn LLM cost accumulator (ContextVar-based).

All LLM calls within a single webhook turn contribute to one shared
accumulator so the final turn_summary reflects the true USD cost —
including Vision (nova-lite via LLMProvider.chat), Reflexion evaluator,
intent classifier, router, and the ReAct loop.

Every accumulation is tagged with a ``source`` label and recorded in a
per-source breakdown (``by_source``) so the cost of each call-site is
individually identifiable — not just the turn total.

Usage pattern:
  telegram.py _invoke_graph  → reset_turn()       (once per turn)
  llm.py LLMProvider.chat()  → accumulate_raw(model, usage, source=...)
  react_loop.py per-LLM-call → accumulate_lc(model, usage_metadata, source="react_loop")
  react_loop.py finally      → get_turn_totals() + mark_summary_emitted()
  telegram.py _invoke_graph  → get_turn_totals() fallback emit (non-agent paths)

ContextVar semantics guarantee each asyncio task (= each concurrent
webhook) gets its own isolated accumulator.  Child tasks created via
asyncio.create_task() inherit the parent's context at spawn time, but
mutations (reset_turn) only affect the context that calls them, so
parallel requests cannot cross-contaminate each other.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)

# keys: input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
#       total_tokens, cost_usd, by_source, summary_emitted
_TurnState = dict

_state: contextvars.ContextVar[_TurnState] = contextvars.ContextVar("kiko_turn_cost")


def _new_state() -> _TurnState:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        # source label → {input, output, cache_read, cache_creation, cost_usd, calls, cost_known}
        "by_source": {},
        # True once a turn_summary row has been persisted for this turn (set by
        # react_loop). The webhook fallback emit reads this to avoid double rows.
        "summary_emitted": False,
    }


# Cost per million tokens (USD).  First substring match wins — order matters.
# Sources: AWS Bedrock / Anthropic / OpenAI pricing.  last_verified 2025-06.
# NOTE: a model whose name does not substring-match any key here is costed at
# $0 with a warning (see _record) — keep this in sync with litellm config.
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
    lower = (model or "").lower()
    for key, costs in _MODEL_COSTS:
        if key in lower:
            return costs
    return None


def _calc(inp: int, out: int, cr: int, cc: int, r: dict[str, float]) -> float:
    plain_inp = max(0, inp - cr - cc)
    return (plain_inp * r["input"] + out * r["output"] + cr * r["cache_read"] + cc * r["cache_creation"]) / 1_000_000


def _record(s: _TurnState, source: str, model: str, inp: int, out: int, cr: int, cc: int) -> None:
    """Apply one LLM call's usage to the turn accumulator + per-source bucket."""
    r = _rates(model)
    cost_known = r is not None
    cost = _calc(inp, out, cr, cc, r) if r else 0.0
    if not cost_known:
        # Unknown model → tokens still counted, but cost is $0. Surface loudly so
        # a new litellm model that was never added to _MODEL_COSTS does not
        # silently undercount the bill (audit finding #5).
        logger.warning(
            "💸 [turn_cost] unknown model for costing: %r (source=%s) — tokens counted, cost=0",
            model,
            source,
        )

    s["input_tokens"] += inp
    s["output_tokens"] += out
    s["cache_read_tokens"] += cr
    s["cache_creation_tokens"] += cc
    s["total_tokens"] += inp + out
    s["cost_usd"] += cost

    bucket = s["by_source"].setdefault(
        source,
        {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "cost_usd": 0.0,
            "calls": 0,
            "cost_known": True,
        },
    )
    bucket["input"] += inp
    bucket["output"] += out
    bucket["cache_read"] += cr
    bucket["cache_creation"] += cc
    bucket["cost_usd"] += cost
    bucket["calls"] += 1
    if not cost_known:
        bucket["cost_known"] = False


# ── Public API ────────────────────────────────────────────────────────────────


def reset_turn() -> None:
    """Initialize a fresh accumulator for the current async context.

    Must be called once per webhook turn before any LLM calls.
    """
    _state.set(_new_state())


def get_turn_totals() -> _TurnState:
    """Return a snapshot of accumulated cost for the current turn.

    Never raises — returns a zeroed state if reset_turn() was never called.
    The returned dict is a shallow copy; ``by_source`` is deep-copied so callers
    can serialize it without racing the live accumulator.
    """
    try:
        s = _state.get()
    except LookupError:
        return _new_state()
    out = dict(s)
    out["by_source"] = {k: dict(v) for k, v in s["by_source"].items()}
    return out


def mark_summary_emitted() -> None:
    """Flag that a turn_summary row was persisted for this turn.

    Read by the webhook-level fallback emit so non-agent turns get exactly one
    cost row (audit finding #2). No-op when reset_turn() was never called.
    """
    try:
        s = _state.get()
    except LookupError:
        return
    s["summary_emitted"] = True


def accumulate_raw(model: str, usage: dict[str, Any], *, source: str = "unknown") -> None:
    """Accumulate cost from a raw LiteLLM/OpenAI response ``usage`` dict.

    Handles both naming conventions:
    - OpenAI: prompt_tokens / completion_tokens
    - Anthropic: input_tokens / output_tokens
    - OpenAI cached: prompt_tokens_details.cached_tokens
    - Anthropic cached: cache_read_input_tokens / cache_creation_input_tokens

    ``source`` labels the call-site (e.g. "vision", "intent_classifier") for the
    per-source breakdown.
    """
    try:
        s = _state.get()
    except LookupError:
        return  # reset_turn() not called — skip silently (e.g. unframed call)

    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cr = int(usage.get("cache_read_input_tokens") or details.get("cached_tokens") or 0)
    cc = int(usage.get("cache_creation_input_tokens") or 0)

    _record(s, source, model, inp, out, cr, cc)


def accumulate_lc(model: str, usage_metadata: dict[str, Any], *, source: str = "unknown") -> None:
    """Accumulate cost from a LangChain ``AIMessage.usage_metadata`` dict.

    LangChain normalises Bedrock/Anthropic responses to use
    ``input_tokens`` / ``output_tokens`` / ``total_tokens`` plus cache fields.
    Cache tokens appear in two locations depending on provider/version:
    - Anthropic convention: ``cache_read_input_tokens`` (top-level)
    - LangChain >=0.3 / Bedrock nova: ``input_token_details.cache_read``
    Both are checked so neither is missed.
    """
    try:
        s = _state.get()
    except LookupError:
        return

    inp = int(usage_metadata.get("input_tokens") or 0)
    out = int(usage_metadata.get("output_tokens") or 0)
    details: dict = usage_metadata.get("input_token_details") or {}
    cr = int(usage_metadata.get("cache_read_input_tokens") or details.get("cache_read") or 0)
    cc = int(usage_metadata.get("cache_creation_input_tokens") or details.get("cache_creation") or 0)

    _record(s, source, model, inp, out, cr, cc)
