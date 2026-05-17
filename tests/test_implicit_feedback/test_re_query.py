"""REQ-FB-REQUERY-001 — rapid re-query soft-negative."""

from __future__ import annotations

import time

import pytest


def _make_session_results_sent(last_active_offset_s: float, results: list):
    from app.infrastructure.memory.session import Session, SessionState

    return Session(
        chat_id=31,
        from_user_id=999,
        state=SessionState.RESULTS_SENT,
        last_results=list(results),
        last_active=time.time() - last_active_offset_s,
    )


@pytest.mark.asyncio
async def test_re_query_triggers_within_window(fake_candidates, in_memory_taste_store):
    from app.channels.implicit_feedback import detect_and_apply_re_query

    sess = _make_session_results_sent(30, fake_candidates)
    triggered = await detect_and_apply_re_query(sess, inbound_is_fresh_query=True)
    assert triggered is True

    profile = in_memory_taste_store.get_or_create("u:999")
    # all 3 brands should be disliked-reinforced
    assert "ami" in profile.disliked_brands
    assert "acne" in profile.disliked_brands
    assert "maison" in profile.disliked_brands


@pytest.mark.asyncio
async def test_re_query_outside_window_no_trigger(fake_candidates, in_memory_taste_store):
    from app.channels.implicit_feedback import detect_and_apply_re_query

    sess = _make_session_results_sent(120, fake_candidates)
    triggered = await detect_and_apply_re_query(sess, inbound_is_fresh_query=True)
    assert triggered is False
    profile = in_memory_taste_store.get_or_create("u:999")
    assert profile.disliked_brands == {}


@pytest.mark.asyncio
async def test_re_query_empty_results_no_trigger(in_memory_taste_store):
    from app.channels.implicit_feedback import detect_and_apply_re_query

    sess = _make_session_results_sent(10, [])
    triggered = await detect_and_apply_re_query(sess, inbound_is_fresh_query=True)
    assert triggered is False


@pytest.mark.asyncio
async def test_re_query_non_fresh_query_no_trigger(fake_candidates, in_memory_taste_store):
    from app.channels.implicit_feedback import detect_and_apply_re_query

    sess = _make_session_results_sent(10, fake_candidates)
    triggered = await detect_and_apply_re_query(sess, inbound_is_fresh_query=False)
    assert triggered is False


@pytest.mark.asyncio
async def test_re_query_idle_state_no_trigger(fake_candidates, in_memory_taste_store):
    from app.channels.implicit_feedback import detect_and_apply_re_query
    from app.infrastructure.memory.session import Session, SessionState

    sess = Session(
        chat_id=1,
        state=SessionState.IDLE,
        last_results=list(fake_candidates),
        last_active=time.time(),
    )
    triggered = await detect_and_apply_re_query(sess, inbound_is_fresh_query=True)
    assert triggered is False
