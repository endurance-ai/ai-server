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
from collections.abc import Mapping
from typing import Any
from uuid import UUID

# keys: input_tokens, output_tokens, cache_read_tokens, total_tokens, cost_usd
_TurnState = dict

_state: contextvars.ContextVar[_TurnState] = contextvars.ContextVar("kiko_turn_cost")

_ZERO: _TurnState = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "total_tokens": 0,
    "cost_usd": 0.0,
    "calls": [],
    "turn_id": None,
    "user_key": None,
    "chat_id": None,
    "thread_id": None,
    "turn_no": None,
    "framed": True,
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


def _extract_response_cost(data: Mapping[str, Any]) -> float | None:
    """Return provider/proxy supplied USD cost when LiteLLM includes it."""
    candidates = [
        data.get("response_cost"),
        data.get("cost"),
        (data.get("_hidden_params") or {}).get("response_cost"),
        (data.get("_hidden_params") or {}).get("cost"),
    ]
    for value in candidates:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _record_call(
    *,
    state: _TurnState,
    source: str,
    transport: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    total_tokens: int,
    cost_usd: float,
    cost_source: str,
) -> None:
    call = {
        "turn_id": state.get("turn_id"),
        # `source` = the call-site (vision / evaluator / intent_classifier / ...)
        # so each `llm_call` row is attributable to where the spend originated.
        # `transport` = raw HTTP vs LangChain (kept for debugging).
        "source": source,
        "transport": transport,
        "framed": bool(state.get("framed", True)),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 10),
        "cost_source": cost_source,
    }
    state["calls"].append(call)

    if not state.get("user_key") or state.get("chat_id") is None:
        return
    try:
        from app.observability.conversation_log import emit
        from app.observability.langfuse import current_langfuse_trace_id

        trace_id = current_langfuse_trace_id()
        if trace_id:
            call["langfuse_trace"] = trace_id
        emit(
            event_type="llm_call",
            user_key=str(state["user_key"]),
            chat_id=int(state["chat_id"]),
            thread_id=state.get("thread_id"),
            turn_no=state.get("turn_no"),
            payload=call,
            langfuse_trace=trace_id,
        )
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────


def reset_turn(
    *,
    turn_id: str | None = None,
    user_key: str | None = None,
    chat_id: int | None = None,
    thread_id: UUID | None = None,
    turn_no: int | None = None,
) -> None:
    """Initialize a fresh accumulator for the current async context.

    Must be called once per webhook turn before any LLM calls.
    """
    state = dict(_ZERO)
    state["calls"] = []
    state["turn_id"] = turn_id
    state["user_key"] = user_key
    state["chat_id"] = chat_id
    state["thread_id"] = thread_id
    state["turn_no"] = turn_no
    _state.set(state)


def _state_or_unframed() -> _TurnState:
    try:
        return _state.get()
    except LookupError:
        return _unframed_state()


def _unframed_state() -> _TurnState:
    state = dict(_ZERO)
    state["calls"] = []
    state["user_key"] = "internal:unframed"
    state["chat_id"] = 0
    state["framed"] = False
    return state


def get_turn_totals() -> _TurnState:
    """Return a snapshot of accumulated cost for the current turn.

    Never raises — returns zeros if reset_turn() was never called.
    """
    try:
        return dict(_state.get())
    except LookupError:
        return dict(_ZERO)


def clear_turn() -> None:
    """Mark the current context as outside a user turn.

    Used by request cleanup paths after a framed graph turn completes. Later
    LLM calls in the same async context should still be auditable as unframed
    instead of being absorbed by an empty frame.
    """
    _state.set(_unframed_state())


def get_turn_context() -> dict[str, Any]:
    """Return identifiers attached to the current turn accumulator."""
    try:
        s = _state.get()
    except LookupError:
        return {}
    return {
        "turn_id": s.get("turn_id"),
        "user_key": s.get("user_key"),
        "chat_id": s.get("chat_id"),
        "thread_id": s.get("thread_id"),
        "turn_no": s.get("turn_no"),
    }


def langfuse_metadata() -> dict[str, Any]:
    """Small metadata dict safe to attach to LiteLLM/Langfuse calls."""
    ctx = get_turn_context()
    return {k: v for k, v in ctx.items() if k in {"turn_id"} and v is not None}


def accumulate_raw(
    model: str, usage: dict[str, Any], *, response: dict[str, Any] | None = None, source: str = "raw"
) -> None:
    """Accumulate cost from a raw LiteLLM/OpenAI response ``usage`` dict.

    Handles both naming conventions:
    - OpenAI: prompt_tokens / completion_tokens
    - Anthropic: input_tokens / output_tokens
    - OpenAI cached: prompt_tokens_details.cached_tokens
    - Anthropic cached: cache_read_input_tokens / cache_creation_input_tokens

    ``source`` labels the call-site (e.g. "vision", "intent_classifier") so the
    emitted ``llm_call`` row identifies where the spend came from. Defaults to
    the transport name when a caller does not specify one.
    """
    s = _state_or_unframed()

    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cr = int(usage.get("cache_read_input_tokens") or details.get("cached_tokens") or 0)
    cc = int(usage.get("cache_creation_input_tokens") or 0)

    s["input_tokens"] += inp
    s["output_tokens"] += out
    s["cache_read_tokens"] += cr
    s["total_tokens"] += inp + out

    response_cost = _extract_response_cost(response or {})
    cost_source = "litellm" if response_cost is not None else "fallback_rates"
    cost = response_cost or 0.0
    r = _rates(model)
    if response_cost is None and r:
        cost = _calc(inp, out, cr, cc, r)
    if response_cost is None and r is None:
        cost_source = "unknown_model"
    s["cost_usd"] += cost
    _record_call(
        state=s,
        source=source,
        transport="raw",
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_creation_tokens=cc,
        total_tokens=inp + out,
        cost_usd=cost,
        cost_source=cost_source,
    )


def accumulate_lc(model: str, usage_metadata: dict[str, Any], *, source: str = "langchain") -> None:
    """Accumulate cost from a LangChain ``AIMessage.usage_metadata`` dict.

    LangChain normalises Bedrock/Anthropic responses to use
    ``input_tokens`` / ``output_tokens`` / ``total_tokens`` plus cache fields.
    Cache tokens appear in two locations depending on provider/version:
    - Anthropic convention: ``cache_read_input_tokens`` (top-level)
    - LangChain >=0.3 / Bedrock nova: ``input_token_details.cache_read``
    Both are checked so neither is missed.

    ``source`` labels the call-site (e.g. "react_loop", "ask_clarify").
    """
    s = _state_or_unframed()

    inp = int(usage_metadata.get("input_tokens") or 0)
    out = int(usage_metadata.get("output_tokens") or 0)
    details: dict = usage_metadata.get("input_token_details") or {}
    cr = int(usage_metadata.get("cache_read_input_tokens") or details.get("cache_read") or 0)
    cc = int(usage_metadata.get("cache_creation_input_tokens") or details.get("cache_creation") or 0)
    total = int(usage_metadata.get("total_tokens") or inp + out)

    s["input_tokens"] += inp
    s["output_tokens"] += out
    s["cache_read_tokens"] += cr
    s["total_tokens"] += total

    r = _rates(model)
    cost = _calc(inp, out, cr, cc, r) if r else 0.0
    s["cost_usd"] += cost
    _record_call(
        state=s,
        source=source,
        transport="langchain",
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_creation_tokens=cc,
        total_tokens=total,
        cost_usd=cost,
        cost_source="fallback_rates" if r else "unknown_model",
    )
