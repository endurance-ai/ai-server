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
