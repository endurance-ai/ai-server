"""REQ-FB-CLICK-001 + REQ-FB-UX-001 click capture branch."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_record_click_updates_row_and_reinforces_taste(
    pg_pool_with_impressions, fake_candidates, in_memory_taste_store
):
    from app.channels.implicit_feedback import log_impressions, record_click
    from app.providers.db_pool import get_pool, run_in_pool_loop

    await log_impressions(11, None, fake_candidates)

    rows = await record_click(11, None, "p2", "acne", ["wide", "indigo"])
    assert rows == 1

    async def _row():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT click_status, click_at FROM ai.card_impression WHERE chat_id=%s AND product_id=%s",
                (11, "p2"),
            )
            return await cur.fetchone()

    status, click_at = run_in_pool_loop(_row())
    assert status == "clicked"
    assert click_at is not None

    profile = in_memory_taste_store.get_or_create("c:11")
    assert "acne" in profile.liked_brands
    assert "wide" in profile.liked_keywords


@pytest.mark.asyncio
async def test_record_click_idempotent_on_double_tap(pg_pool_with_impressions, fake_candidates, in_memory_taste_store):
    from app.channels.implicit_feedback import log_impressions, record_click

    await log_impressions(12, None, fake_candidates)
    first = await record_click(12, None, "p1", "ami", ["oversized"])
    assert first == 1
    second = await record_click(12, None, "p1", "ami", ["oversized"])
    # second UPDATE finds 0 rows (already clicked)
    assert second == 0


@pytest.mark.asyncio
async def test_stale_click_no_db_no_taste(pg_pool_with_impressions, in_memory_taste_store):
    from app.channels.implicit_feedback import record_click
    from app.providers.db_pool import get_pool, run_in_pool_loop

    n = await record_click(13, None, "nonexistent", "", [], stale=True)
    assert n == 0

    async def _count():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM ai.card_impression WHERE chat_id=%s", (13,))
            return (await cur.fetchone())[0]

    assert run_in_pool_loop(_count()) == 0


def test_send_results_builds_three_critique_buttons():
    """UX simplification 260610 v2 — 3 critique buttons: ❤️ love / ✨ more / 💰 cheaper.

    The ❤️ love button migrated FROM the summary ❤️ 1..5 row TO each card.
    Reuses the existing `card:like:{pid}` callback shape so
    `ingest._handle_card_like` handles it inline.
    """
    from app.graphs.nodes.send_results import (
        CRIT_CHEAP_EN,
        CRIT_CHEAP_KO,
        CRIT_LIKE_EN,
        CRIT_LIKE_KO,
        CRIT_MORE_EN,
        CRIT_MORE_KO,
        _critique_buttons_for,
    )

    en = _critique_buttons_for(0, lang="en", product_id="p1")
    assert len(en) == 3
    assert en[0] == (CRIT_LIKE_EN, "card:like:p1")
    assert en[1] == (CRIT_MORE_EN, "crit:more:0")
    assert en[2] == (CRIT_CHEAP_EN, "crit:cheap:0")

    ko = _critique_buttons_for(0, lang="ko", product_id="p1")
    assert len(ko) == 3
    assert ko[0] == (CRIT_LIKE_KO, "card:like:p1")
    assert ko[1] == (CRIT_MORE_KO, "crit:more:0")
    assert ko[2] == (CRIT_CHEAP_KO, "crit:cheap:0")


def test_critique_like_falls_back_to_idx_when_pid_missing():
    """When product_id is missing, ❤️ uses `card:like:{idx}` (matches the
    summary keyboard's prior idx-fallback behavior so ingest._handle_card_like
    can resolve via sess.last_results[idx])."""
    from app.graphs.nodes.send_results import _critique_buttons_for

    buttons = _critique_buttons_for(2, lang="en", product_id=None)
    assert buttons[0][1] == "card:like:2"


def test_critique_buttons_callback_within_64_bytes():
    from app.graphs.nodes.send_results import _critique_buttons_for

    long_id = "x" * 100
    buttons = _critique_buttons_for(0, lang="en", product_id=long_id)
    for _label, cb in buttons:
        assert len(cb.encode("utf-8")) <= 64
