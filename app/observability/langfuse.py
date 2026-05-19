"""Langfuse v3 tracing.

SPEC-OBSERVABILITY-002. Single-path v3 wiring. When `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` are absent OR the v3 SDK fails to import, the module
degrades to a transparent no-op: `observe(...)` is a passthrough decorator,
`build_callback_handler(...)` returns `None`. The bot starts cleanly in all
fallback cases (REQ-OBS-FALLBACK-001 / REQ-OBS-FALLBACK-002).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.config import settings

logger = logging.getLogger(__name__)

# Langfuse SDK reads `os.environ` directly — mirror settings into env so that
# `get_client()` picks up self-host config without explicit init.
for _k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
    _v = getattr(settings, _k, "")
    if _v and not os.environ.get(_k):
        os.environ[_k] = str(_v)

P = ParamSpec("P")
R = TypeVar("R")

_ENABLED = bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)
_SELECTIVE_MODE = bool(getattr(settings, "LANGFUSE_SELECTIVE_MODE", False))

# REQ-OBS-COST-002 — when selective mode is on, decoration of these nodes
# collapses to no-op even when Langfuse is enabled.
_SELECTIVE_NOOP_SPAN_NAMES = frozenset(
    {
        "node.ingest",
        "node.resolve_image",
        "node.pick_item",
        "node.ask_clarify",
        "node.apply_clarify",
        "node.search",
        "node.send_results",
        "node.taste_update",
    }
)

# v3 → no-op (REQ-OBS-MIGRATION-001 — no v2 cascade).
_lf_observe: Callable[..., Any] | None = None
_lf_get_client: Callable[..., Any] | None = None
if _ENABLED:
    try:
        from langfuse import get_client as _lf_get_client_v3
        from langfuse import observe as _lf_observe_v3

        _lf_observe = _lf_observe_v3
        _lf_get_client = _lf_get_client_v3
    except ImportError:
        logger.error(
            "🐱 [langfuse] v3 SDK import failed — tracing disabled (install `langfuse>=3,<4`)",
            exc_info=True,
        )
        _lf_observe = None
        _lf_get_client = None


def _noop_decorator(
    name: str | None = None,
    **kwargs: Any,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Transparent passthrough decorator (Langfuse disabled / SDK missing)."""

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kw: P.kwargs) -> R:
            return await fn(*args, **kw)

        return wrapper

    return decorator


