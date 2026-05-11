"""REQ-OBS-COST-002 — LANGFUSE_SELECTIVE_MODE flag exists."""

from __future__ import annotations


def test_selective_mode_setting_exists() -> None:
    from app.core.config import settings

    assert hasattr(settings, "LANGFUSE_SELECTIVE_MODE")
    assert isinstance(settings.LANGFUSE_SELECTIVE_MODE, bool)
    # Default off.
    assert settings.LANGFUSE_SELECTIVE_MODE is False
