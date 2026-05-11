"""REQ-OBS-DECORATOR-001 — `observe` re-exported from project module is v3 path."""

from __future__ import annotations

import pytest


def test_module_exposes_observe_callable() -> None:
    from app.observability import langfuse as obs

    assert callable(obs.observe)


@pytest.mark.asyncio
async def test_observe_noop_passthrough_preserves_return_value() -> None:
    """REQ-OBS-DECORATOR-001 — wrapped function must return identical value
    to the undecorated form (R13 regression guard)."""
    from app.observability.langfuse import observe

    @observe(name="test.passthrough", as_type="span")
    async def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    result = await add(2, 3)
    assert result == {"sum": 5}


@pytest.mark.asyncio
async def test_observe_supports_three_call_shapes() -> None:
    """Acceptance: `@observe(name=..., as_type=...)`, `@observe(name=...)`, `@observe()` all work."""
    from app.observability.langfuse import observe

    @observe(name="x", as_type="span")
    async def f1():
        return 1

    @observe(name="y")
    async def f2():
        return 2

    @observe()
    async def f3():
        return 3

    assert await f1() == 1
    assert await f2() == 2
    assert await f3() == 3
