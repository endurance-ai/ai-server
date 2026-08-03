"""REQ-FB-FALLBACK-001 — in-memory backend silent skip + Postgres errors WARN+continue."""

from __future__ import annotations

import logging

import pytest


@pytest.mark.asyncio
async def test_log_impressions_in_memory_skip(caplog):
    from app.channels import implicit_feedback as ifb

    ifb.set_backend_is_postgres(False)
    candidates = [object()]  # any iterable
    with caplog.at_level(logging.WARNING):
        n = await ifb.log_impressions(1234, None, candidates)
    assert n == 0
    # No WARN logs in fallback path
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING and "IMPLICIT_FB" in r.getMessage()]
    assert warns == []


@pytest.mark.asyncio
async def test_record_click_in_memory_skip(in_memory_taste_store):
    from app.channels import implicit_feedback as ifb

    ifb.set_backend_is_postgres(False)
    rows = await ifb.record_click(1, None, "p1", "ami", ["oversized"])
    assert rows == 0
    # Taste profile reinforcement still runs (intent is real)
    profile = in_memory_taste_store.get_or_create("c:1")
    assert "ami" in profile.liked_brands


@pytest.mark.asyncio
async def test_attribute_expired_in_memory_skip():
    from app.channels import implicit_feedback as ifb

    ifb.set_backend_is_postgres(False)
    n = await ifb.attribute_expired_impressions(99, None)
    assert n == 0


@pytest.mark.asyncio
async def test_record_click_stale_returns_zero(in_memory_taste_store):
    from app.channels import implicit_feedback as ifb

    ifb.set_backend_is_postgres(False)
    rows = await ifb.record_click(1, None, "missing", "brand", [], stale=True)
    assert rows == 0


@pytest.mark.asyncio
async def test_detect_requery_no_state_no_trigger():
    from app.channels import implicit_feedback as ifb
    from app.infrastructure.memory.session import Session, SessionState

    sess = Session(chat_id=1, state=SessionState.IDLE)
    triggered = await ifb.detect_and_apply_re_query(sess, inbound_is_fresh_query=True)
    assert triggered is False
