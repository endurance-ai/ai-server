"""Unit tests for the in-process pending-gender and last-query stores.

SPEC-GENDER-PIN-001 (+ follow-up). Both are process-local dicts keyed by
chat_id with set / pop / clear semantics and tolerant chat_id coercion.
"""

from __future__ import annotations

import pytest

from app.agents import last_query as lq
from app.agents import pending_gender as pg


@pytest.fixture(autouse=True)
def _reset_stores():
    pg._reset_all_for_tests()
    lq._reset_all_for_tests()
    yield
    pg._reset_all_for_tests()
    lq._reset_all_for_tests()


# ── pending_gender ──────────────────────────────────────────────────────────


def test_set_then_pop_returns_copy():
    args = {"text_query": "dress", "top_k": 15}
    pg.set_pending(42, args)
    popped = pg.pop_pending(42)
    assert popped == args
    # stored a copy — mutating the original does not affect the stash.
    assert popped is not args
    # pop consumes — second pop is None.
    assert pg.pop_pending(42) is None


def test_pop_missing_returns_none():
    assert pg.pop_pending(999) is None


def test_clear_removes_pending():
    pg.set_pending(42, {"text_query": "x"})
    pg.clear_pending(42)
    assert pg.pop_pending(42) is None


def test_set_non_dict_args_noop():
    pg.set_pending(42, "not-a-dict")  # type: ignore[arg-type]
    assert pg.pop_pending(42) is None


@pytest.mark.parametrize("bad", [None, "abc", object()])
def test_invalid_chat_id_noop(bad):
    pg.set_pending(bad, {"text_query": "x"})  # type: ignore[arg-type]
    assert pg.pop_pending(bad) is None
    # clear on invalid id is also a harmless no-op.
    pg.clear_pending(bad)


def test_chat_id_string_digits_coerced():
    pg.set_pending("42", {"text_query": "x"})
    # integer and digit-string keys resolve to the same slot.
    assert pg.pop_pending(42) == {"text_query": "x"}


def test_pending_isolated_by_chat_id():
    pg.set_pending(1, {"text_query": "a"})
    pg.set_pending(2, {"text_query": "b"})
    assert pg.pop_pending(1) == {"text_query": "a"}
    assert pg.pop_pending(2) == {"text_query": "b"}


# ── last_query ──────────────────────────────────────────────────────────────


def test_set_and_get_last_query():
    lq.set_last_query(42, "grey floral lace dress women")
    assert lq.get_last_query(42) == "grey floral lace dress women"


def test_get_missing_returns_none():
    assert lq.get_last_query(999) is None


def test_set_strips_and_truncates():
    lq.set_last_query(42, "  padded  ")
    assert lq.get_last_query(42) == "padded"
    lq.set_last_query(7, "q" * 500)
    assert len(lq.get_last_query(7)) == 240


def test_set_empty_or_whitespace_noop():
    lq.set_last_query(42, "")
    lq.set_last_query(42, "   ")
    assert lq.get_last_query(42) is None


def test_clear_last_query():
    lq.set_last_query(42, "dress")
    lq.clear_last_query(42)
    assert lq.get_last_query(42) is None


@pytest.mark.parametrize("bad", [None, "xyz"])
def test_last_query_invalid_chat_id_noop(bad):
    lq.set_last_query(bad, "dress")  # type: ignore[arg-type]
    assert lq.get_last_query(bad) is None  # type: ignore[arg-type]
    lq.clear_last_query(bad)  # type: ignore[arg-type]
