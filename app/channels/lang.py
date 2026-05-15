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

# SPEC-AGENT-V2-REACT §15 Decision 5 — a single whitespace-delimited token is
# "URL-like" if it is an http(s):// URL, a www. host, or a bare pin.it /
# pinterest.com style short link. A link is not a language signal.
_URL_TOKEN_RE = re.compile(
    r"^(?:https?://|www\.|(?:[\w-]+\.)*(?:pin\.it|pinterest\.com)/)",
    re.IGNORECASE,
)

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
    """Update `sess.lang` from `text` if `text` carries a meaningful language signal.

    Sticky-preserve cases (do NOT overwrite session lang):
      - command-like prefix (`/start`, `/reset`, ...): commands aren't language
      - very short text (< 3 chars) without Hangul: e.g. "ㅇㅇ", "ok"
      - pure punctuation / digits

    Returns the resolved language: detected from text when meaningful,
    otherwise the previously remembered session language (default 'en').
    """
    prior = getattr(sess, "lang", None) or LANG_EN
    if not text or not text.strip():
        return prior
    stripped = text.strip()
    # Commands never carry a language signal — preserve sticky.
    if stripped.startswith("/"):
        return prior
    # Short text without Hangul → preserve sticky (avoid "ok"/"hi"/"음" 등으로 영구 전환).
    if len(stripped) < 3 and not _HANGUL_RE.search(stripped):
        return prior
    # URL-only / link-only input → not a language signal, preserve sticky.
    # A Korean user dropping a Pinterest URL must keep replying in Korean
    # (exactly like the `/`-command guard). Mixed input (e.g. "이거 봐 https://...")
    # has a non-URL token → falls through to detect_lang as before.
    tokens = stripped.split()
    if tokens and all(_URL_TOKEN_RE.match(t) for t in tokens):
        return prior
    lang = detect_lang(stripped)
    try:
        setattr(sess, "lang", lang)
    except Exception:  # noqa: BLE001
        pass
    return lang


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
