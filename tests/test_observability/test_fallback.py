"""REQ-OBS-FALLBACK-001 + REQ-OBS-FALLBACK-002 — degrade gracefully."""

from __future__ import annotations

import importlib

import pytest


def test_module_imports_with_empty_keys(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")

    from app.observability import langfuse as obs

    importlib.reload(obs)

    assert obs.build_callback_handler(session_id="x", user_id="y", metadata={}) is None


@pytest.mark.asyncio
async def test_observe_noop_when_disabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")

    from app.observability import langfuse as obs

    importlib.reload(obs)

    @obs.observe(name="x")
    async def f():
        return 42

    assert await f() == 42


def test_no_per_webhook_error_log_for_host_failure(caplog) -> None:
    """REQ-OBS-FALLBACK-002 — observability module itself must not log ERROR per webhook.

    Structural: scan `app/observability/langfuse.py` source — it must NOT contain
    any `logger.error(` call inside a function that is called per webhook.
    """
    import inspect

    from app.observability import langfuse as obs

    src = inspect.getsource(obs)
    # build_callback_handler may log WARNING once at startup, but not ERROR per call.
    # Just ensure no `.error(` is dispatched from within `build_callback_handler` body.
    lines = src.split("\n")
    in_fn = False
    for line in lines:
        if line.startswith("def build_callback_handler"):
            in_fn = True
            continue
        if in_fn and line and not line.startswith((" ", "\t")):
            in_fn = False
        if in_fn and ".error(" in line:
            pytest.fail(f"build_callback_handler emits ERROR log: {line!r}")
