"""Feature-axis taste scoring (0022) — product signals fan out to color/fit/material/...

Verifies that product-level signals (save/unsave/view/outbound) also teach the
user's visual-feature preferences via public.product_features.feature_metadata,
on top of the existing style-node scoring.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from psycopg.types.json import Jsonb


async def _login(client: AsyncClient) -> str:
    from app.core.social_auth.google import GoogleClaims

    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})
    return f"Bearer {resp.json()['access_token']}"


async def _insert_product(pool, feature_metadata: dict | None) -> int:
    """Insert a catalog product (+ optional enrichment row) and return its id."""
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


async def _feature_scores(pool) -> dict[tuple[str, str], float]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT axis, value, score, signal_count FROM ai.user_feature_scores")
        return {(r[0], r[1]): (r[2], r[3]) for r in await cur.fetchall()}


_HOODIE = {
    "primary_color": "BLACK",
    "secondary_colors": ["GREY"],
    "fit": "oversized",
    "material": ["cotton", "polyester"],
    "pattern": "solid",
    "neckline": "hooded",
    "style_tags": ["H"],
    "details": ["kangaroo pocket"],
}


@pytest.mark.asyncio
async def test_save_fans_out_to_feature_axes(client: AsyncClient, pool):
    auth = await _login(client)
    pid = await _insert_product(pool, _HOODIE)

    resp = await client.post("/v1/saves", json={"product_id": str(pid)}, headers={"Authorization": auth})
    assert resp.status_code == 201

    scores = await _feature_scores(pool)
    # color/fit/pattern/neckline each once, both materials — style_tags/details ignored.
    assert set(scores) == {
        ("color", "BLACK"),
        ("fit", "oversized"),
        ("pattern", "solid"),
        ("neckline", "hooded"),
        ("material", "cotton"),
        ("material", "polyester"),
    }
    # save weight is +2.0 for every axis.
    for (axis, value), (score, count) in scores.items():
        assert score == pytest.approx(2.0), (axis, value)
        assert count == 1


@pytest.mark.asyncio
async def test_outbound_uses_stronger_weight(client: AsyncClient, pool):
    auth = await _login(client)
    pid = await _insert_product(pool, _HOODIE)

    resp = await client.post(f"/v1/products/{pid}/outbound", json={}, headers={"Authorization": auth})
    assert resp.status_code == 200

    scores = await _feature_scores(pool)
    assert scores[("color", "BLACK")][0] == pytest.approx(4.0)  # outbound weight


@pytest.mark.asyncio
async def test_unenriched_product_is_noop(client: AsyncClient, pool):
    auth = await _login(client)
    pid = await _insert_product(pool, feature_metadata=None)

    resp = await client.post("/v1/saves", json={"product_id": str(pid)}, headers={"Authorization": auth})
    assert resp.status_code == 201
    assert await _feature_scores(pool) == {}


@pytest.mark.asyncio
async def test_na_values_are_skipped(client: AsyncClient, pool):
    auth = await _login(client)
    pid = await _insert_product(
        pool,
        {"primary_color": "WHITE", "fit": "n/a", "material": [], "pattern": "solid", "neckline": "n/a"},
    )

    await client.post("/v1/saves", json={"product_id": str(pid)}, headers={"Authorization": auth})

    scores = await _feature_scores(pool)
    assert set(scores) == {("color", "WHITE"), ("pattern", "solid")}


@pytest.mark.asyncio
async def test_signal_is_idempotent_within_dedupe_window(client: AsyncClient, pool):
    auth = await _login(client)
    pid = await _insert_product(pool, _HOODIE)

    # Two views of the same product on the same day share a dedupe key.
    for _ in range(2):
        await client.post(f"/v1/products/{pid}/view", json={}, headers={"Authorization": auth})

    scores = await _feature_scores(pool)
    assert scores[("color", "BLACK")] == (pytest.approx(1.0), 1)  # view weight, counted once
