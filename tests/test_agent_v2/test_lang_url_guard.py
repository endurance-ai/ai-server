"""SPEC-AGENT-V2-REACT / T-007.5 §15 Decision 5 — remember_lang URL-only guard.

A Pinterest URL is not a language signal. URL-only input must preserve the
sticky language (like the `/`-command guard). Mixed text+URL still detects.
Normal KO/EN detection must NOT regress.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.channels.lang import remember_lang


@dataclass
class _Sess:
    lang: str = "en"


@pytest.mark.parametrize(
    ("prior", "text", "expected"),
    [
        # URL-only — sticky preserved (the regression this fixes).
        ("ko", "https://pin.it/abc123", "ko"),
        ("ko", "https://www.pinterest.com/user/board/", "ko"),
        ("en", "https://www.pinterest.com/board/", "en"),
        ("ko", "pin.it/xY9", "ko"),
        ("ko", "www.pinterest.com/x", "ko"),
        ("ko", "https://pin.it/a https://pin.it/b", "ko"),  # multiple URLs only
        # Mixed text + URL — Hangul present → detect ko (no regression).
        ("en", "이거랑 비슷한 거 https://pin.it/x", "ko"),
        ("ko", "find me this https://pin.it/x", "en"),  # English words → en
        # Normal text — detection unchanged.
        ("en", "casual blazer", "en"),
        ("en", "캐주얼 블레이저", "ko"),
        # `/`-command guard still works.
        ("ko", "/start", "ko"),
        ("en", "/reset", "en"),
    ],
)
def test_remember_lang_url_guard(prior, text, expected):
    sess = _Sess(lang=prior)
    assert remember_lang(sess, text) == expected


def test_url_only_does_not_mutate_session_lang():
    sess = _Sess(lang="ko")
    remember_lang(sess, "https://pin.it/abc123")
    assert sess.lang == "ko"  # untouched


def test_normal_text_still_mutates_session_lang():
    sess = _Sess(lang="en")
    remember_lang(sess, "캐주얼한 블레이저 찾아줘")
    assert sess.lang == "ko"
