"""Lightweight language detection + session-sticky helpers.

The bot mirrors the user's language. We use a deliberately tiny rule:
- any Hangul syllable present → 'ko'
- otherwise → 'en'

This is sufficient for the kiko.ai audience (KR / EN). Detection is sticky
on the session: once a user types Korean, follow-up button taps (which carry
no text) keep responding in Korean until the user switches back.
"""

from __future__ import annotations

import re
from typing import Any

_HANGUL_RE = re.compile(r"[가-힣]")

LANG_KO = "ko"
LANG_EN = "en"


def detect_lang(text: str | None) -> str:
    """Return 'ko' if any Hangul syllable is present, else 'en'.

    DEMO_MODE override — always force KO for the video shoot.
    """
    from app.core.config import settings

    if settings.DEMO_MODE:
        return LANG_KO
    if not text:
        return LANG_EN
    return LANG_KO if _HANGUL_RE.search(text) else LANG_EN


def remember_lang(sess: Any, text: str | None) -> str:
    """Update `sess.lang` from `text` if `text` is non-empty.

    Returns the resolved language: detected from text when present,
    otherwise the previously remembered session language (default 'en').
    Callers that want to *write* the change back to the store must call
    `get_store().update(sess)` themselves — this helper is store-agnostic.
    """
    if text and text.strip():
        lang = detect_lang(text)
        try:
            setattr(sess, "lang", lang)
        except Exception:  # noqa: BLE001
            pass
        return lang
    return getattr(sess, "lang", None) or LANG_EN


def session_lang(sess: Any | None) -> str:
    """Read sticky language from session, defaulting to 'en'.

    DEMO_MODE override — always force KO for the video shoot.
    """
    from app.core.config import settings

    if settings.DEMO_MODE:
        return LANG_KO
    if sess is None:
        return LANG_EN
    return getattr(sess, "lang", None) or LANG_EN
