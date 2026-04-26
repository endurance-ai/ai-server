"""Langfuse 트레이싱.

LANGFUSE_PUBLIC_KEY/SECRET_KEY가 비어있으면 no-op (로컬 개발/테스트 안전).
"""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.config import settings

P = ParamSpec("P")
R = TypeVar("R")

_ENABLED = bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)


if _ENABLED:
    from langfuse.decorators import observe as _lf_observe  # type: ignore[import-not-found]

    def observe(
        name: str | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        return _lf_observe(name=name, **kwargs)  # type: ignore[no-any-return]
else:

    def observe(
        name: str | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """No-op decorator (Langfuse 비활성)."""

        def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            @wraps(fn)
            async def wrapper(*args: P.args, **kw: P.kwargs) -> R:
                return await fn(*args, **kw)

            return wrapper

        return decorator
