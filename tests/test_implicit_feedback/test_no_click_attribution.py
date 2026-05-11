"""REQ-FB-NOCLICK-001 lazy attribution of expired pending impressions."""

from __future__ import annotations

import pytest


async def _expire_row(chat_id: int, product_id: str, age_seconds: int):
    from app.providers.db_pool import get_pool

    async with get_pool().connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            UPDATE ai.card_impression
               SET shown_at = now() - interval '{age_seconds} seconds'
             WHERE chat_id = %s AND product_id = %s
            """,
            (chat_id, product_id),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_attribute_expired_credits_disliked(pg_pool_with_impressions, fake_candidates, in_memory_taste_store):
    from app.channels.implicit_feedback import attribute_expired_impressions, log_impressions
    from app.providers.db_pool import get_pool, run_in_pool_loop

    await log_impressions(21, None, fake_candidates)

    # Expire p1 (shown 1200s ago, window 600s ⇒ past) but leave p2/p3 pending
    async def _setup():
        await _expire_row(21, "p1", 1200)

    run_in_pool_loop(_setup())

    n = await attribute_expired_impressions(21, None)
    assert n == 1

    async def _status():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT product_id, click_status FROM ai.card_impression WHERE chat_id=%s ORDER BY id",
                (21,),
            )
            return await cur.fetchall()

    rows = run_in_pool_loop(_status())
    assert ("p1", "attributed_no_click") in rows
    assert ("p2", None) in rows
    assert ("p3", None) in rows

    profile = in_memory_taste_store.get_or_create("c:21")
    assert "ami" in profile.disliked_brands
    assert "oversized" in profile.disliked_keywords


@pytest.mark.asyncio
async def test_attribute_does_not_revisit_clicked(pg_pool_with_impressions, fake_candidates, in_memory_taste_store):
    from app.channels.implicit_feedback import (
        attribute_expired_impressions,
        log_impressions,
        record_click,
    )
    from app.providers.db_pool import get_pool, run_in_pool_loop

    await log_impressions(22, None, fake_candidates)
    await record_click(22, None, "p1", "ami", ["oversized"])

    # Expire all rows
    async def _setup():
        for pid in ("p1", "p2", "p3"):
            await _expire_row(22, pid, 1200)

    run_in_pool_loop(_setup())
    n = await attribute_expired_impressions(22, None)
    # p1 already clicked → still p2 + p3 attributed
    assert n == 2

    async def _status():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT product_id, click_status FROM ai.card_impression WHERE chat_id=%s ORDER BY product_id",
                (22,),
            )
            return await cur.fetchall()

    rows = dict(run_in_pool_loop(_status()))
    assert rows["p1"] == "clicked"
    assert rows["p2"] == "attributed_no_click"
    assert rows["p3"] == "attributed_no_click"


@pytest.mark.asyncio
async def test_attribute_empty_returns_zero(pg_pool_with_impressions):
    from app.channels.implicit_feedback import attribute_expired_impressions

    n = await attribute_expired_impressions(404, None)
    assert n == 0
