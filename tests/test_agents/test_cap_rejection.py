"""Unit tests for the cap-box rejection keyword detector.

`is_rejection` is a normalized contains-check against a curated KO/EN phrase
set, with empty/None and >80-char guards returning False.
"""

from __future__ import annotations

import pytest

from app.channels.cap_rejection import is_rejection


@pytest.mark.parametrize(
    "text",
    [
        "안 써",
        "안써",
        "관심 없어",
        "필요없어",
        "돈 안 내",
        "싫어",
        "그만",
        "no thanks",
        "not interested",
        "won't pay",
        "stop",
        "leave me alone",
    ],
)
def test_rejection_phrases_detected(text):
    assert is_rejection(text) is True


def test_phrase_embedded_in_sentence_detected():
    assert is_rejection("아 진짜 관심 없어 그만해") is True


def test_case_insensitive_and_whitespace_collapsed():
    assert is_rejection("  NO   THANKS  ") is True


@pytest.mark.parametrize("text", [None, ""])
def test_empty_or_none_returns_false(text):
    assert is_rejection(text) is False


def test_whitespace_only_returns_false():
    assert is_rejection("    ") is False


def test_overly_long_message_returns_false():
    # >80 chars: almost certainly a real query, not a one-liner refusal.
    long_text = "no thanks " + "x" * 80
    assert len(long_text) > 80
    assert is_rejection(long_text) is False


def test_normal_query_not_flagged():
    assert is_rejection("그레이 반팔 티셔츠 추천해줘") is False
    assert is_rejection("find me a denim jacket") is False
