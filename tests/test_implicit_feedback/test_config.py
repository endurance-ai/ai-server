"""REQ-FB-CONFIG-001 — env var declarations + defaults."""

from __future__ import annotations


def test_attribution_window_default():
    from app.core.config import settings

    assert settings.IMPLICIT_FB_ATTRIBUTION_WINDOW_S == 600


def test_requery_window_default():
    from app.core.config import settings

    assert settings.IMPLICIT_FB_REQUERY_WINDOW_S == 90


def test_weight_defaults():
    from app.core.config import settings

    assert settings.IMPLICIT_FB_CLICK_WEIGHT == 1.0
    assert settings.IMPLICIT_FB_REQUERY_WEIGHT == 0.5
    assert settings.IMPLICIT_FB_NOCLICK_WEIGHT == 0.2


def test_weight_invariant_default_documented():
    """Informative convention: CLICK > REQUERY > NOCLICK."""
    from app.core.config import settings

    assert settings.IMPLICIT_FB_CLICK_WEIGHT > settings.IMPLICIT_FB_REQUERY_WEIGHT > settings.IMPLICIT_FB_NOCLICK_WEIGHT


def test_env_example_documents_all_five_vars():
    import pathlib

    env_text = pathlib.Path(__file__).parent.parent.parent.joinpath(".env.example").read_text()
    for v in (
        "IMPLICIT_FB_ATTRIBUTION_WINDOW_S",
        "IMPLICIT_FB_REQUERY_WINDOW_S",
        "IMPLICIT_FB_NOCLICK_WEIGHT",
        "IMPLICIT_FB_CLICK_WEIGHT",
        "IMPLICIT_FB_REQUERY_WEIGHT",
    ):
        assert v in env_text, f"missing {v} in .env.example"
