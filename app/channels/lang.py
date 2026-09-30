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

# 한국어 세션을 영어로 바꾸려면 '영어 문장'이어야 한다 — 기능어(대명사·조동사·전치사 등)가
# 하나 이상 있고 단어가 2개 이상. 브랜드명("Auralee")·품목어("y2k top", "black oversized
# hoodie")는 한국어 유저도 영문으로 치므로 언어 신호가 아니다(9월 실유저 3턴이 이렇게
# 영어로 전환됨).
_EN_FUNCTION_WORDS = frozenset(
    "i me my you your we us it this that these those the a an is are am was were be do does did "
    "can could would will should please show find give want need looking look for with and or "
    "what which how where any some something anything help thanks thank hi hello hey".split()
)
_LATIN_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'-]*")
# 공유 시트가 링크에 자동으로 붙이는 문구 — 유저가 쓴 말이 아니다(9월 "https://pin.it/… Take a
# look! 📌" 로 한국어 세션이 영어로 바뀜).
_SHARE_BOILERPLATE_RE = re.compile(
    r"take a look!?|check (?:this|it) out!?|check out this pin!?|look what i found(?: on pinterest)?!?|"
    r"found (?:this|it) on pinterest!?|saw this on pinterest!?",
    re.IGNORECASE,
)


def _looks_like_english_sentence(text: str) -> bool:
    words = [w.lower() for w in _LATIN_WORD_RE.findall(text)]
    return len(words) >= 2 and any(w in _EN_FUNCTION_WORDS for w in words)


# Explicit language-switch triggers. Beats the Hangul-detection default so a
# Korean sentence asking for English ("영어로 말해줘") flips the sticky lang to
# EN instead of staying KO just because the request contains Hangul. Patterns
# are intentionally narrow — only direct switch requests, not casual mentions
# ("영어 노래 추천" must NOT switch). Matched case-insensitively on the
# lowercased stripped text.
_LANG_SWITCH_EN_RE = re.compile(
    r"(?:영어로|영문으로|english로|영어\s*로|switch\s+to\s+english|"
    r"in\s+english|reply\s+in\s+english|answer\s+in\s+english|speak\s+english|"
    r"english\s+please)",
    re.IGNORECASE,
)
_LANG_SWITCH_KO_RE = re.compile(
    r"(?:한국어로|한글로|한국말로|국문으로|switch\s+to\s+korean|"
    r"in\s+korean|reply\s+in\s+korean|answer\s+in\s+korean|speak\s+korean|"
    r"korean\s+please)",
    re.IGNORECASE,
)


def detect_explicit_switch(text: str | None) -> str | None:
    """Return 'en'/'ko' if the text contains an explicit language-switch
    request, else None. Used as a sticky-lang override before Hangul detection.
    """
    if not text:
        return None
    s = text.strip().lower()
    if not s:
        return None
    # KO check first — Korean trigger phrases often coexist with the word
    # "english" inside them (e.g. "english로 답해줘"), so the more specific
    # English-switch regex wins by being checked first when both match.
    if _LANG_SWITCH_EN_RE.search(s):
        return LANG_EN
    if _LANG_SWITCH_KO_RE.search(s):
        return LANG_KO
    return None


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
    # Explicit language-switch request wins over Hangul-based detection. The
    # canonical case: a Korean user typing "영어로 말해줘" — the sentence is
    # Hangul-positive (would detect as KO) but the INTENT is to flip to EN.
    explicit = detect_explicit_switch(stripped)
    if explicit is not None:
        try:
            setattr(sess, "lang", explicit)
        except Exception:  # noqa: BLE001
            pass
        return explicit
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
    # 한국어 세션 → 영어 전환은 영어 문장일 때만. 링크·공유 시트 문구("Take a look! 📌")는
    # 유저가 쓴 말이 아니고, 브랜드·품목어만 친 메시지도 신호가 아니다.
    if prior == LANG_KO and lang == LANG_EN:
        rest = _SHARE_BOILERPLATE_RE.sub(" ", " ".join(t for t in tokens if not _URL_TOKEN_RE.match(t)))
        if not _looks_like_english_sentence(rest):
            return prior
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
