"""REQ-FB-CLEANUP-001 — 7d attributed + 60d pending hard delete."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_cleanup_deletes_old_attributed_and_pending(pg_pool_with_impressions):
    from app.channels.implicit_feedback import cleanup_card_impressions
    from app.providers.db_pool import get_pool, run_in_pool_loop

    async def _seed():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            # A: clicked 8d ago → delete
            await cur.execute(
                """
                INSERT INTO ai.card_impression
                  (chat_id, product_id, brand, attribution_window_s, click_status, click_at, shown_at)
                VALUES (1, 'A', 'a', 600, 'clicked', now() - interval '8 days', now() - interval '8 days')
                """
            )
            # B: clicked 3d ago → keep
            await cur.execute(
                """
                INSERT INTO ai.card_impression
                  (chat_id, product_id, brand, attribution_window_s, click_status, click_at, shown_at)
                VALUES (1, 'B', 'b', 600, 'clicked', now() - interval '3 days', now() - interval '3 days')
                """
            )
            # C: attributed_no_click 8d ago → delete (via shown_at + window)
            await cur.execute(
                """
                INSERT INTO ai.card_impression
                  (chat_id, product_id, brand, attribution_window_s, click_status, shown_at)
                VALUES (1, 'C', 'c', 600, 'attributed_no_click', now() - interval '8 days')
                """
            )
            # D: attributed_no_click 6d ago → keep
            await cur.execute(
                """
                INSERT INTO ai.card_impression
                  (chat_id, product_id, brand, attribution_window_s, click_status, shown_at)
                VALUES (1, 'D', 'd', 600, 'attributed_no_click', now() - interval '6 days')
                """
            )
            # E: pending NULL 70d ago → delete
            await cur.execute(
                """
                INSERT INTO ai.card_impression
                  (chat_id, product_id, brand, attribution_window_s, shown_at)
                VALUES (1, 'E', 'e', 600, now() - interval '70 days')
                """
            )
            # F: pending NULL 10d ago → keep
            await cur.execute(
                """
                INSERT INTO ai.card_impression
                  (chat_id, product_id, brand, attribution_window_s, shown_at)
                VALUES (1, 'F', 'f', 600, now() - interval '10 days')
                """
            )
            await conn.commit()

    run_in_pool_loop(_seed())

    attr, pending = await cleanup_card_impressions()
    assert attr == 2  # A + C
    assert pending == 1  # E

    async def _remaining():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT product_id FROM ai.card_impression WHERE chat_id=1 ORDER BY product_id")
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    assert run_in_pool_loop(_remaining()) == ["B", "D", "F"]


@pytest.mark.asyncio
async def test_cleanup_idempotent_on_empty(pg_pool_with_impressions):
    from app.channels.implicit_feedback import cleanup_card_impressions

    attr, pending = await cleanup_card_impressions()
    assert (attr, pending) == (0, 0)


@pytest.mark.asyncio
async def test_cleanup_in_memory_skip():
    from app.channels.implicit_feedback import cleanup_card_impressions, set_backend_is_postgres

    set_backend_is_postgres(False)
    attr, pending = await cleanup_card_impressions()
    assert (attr, pending) == (0, 0)
