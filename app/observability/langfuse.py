"""Langfuse 트레이싱.

LANGFUSE_PUBLIC_KEY/SECRET_KEY가 비어있거나 langfuse 라이브러리 import 실패 시 no-op 폴백.
v2(`langfuse.decorators.observe`) / v3(`langfuse.observe`) 양쪽 호환.
"""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.config import settings

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
