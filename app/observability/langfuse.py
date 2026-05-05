"""Langfuse 트레이싱.

LANGFUSE_PUBLIC_KEY/SECRET_KEY가 비어있거나 langfuse 라이브러리 import 실패 시 no-op 폴백.
v2(`langfuse.decorators.observe`) / v3(`langfuse.observe`) 양쪽 호환.
"""

import os
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.config import settings

# langfuse SDK가 os.environ을 직접 읽기 때문에 settings 값을 환경변수로 미리 주입한다.
for _k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
    _v = getattr(settings, _k, "")
    if _v and not os.environ.get(_k):
        os.environ[_k] = str(_v)

P = ParamSpec("P")
R = TypeVar("R")

_ENABLED = bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)

# v3 → v2 → no-op 순서로 폴백. import 실패해도 앱은 떠야 함.
_lf_observe: Callable[..., Any] | None = None
if _ENABLED:
    try:
        # langfuse v3+ : `from langfuse import observe`
        from langfuse import observe as _lf_observe_v3  # type: ignore[import-not-found]

        _lf_observe = _lf_observe_v3
    except ImportError:
        try:
            # langfuse v2 : `from langfuse.decorators import observe`
            from langfuse.decorators import observe as _lf_observe_v2  # type: ignore[import-not-found]

            _lf_observe = _lf_observe_v2
        except ImportError:
            _lf_observe = None


if _lf_observe is not None:

    def observe(
        name: str | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        return _lf_observe(name=name, **kwargs)  # type: ignore[no-any-return,misc]
else:

    def observe(
        name: str | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """No-op decorator (Langfuse 비활성 또는 import 실패)."""

        def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            @wraps(fn)
            async def wrapper(*args: P.args, **kw: P.kwargs) -> R:
                return await fn(*args, **kw)

            return wrapper

        return decorator


# ── SPEC-AGENT-001 / REQ-OBSV-002 ──────────────────────────────────────────
# Optional Langfuse `CallbackHandler` factory for nested LLM tracing inside
# LangGraph nodes (`respond`, `ask_clarify`).
#
# Important compatibility note: Langfuse 2.x's `langfuse.callback.CallbackHandler`
# imports `from langchain.callbacks.base import BaseCallbackHandler`, which only
# exists on langchain < 1.0. Once langchain is bumped to 1.x (required by
# langgraph 1.x via langchain-core 1.3+), the legacy import path goes away and
# the handler can no longer be constructed against this Langfuse major version.
#
# The function below tries to import the handler and returns `None` on any
# import / construction failure. The graph's RunnableConfig.callbacks list
# stays empty in that case — REQ-OBSV-002 acceptance #4 explicitly allows the
# no-op fallback. The trace tree still records the parent `@observe` span and
# the LiteLLM dashboard remains authoritative for cost (REQ-OBSV-004).


def build_callback_handler(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    """Return a Langfuse `CallbackHandler` for the current webhook, or `None`
    when keys are absent OR when the host langchain version no longer exports
    the legacy callback base class. Callers pass the result into
    `RunnableConfig.callbacks` — `None` falls through to no-op nicely.
    """
    if not _ENABLED:
        return None
    try:
        from langfuse.callback import CallbackHandler  # type: ignore[import-not-found]
    except Exception:
        # langchain 1.x removed `langchain.callbacks.base.BaseCallbackHandler`
        # which langfuse v2's CallbackHandler depends on. Until the host
        # bumps to langfuse v3 (server-side incompatible) or langchain
        # restores the legacy module, the nested-tracing path is no-op.
        return None
    try:
        return CallbackHandler(session_id=session_id, user_id=user_id, metadata=metadata or {})
    except Exception:
        return None