if _lf_observe is not None:

    def observe(
        name: str | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        # REQ-OBS-COST-002 — selective-mode rollback skips non-LLM nodes.
        if _SELECTIVE_MODE and name in _SELECTIVE_NOOP_SPAN_NAMES:
            return _noop_decorator(name=name, **kwargs)
        return _lf_observe(name=name, **kwargs)  # type: ignore[no-any-return,misc]
else:
    observe = _noop_decorator  # type: ignore[assignment]


def update_current_span(metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Attach metadata to the currently-active Langfuse span (v3 API).

    Used inside node bodies for values that are computed mid-function — e.g.
    `iteration` index in the Reflexion loop's `evaluator` node. No-op when
    Langfuse is disabled.
    """
    if _lf_get_client is None:
        return
    try:
        client = _lf_get_client()
        client.update_current_span(metadata=metadata, **kwargs)
    except Exception:  # noqa: BLE001 — tracing is best-effort
        pass


def update_current_trace(metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Attach metadata to the root trace (v3 API). No-op when disabled."""
    if _lf_get_client is None:
        return
    try:
        client = _lf_get_client()
        client.update_current_trace(metadata=metadata, **kwargs)
    except Exception:  # noqa: BLE001
        pass


# @MX:ANCHOR: [AUTO] SPEC-CONVERSATION-LOG-001 — caller-context trace_id capture
# @MX:REASON: every emit() in app/observability/conversation_log.py reads this before scheduling
# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def current_langfuse_trace_id() -> str | None:
    """Return current Langfuse v3 trace_id, or None. **Never raises.**

    REQ-LOG-LANGFUSE-XREF-001 — fallback cascade:
      1. v3 client `get_current_observation().trace_id`
      2. `langfuse.langfuse_context.get_current_trace_id()`
      3. None

    Called from `emit(...)` in *caller* context (BEFORE asyncio.create_task)
    so the contextvar resolution happens on the active task's stack
    (plan §8.2, mitigates R8 — contextvar loss across task boundary).
    """
    if _lf_get_client is None:
        return None
    # Cascade 1 — v3 client current observation.
    try:
        client = _lf_get_client()
        obs = client.get_current_observation()
        if obs is not None:
            tid = getattr(obs, "trace_id", None)
            if tid:
                return str(tid)
    except Exception:  # noqa: BLE001 — best-effort, never raise
        pass
    # Cascade 2 — `langfuse_context` accessor (older / contextvar-only path).
    try:
        from langfuse import langfuse_context  # type: ignore[attr-defined]

        tid = langfuse_context.get_current_trace_id()
        if tid:
            return str(tid)
    except Exception:  # noqa: BLE001
        pass
    return None


# P0 user-feedback scores — signal → (score name, value) table. re_query emits
# TWO scores so it is filterable as a distinct boolean-ish signal while still
# contributing a 0.0 user_feedback like a negative.
_FEEDBACK_SCORES: dict[str, tuple[tuple[str, float], ...]] = {
    "click": (("user_feedback", 1.0),),
    "no_click": (("user_feedback", 0.0),),
    "re_query": (("user_feedback", 0.0), ("re_query", 1.0)),
}


# @MX:ANCHOR: [AUTO] P0 user-feedback score sink — fan_in from implicit_feedback
#   record_click / attribute_expired_impressions / detect_and_apply_re_query
# @MX:REASON: single source for retro-scoring; a raise here would break the
#   feedback path and the webhook, so it MUST stay fail-open / never-raise.
def emit_feedback_score(
    trace_id: str | None,
    *,
    signal: str,
    product_id: str | None = None,
    brand: str | None = None,
    attribution_window_s: int | None = None,
) -> None:
    """Retro-attach implicit-feedback score(s) to the ORIGINAL recommendation
    trace (the one active when the cards were sent), by trace id.

    Fail-open / never raises. Silent no-op when Langfuse is disabled, the
    kill-switch (`LANGFUSE_FEEDBACK_SCORES`) is off, `trace_id` is missing, or
    `signal` is unknown. Scoring failures are logged at WARNING and swallowed —
    they must NEVER propagate into the feedback path or the webhook.

    `signal` ∈ {"click", "no_click", "re_query"}. `create_score()` is the v3
    SDK API (`langfuse.create_score(trace_id=..., name=..., value=...,
    data_type="NUMERIC")`); it is queued on the SDK background thread (non-
    blocking), so it is safe to call directly from async code — consistent
    with how the rest of this module calls the v3 client synchronously.
    """
    if _lf_get_client is None:
        return
    if not getattr(settings, "LANGFUSE_FEEDBACK_SCORES", True):
        return
    if not trace_id:
        return
    if len(trace_id) > 128:
        return
    scores = _FEEDBACK_SCORES.get(signal)
    if not scores:
        return
    comment_parts = [f"source=implicit_feedback.{signal}"]
    if product_id:
        comment_parts.append(f"product_id={product_id}")
    if brand:
        comment_parts.append(f"brand={brand}")
    if attribution_window_s is not None:
        comment_parts.append(f"attribution_window_s={attribution_window_s}")
    comment = " ".join(comment_parts)
    try:
        client = _lf_get_client()
        for name, value in scores:
            client.create_score(
                trace_id=trace_id,
                name=name,
                value=float(value),
                data_type="NUMERIC",
                comment=comment,
            )
    except Exception:  # noqa: BLE001 — scoring is best-effort; never break feedback/webhook
        logger.warning(
            "🐱 [langfuse] feedback score emit failed (signal=%s trace=%s) — swallowed",
            signal,
            trace_id,
            exc_info=True,
        )


def flush() -> None:
    """Drain the SDK background queue. Call on lifespan shutdown."""
    if _lf_get_client is None:
        return
    try:
        _lf_get_client().flush()
    except Exception:  # noqa: BLE001
        pass


# REQ-OBS-CALLBACK-001 — v3 LangChain CallbackHandler factory.
# @MX:ANCHOR: [AUTO] high fan_in — called per webhook from 12 graph nodes via LangGraph callback propagation
# @MX:REASON: This is the single bridge between LangGraph runnable callbacks and Langfuse v3 nested-generation spans.


def build_callback_handler(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    """Return a Langfuse v3 `CallbackHandler` bound to the current webhook trace.

    Callers MUST pass pre-hashed `session_id` / `user_id` (use
    `app.observability.pii.hash_id`). Raw `chat_id` / `from_user_id` MUST NEVER
    be passed to this function. Returns `None` when Langfuse is disabled OR the
    v3 langchain sub-module import fails — callers should filter `None` from
    the `RunnableConfig.callbacks` list.
    """
    if not _ENABLED:
        return None
    try:
        from langfuse.langchain import CallbackHandler  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — broad on purpose; any import path failure → no-op
        logger.warning("🐱 [langfuse] CallbackHandler import failed — nested-LLM tracing disabled")
        return None
    try:
        # v3 CallbackHandler attaches metadata via update_current_trace lazily;
        # pass it through __init__ for surface compatibility.
        kwargs: dict[str, Any] = {}
        if session_id is not None:
            kwargs["session_id"] = session_id
        if user_id is not None:
            kwargs["user_id"] = user_id
        if metadata:
            kwargs["metadata"] = metadata
        return CallbackHandler(**kwargs)
    except TypeError:
        # Some v3 minor versions accept fewer kwargs — fall back to bare construction.
        try:
            return CallbackHandler()
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
