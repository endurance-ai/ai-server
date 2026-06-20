"""Langfuse v3 tracing.

SPEC-OBSERVABILITY-002. Single-path v3 wiring. When `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` are absent OR the v3 SDK fails to import, the module
degrades to a transparent no-op: `observe(...)` is a passthrough decorator,
`build_callback_handler(...)` returns `None`. The bot starts cleanly in all
fallback cases (REQ-OBS-FALLBACK-001 / REQ-OBS-FALLBACK-002).
"""

from __future__ import annotations

import contextvars
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
_TRACE_EVENTS: contextvars.ContextVar[list[dict[str, Any]]] = contextvars.ContextVar(
    "kiko_langfuse_trace_events",
    default=[],
)
_TRACE_EVENT_LIMIT = 80

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
    except Exception:  # noqa: BLE001 — covers ImportError + pydantic.v1 ConfigError on Py3.14+
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


def reset_trace_story() -> None:
    """Clear per-turn conversation breadcrumbs for the current async context."""
    _TRACE_EVENTS.set([])


def _safe_preview(text: Any, *, limit: int = 160) -> str | None:
    if text is None:
        return None
    value = str(text).replace("\n", " ").strip()
    if not value:
        return None
    return value[:limit]


def _summarize_trace_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, Langfuse-friendly event summary.

    The conversation event table remains the full analytics log. Langfuse gets
    only operator-facing breadcrumbs so a trace is readable without opening DB
    rows. Keep this intentionally small to avoid metadata bloat.
    """
    if event_type == "user_text":
        return {
            "text": _safe_preview(payload.get("text")),
            "lang": payload.get("lang_detected"),
        }
    if event_type == "user_photo":
        return {
            "has_image": bool(payload.get("attachment_id") or payload.get("image_url")),
            "caption": _safe_preview(payload.get("caption"), limit=100),
        }
    if event_type == "user_callback":
        return {"callback": _safe_preview(payload.get("callback_data"), limit=80)}
    if event_type == "intent_routed":
        return {
            "intent": payload.get("intent"),
            "critique": _safe_preview(payload.get("critique_delta_summary"), limit=100),
        }
    if event_type == "vision_done":
        items = payload.get("items") or []
        return {
            "style": payload.get("style") or payload.get("style_node_primary"),
            "mood": (payload.get("mood") or [])[:3],
            "palette": (payload.get("palette") or [])[:3],
            "items": len(items) if isinstance(items, list) else None,
            "error": _safe_preview(payload.get("error"), limit=80),
        }
    if event_type == "pick_item_done":
        candidates = payload.get("candidate_items") or []
        return {
            "picked_index": payload.get("picked_index"),
            "auto": payload.get("auto_picked"),
            "candidates": len(candidates) if isinstance(candidates, list) else None,
        }
    if event_type == "search_done":
        return {
            "query_keys": sorted((payload.get("query") or {}).keys())[:12]
            if isinstance(payload.get("query"), dict)
            else None,
            "products": len(payload.get("top_k_product_ids") or []),
            "dense": payload.get("dense_count"),
            "sparse": payload.get("sparse_count"),
        }
    if event_type == "card_sent":
        return {
            "product_id": payload.get("product_id"),
            "position": payload.get("position"),
            "ok": payload.get("send_ok"),
        }
    if event_type == "bot_text":
        return {
            "text": _safe_preview(payload.get("chunk_text")),
            "chunk": payload.get("chunk_index"),
            "chunks": payload.get("total_chunks"),
            "flow": payload.get("flow"),
        }
    if event_type == "tool_call":
        return {
            "tool": payload.get("tool_name"),
            "iter": payload.get("iteration_no"),
            "latency_ms": payload.get("latency_ms"),
            "error": _safe_preview(payload.get("error"), limit=80),
        }
    if event_type == "llm_call":
        return {
            "model": payload.get("model"),
            "tokens": payload.get("total_tokens"),
            "cost_usd": payload.get("cost_usd"),
            "cost_source": payload.get("cost_source"),
        }
    if event_type == "turn_summary":
        return {
            "status": payload.get("status"),
            "exit": payload.get("exit_reason"),
            "tokens": payload.get("total_tokens"),
            "cost_usd": payload.get("cost_usd"),
            "llm_calls": payload.get("llm_call_count"),
        }
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = _safe_preview(value, limit=100) if isinstance(value, str) else value
        if len(out) >= 6:
            break
    return out


def add_trace_event(event_type: str, payload: dict[str, Any]) -> None:
    """Append one compact conversation breadcrumb to the active Langfuse trace."""
    try:
        current = list(_TRACE_EVENTS.get())
        event = {
            "idx": len(current) + 1,
            "type": event_type,
            **{k: v for k, v in _summarize_trace_payload(event_type, payload).items() if v is not None},
        }
        current.append(event)
        if len(current) > _TRACE_EVENT_LIMIT:
            current = current[-_TRACE_EVENT_LIMIT:]
        _TRACE_EVENTS.set(current)
        update_current_trace(
            metadata={
                "conversation_flow": current,
                "conversation_last_event": event,
                "conversation_event_count": len(current),
            }
        )
    except Exception:  # noqa: BLE001
        pass


# @MX:ANCHOR: [AUTO] SPEC-CONVERSATION-LOG-001 — caller-context trace_id capture
# @MX:REASON: every emit() in app/observability/conversation_log.py reads this before scheduling
# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def current_langfuse_trace_id() -> str | None:
    """Return current Langfuse v3 trace_id, or None. **Never raises.**

    REQ-LOG-LANGFUSE-XREF-001 — resolves the active trace via the v3 SDK's
    `client.get_current_trace_id()` (OTEL-context-local; returns `str | None`).
    Returns None when Langfuse is disabled or no trace is active.

    Called from `emit(...)` in *caller* context (BEFORE asyncio.create_task)
    so the contextvar resolution happens on the active task's stack
    (plan §8.2, mitigates R8 — contextvar loss across task boundary).
    """
    if _lf_get_client is None:
        return None
    try:
        client = _lf_get_client()
        tid = client.get_current_trace_id()
        if tid:
            return str(tid)
    except Exception:  # noqa: BLE001 — best-effort, never raise
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
