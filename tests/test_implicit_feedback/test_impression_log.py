"""REQ-FB-IMPRESSION-001 + REQ-FB-MIGRATION-001."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_migration_creates_card_impression_table(pg_pool_with_impressions):
    from app.providers.db_pool import get_pool, run_in_pool_loop

    async def _check() -> list[str]:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema = 'ai' AND table_name = 'card_impression'
                 ORDER BY ordinal_position
                """
            )
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    cols = run_in_pool_loop(_check())
    assert set(cols) >= {
        "id",
        "chat_id",
        "from_user_id",
        "product_id",
        "brand",
        "keywords",
        "shown_at",
        "attribution_window_s",
        "click_status",
        "click_at",
    }


@pytest.mark.asyncio
async def test_log_impressions_inserts_one_row_per_candidate(pg_pool_with_impressions, fake_candidates):
    from app.channels.implicit_feedback import log_impressions
    from app.providers.db_pool import get_pool, run_in_pool_loop

    n = await log_impressions(7777, 9000, fake_candidates)
    assert n == 3

    async def _query():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT product_id, brand, keywords, click_status, attribution_window_s "
                "FROM ai.card_impression WHERE chat_id=%s ORDER BY id",
                (7777,),
            )
            return await cur.fetchall()

    rows = run_in_pool_loop(_query())
    assert len(rows) == 3
    product_ids = [r[0] for r in rows]
    assert product_ids == ["p1", "p2", "p3"]
    brands = [r[1] for r in rows]
    assert brands == ["ami", "acne", "maison"]
    statuses = [r[3] for r in rows]
    assert statuses == [None, None, None]
    windows = [r[4] for r in rows]
    assert all(w == 600 for w in windows)
    # keywords are lowercased
    for _pid, _br, kws, _s, _w in rows:
        assert all(k == k.lower() for k in kws)


@pytest.mark.asyncio
async def test_log_impressions_skips_missing_id(pg_pool_with_impressions, fake_candidates):
    from app.channels.implicit_feedback import log_impressions
    from app.providers.db_pool import get_pool, run_in_pool_loop
    from tests.test_implicit_feedback.conftest import FakeCandidate

    candidates = [FakeCandidate(id="", brand="x"), *fake_candidates]
    n = await log_impressions(8888, None, candidates)
    assert n == 3  # the id="" was skipped

    async def _count():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM ai.card_impression WHERE chat_id=%s", (8888,))
            return (await cur.fetchone())[0]

    assert run_in_pool_loop(_count()) == 3


@pytest.mark.asyncio
async def test_log_impressions_empty_list_is_zero(pg_pool_with_impressions):
    from app.channels.implicit_feedback import log_impressions

    n = await log_impressions(9999, None, [])
    assert n == 0
