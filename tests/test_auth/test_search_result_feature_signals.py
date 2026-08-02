"""Result-set feature fan-out (Phase 6) — search/feedback signals also teach feature axes.

Before Phase 6, ``record_search_result_signals`` only fed the style-node axis, so
search-heavy users (who rarely tap individual products) never built a color/fit/
material profile. This verifies the result set's *dominant* features now fan out to
``ai.user_feature_scores`` — while diffuse per-item features stay filtered out.

Both callers (chat_service search/image/chip, feedback conversation_like/dislike) go
through ``record_search_result_signals``, so exercising it directly covers both.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from psycopg.types.json import Jsonb

from app.services.curation_taste import record_search_result_signals


async def _login(client: AsyncClient) -> None:
    from app.core.social_auth.google import GoogleClaims

    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None),
    ):
        await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})


async def _user_id(pool) -> UUID:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT user_id FROM ai.user_profiles ORDER BY created_at DESC LIMIT 1")
        return (await cur.fetchone())[0]


async def _seed_session(pool, user_id: UUID) -> UUID:
    sid = uuid4()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.chat_sessions (session_id, user_id) VALUES (%s, %s)",
            (sid, user_id),
        )
        await conn.commit()
    return sid


async def _insert_product(pool, feature_metadata: dict | None) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.products (brand, name, category, price, image_url, product_url)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            ("TestBrand", "Test Product", "tops", 50000, "https://img.test/x.jpg", f"https://shop.test/{uuid4()}"),
        )
        product_id = (await cur.fetchone())[0]
        if feature_metadata is not None:
            await cur.execute(
                "INSERT INTO public.product_features (product_id, retrieval_text, feature_metadata) "
                "VALUES (%s, %s, %s)",
                (product_id, "a test garment", Jsonb(feature_metadata)),
            )
        await conn.commit()
    return product_id


async def _seed_search(pool, session_id: UUID, user_id: UUID, product_ids: list[int]) -> UUID:
    sid = uuid4()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.searches (search_id, session_id, user_id, title, product_ids, total)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (sid, session_id, user_id, "black tops", product_ids, len(product_ids)),
        )
        await conn.commit()
    return sid


async def _feature_scores(pool) -> dict[tuple[str, str], tuple[float, int]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT axis, value, score, signal_count FROM ai.user_feature_scores")
        return {(r[0], r[1]): (r[2], r[3]) for r in await cur.fetchall()}


def _garment(color: str, fit: str, pattern: str) -> dict:
    return {"primary_color": color, "fit": fit, "pattern": pattern, "material": [], "neckline": "n/a"}


async def _seed_result_set(pool, metadatas: list[dict | None]) -> tuple[UUID, UUID]:
    """Login + session + one search whose product_ids carry the given feature_metadata."""
    user_id = await _user_id(pool)
    session_id = await _seed_session(pool, user_id)
    pids = [await _insert_product(pool, md) for md in metadatas]
    search_id = await _seed_search(pool, session_id, user_id, pids)
    return user_id, search_id


@pytest.mark.asyncio
async def test_dominant_features_teach_axes_diffuse_ones_skipped(client: AsyncClient, pool):
    await _login(client)
    # 5 enriched results: color BLACK everywhere, pattern solid in 4/5, every fit distinct.
    # threshold = max(2, ceil(5 * 0.4)) = 2 → BLACK(5) & solid(4) survive, each fit(1) filtered.
    user_id, search_id = await _seed_result_set(
        pool,
        [
            _garment("BLACK", "oversized", "solid"),
            _garment("BLACK", "regular", "solid"),
            _garment("BLACK", "slim", "solid"),
            _garment("BLACK", "boxy", "solid"),
            _garment("BLACK", "relaxed", "striped"),
        ],
    )

    await record_search_result_signals(pool, user_id=user_id, search_id=search_id, signal_type="search")

    scores = await _feature_scores(pool)
    assert set(scores) == {("color", "BLACK"), ("pattern", "solid")}
    # "search" weight is +1.5, applied once per dominant pair.
    assert scores[("color", "BLACK")] == (pytest.approx(1.5), 1)
    assert scores[("pattern", "solid")] == (pytest.approx(1.5), 1)


@pytest.mark.asyncio
async def test_conversation_dislike_penalizes_dominant_feature(client: AsyncClient, pool):
    await _login(client)
    user_id, search_id = await _seed_result_set(
        pool,
        [_garment("BLACK", "oversized", "solid"), _garment("BLACK", "regular", "solid")],
    )

    await record_search_result_signals(pool, user_id=user_id, search_id=search_id, signal_type="conversation_dislike")

    scores = await _feature_scores(pool)
    # conversation_dislike is -4.0 — a disliked result set pushes its dominant color down.
    assert scores[("color", "BLACK")][0] == pytest.approx(-4.0)


@pytest.mark.asyncio
async def test_no_dominant_feature_is_noop(client: AsyncClient, pool):
    await _login(client)
    # 3 results, all features distinct → threshold 2, nothing reaches it.
    user_id, search_id = await _seed_result_set(
        pool,
        [
            _garment("BLACK", "oversized", "solid"),
            _garment("WHITE", "regular", "striped"),
            _garment("NAVY", "slim", "check"),
        ],
    )

    await record_search_result_signals(pool, user_id=user_id, search_id=search_id, signal_type="search")

    assert await _feature_scores(pool) == {}


@pytest.mark.asyncio
async def test_unenriched_result_set_is_noop(client: AsyncClient, pool):
    await _login(client)
    user_id, search_id = await _seed_result_set(pool, [None, None, None])

    await record_search_result_signals(pool, user_id=user_id, search_id=search_id, signal_type="image")

    assert await _feature_scores(pool) == {}


@pytest.mark.asyncio
async def test_result_feature_signal_is_idempotent(client: AsyncClient, pool):
    await _login(client)
    user_id, search_id = await _seed_result_set(
        pool,
        [_garment("BLACK", "oversized", "solid"), _garment("BLACK", "regular", "solid")],
    )

    for _ in range(2):
        await record_search_result_signals(pool, user_id=user_id, search_id=search_id, signal_type="search")

    scores = await _feature_scores(pool)
    # Same (signal_type, search_id, axis, value) dedupe key → counted once.
    assert scores[("color", "BLACK")] == (pytest.approx(1.5), 1)
