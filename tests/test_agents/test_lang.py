"""Unit tests for the sticky language-detection helpers.

Pure rule set: Hangul present → 'ko' else 'en', with explicit-switch overrides
and sticky-preserve guards (commands / short non-Hangul / URL-only). DEMO_MODE
forces 'ko'. `settings.DEMO_MODE` is pinned False per test (then True for the
override cases) so the default does not depend on the ambient env.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.channels import lang
from app.channels.lang import (
    LANG_EN,
    LANG_KO,
    detect_explicit_switch,
    detect_lang,
    remember_lang,
    session_lang,
)
from app.core.config import settings


@pytest.fixture(autouse=True)
def _demo_off(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)


def _sess(language=LANG_EN):
    return SimpleNamespace(lang=language)


# ── detect_explicit_switch ──────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["영어로 말해줘", "in English please", "switch to english", "speak english"])
def test_explicit_switch_to_en(text):
    assert detect_explicit_switch(text) == LANG_EN


@pytest.mark.parametrize("text", ["한국어로 답해줘", "한글로", "reply in korean", "korean please"])
def test_explicit_switch_to_ko(text):
    assert detect_explicit_switch(text) == LANG_KO


def test_explicit_switch_none_for_casual_mention():
    assert detect_explicit_switch("영어 노래 추천") is None


@pytest.mark.parametrize("text", [None, "", "   "])
def test_explicit_switch_empty(text):
    assert detect_explicit_switch(text) is None


def test_explicit_switch_en_wins_when_both_present():
    # "english로 답해줘" matches the EN regex; EN is checked first.
    assert detect_explicit_switch("english로 답해줘") == LANG_EN


# ── detect_lang ─────────────────────────────────────────────────────────────


def test_detect_lang_hangul_is_ko():
    assert detect_lang("그레이 셔츠") == LANG_KO


def test_detect_lang_ascii_is_en():
    assert detect_lang("grey shirt") == LANG_EN


def test_detect_lang_empty_is_en():
    assert detect_lang("") == LANG_EN
    assert detect_lang(None) == LANG_EN


def test_detect_lang_demo_mode_forces_ko(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    assert detect_lang("grey shirt") == LANG_KO


# ── remember_lang ───────────────────────────────────────────────────────────


def test_remember_lang_sets_ko_from_hangul():
    sess = _sess()
    assert remember_lang(sess, "그레이 셔츠 찾아줘") == LANG_KO
    assert sess.lang == LANG_KO


def test_remember_lang_sets_en_from_ascii():
    sess = _sess(LANG_KO)
    assert remember_lang(sess, "find a grey shirt") == LANG_EN
    assert sess.lang == LANG_EN


def test_remember_lang_preserves_for_command():
    sess = _sess(LANG_KO)
    assert remember_lang(sess, "/start") == LANG_KO
    assert sess.lang == LANG_KO


def test_remember_lang_preserves_for_short_non_hangul():
    sess = _sess(LANG_KO)
    assert remember_lang(sess, "ok") == LANG_KO


def test_remember_lang_preserves_for_url_only():
    sess = _sess(LANG_KO)
    assert remember_lang(sess, "https://pin.it/abc123") == LANG_KO
    assert sess.lang == LANG_KO


def test_remember_lang_mixed_text_with_url_detects():
    sess = _sess(LANG_KO)
    # has a non-URL token → falls through to detect_lang (ascii → en).
    assert remember_lang(sess, "look at this https://pin.it/abc") == LANG_EN


def test_remember_lang_explicit_switch_overrides_hangul():
    sess = _sess(LANG_KO)
    # Hangul-positive but the intent is to flip to EN.
    assert remember_lang(sess, "영어로 말해줘") == LANG_EN
    assert sess.lang == LANG_EN


def test_remember_lang_empty_returns_prior():
    sess = _sess(LANG_KO)
    assert remember_lang(sess, "") == LANG_KO
    assert remember_lang(sess, "   ") == LANG_KO


def test_remember_lang_default_prior_when_no_lang_attr():
    sess = SimpleNamespace()  # no .lang
    assert remember_lang(sess, "/reset") == LANG_EN


# ── session_lang ────────────────────────────────────────────────────────────


def test_session_lang_reads_sticky():
    assert session_lang(_sess(LANG_KO)) == LANG_KO


def test_session_lang_none_is_en():
    assert session_lang(None) == LANG_EN


def test_session_lang_missing_attr_is_en():
    assert session_lang(SimpleNamespace()) == LANG_EN


def test_session_lang_demo_mode_forces_ko(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    assert session_lang(_sess(LANG_EN)) == LANG_KO


def test_module_constants():
    assert lang.LANG_KO == "ko"
    assert lang.LANG_EN == "en"
