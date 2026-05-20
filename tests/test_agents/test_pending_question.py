"""Pending-question carry-across-turn — unit tests.

Covers:
- set/peek/pop/clear semantics in app.agents.pending_question
- chat_id coercion / invalid inputs no-op safely
- truncation caps
- _is_short_affirmative classifier in react_loop
"""

from __future__ import annotations

import pytest

from app.agents import pending_question as pq
from app.agents.react_loop import _is_short_affirmative


@pytest.fixture(autouse=True)
def _reset_store():
    pq._reset_all_for_tests()
    yield
    pq._reset_all_for_tests()


# ─── pending_question store ────────────────────────────────────────────────


def test_set_and_peek():
    pq.set_pending(42, "단순한 거 찾는 거야?", "grey t-shirt")
    assert pq.peek_pending(42) == ("단순한 거 찾는 거야?", "grey t-shirt")


def test_peek_missing_returns_none_tuple():
    assert pq.peek_pending(999) == (None, None)


def test_pop_removes_entry():
    pq.set_pending(42, "Q?", "intent")
    assert pq.pop_pending(42) == ("Q?", "intent")
    assert pq.peek_pending(42) == (None, None)


def test_clear_pending():
    pq.set_pending(42, "Q?", "intent")
    pq.clear_pending(42)
    assert pq.peek_pending(42) == (None, None)


def test_set_with_no_intent_keeps_question():
    pq.set_pending(42, "Q?", None)
    assert pq.peek_pending(42) == ("Q?", None)


def test_set_empty_question_noop():
    pq.set_pending(42, "", "intent")
    assert pq.peek_pending(42) == (None, None)


def test_set_whitespace_only_question_noop():
    pq.set_pending(42, "   ", "intent")
    assert pq.peek_pending(42) == (None, None)


def test_invalid_chat_id_noop():
    pq.set_pending(None, "Q?", "intent")  # type: ignore[arg-type]
    pq.set_pending("not-a-number", "Q?", "intent")  # type: ignore[arg-type]
    assert pq.peek_pending(None) == (None, None)  # type: ignore[arg-type]


def test_truncation_caps_at_240_chars():
    long_q = "Q" * 500
    long_intent = "I" * 500
    pq.set_pending(42, long_q, long_intent)
    q, i = pq.peek_pending(42)
    assert q is not None and len(q) <= 240
    assert i is not None and len(i) <= 240


def test_isolated_chat_ids():
    pq.set_pending(1, "Q1?", "i1")
    pq.set_pending(2, "Q2?", "i2")
    assert pq.peek_pending(1) == ("Q1?", "i1")
    assert pq.peek_pending(2) == ("Q2?", "i2")


# ─── _is_short_affirmative ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "어",
        "응",
        "네",
        "맞아",
        "그래",
        "좋아",
        "오케이",
        "ㅇㅇ",
        "ㅇㅋ",
        "yes",
        "Yes",
        "yeah",
        "ok",
        "OK",
        "sure",
        "아니",
        "no",
        "nope",
        "어.",
        "응!",
        "yes!",
        "ok?",  # punctuation tolerant
    ],
)
def test_classifier_accepts_affirmative_tokens(text):
    assert _is_short_affirmative(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "그레이 반팔 티셔츠 찾아줘",
        "find me a denim jacket please",
        "어떤 브랜드가 좋아",
        "yes please find one",  # too long
        "I want a t-shirt",
        "",
        "   ",
        None,
    ],
)
def test_classifier_rejects_long_or_empty(text):
    assert _is_short_affirmative(text) is False
