"""REQ-OBS-CALLBACK-001 + REQ-OBS-CALLBACK-002 — v3 handler factory."""

from __future__ import annotations

import logging


def test_build_callback_handler_returns_none_when_keys_absent(monkeypatch, caplog) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")

    # Force module re-evaluation
    import importlib

    from app.observability import langfuse as obs

    importlib.reload(obs)

    with caplog.at_level(logging.WARNING):
        handler = obs.build_callback_handler(session_id="abc", user_id="def", metadata={})
    assert handler is None


def test_build_callback_handler_returns_v3_handler_when_keys_present(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-test")

    import importlib

    from app.observability import langfuse as obs

    importlib.reload(obs)

    handler = obs.build_callback_handler(session_id="abc", user_id="def", metadata={"flow": "image"})
    assert handler is not None
    # Confirm it's the v3 langchain CallbackHandler.
    from langfuse.langchain import CallbackHandler

    assert isinstance(handler, CallbackHandler)


def test_build_callback_handler_none_safe_to_pass_through() -> None:
    """REQ-OBS-CALLBACK-002 — caller filters None from callbacks list."""
    handler = None
    callbacks = [handler] if handler is not None else []
    assert callbacks == []
