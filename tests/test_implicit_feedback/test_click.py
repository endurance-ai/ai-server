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


def test_send_results_builds_four_critique_buttons():
    """REQ-FB-UX-001 — 4th 👀 button added per card with KO/EN labels."""
    from app.agents.tools.respond import _critique_buttons_for

    crit_click_en = "👀 View"
    crit_click_ko = "👀 자세히"

    en = _critique_buttons_for(0, lang="en", product_id="p1")
    assert len(en) == 4
    assert en[3][0] == crit_click_en
    assert en[3][1] == "crit:click:p1"

    ko = _critique_buttons_for(0, lang="ko", product_id="p1")
    assert ko[3][0] == crit_click_ko
    assert ko[3][1] == "crit:click:p1"


def test_critique_buttons_callback_within_64_bytes():
    from app.agents.tools.respond import _critique_buttons_for

    long_id = "x" * 100
    buttons = _critique_buttons_for(0, lang="en", product_id=long_id)
    for _label, cb in buttons:
        assert len(cb.encode("utf-8")) <= 64
