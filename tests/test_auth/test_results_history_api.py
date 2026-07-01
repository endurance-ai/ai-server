"""Integration tests for GET /v1/results, /v1/results/{search_id}, /v1/history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient


async def _login(client: AsyncClient) -> str:
    from app.core.social_auth.google import GoogleClaims

    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})
    return f"Bearer {resp.json()['access_token']}"


async def _user_id(pool) -> UUID:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT user_id FROM ai.user_profiles ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
    return row[0]


async def _seed_session(pool, user_id: UUID) -> UUID:
    sid = uuid4()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.chat_sessions (session_id, user_id) VALUES (%s, %s)",
            (sid, user_id),
        )
        await conn.commit()
    return sid


async def _insert_product(pool, brand: str = "Brand", price: int = 50000) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.products (brand, name, category, price, image_url, product_url)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (brand, f"{brand} item", "tops", price, f"https://img.test/{uuid4()}.jpg", f"https://shop.test/{uuid4()}"),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def _seed_search(
    pool,
    session_id: UUID,
    user_id: UUID,
    product_ids: list[int],
    *,
    is_listed: bool = False,
    title: str = "leather jacket",
    created_at: datetime | None = None,
) -> UUID:
    sid = uuid4()
    async with pool.connection() as conn, conn.cursor() as cur:
        cover_image_urls: list[str] = []
        if product_ids:
            await cur.execute(
                "SELECT image_url FROM public.products WHERE id = ANY(%s) ORDER BY array_position(%s, id) LIMIT 4",
                (product_ids, product_ids),
            )
            cover_image_urls = [r[0] for r in await cur.fetchall()]
        await cur.execute(
            """
            INSERT INTO ai.searches
                (search_id, session_id, user_id, title, product_ids, cover_image_urls, total, is_listed, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
            """,
            (sid, session_id, user_id, title, product_ids, cover_image_urls, len(product_ids), is_listed, created_at),
        )
        await conn.commit()
    return sid


async def _seed_view(pool, session_id: UUID, user_id: UUID, product_id: int, viewed_at: datetime) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.product_views (user_id, product_id, session_id, viewed_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, product_id, session_id, viewed_at),
        )
        await conn.commit()


# ── GET /v1/results ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_result_sets_only_listed(client: AsyncClient, pool):
    auth = await _login(client)
    uid = await _user_id(pool)
    session_id = await _seed_session(pool, uid)
    p1 = await _insert_product(pool)
    await _seed_search(pool, session_id, uid, [p1], is_listed=True, title="listed one")
    await _seed_search(pool, session_id, uid, [p1], is_listed=False, title="hidden one")

    resp = await client.get(f"/v1/results?session_id={session_id}", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "listed one"
    assert data["items"][0]["preview_images"]  # non-empty


@pytest.mark.asyncio
async def test_list_result_sets_requires_auth(client: AsyncClient):
    resp = await client.get(f"/v1/results?session_id={uuid4()}")
    assert resp.status_code in (401, 403)


# ── GET /v1/results/{search_id} ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_result_set_page_orders_and_flips_listed(client: AsyncClient, pool):
    auth = await _login(client)
    uid = await _user_id(pool)
    session_id = await _seed_session(pool, uid)
    ids = [await _insert_product(pool, brand=f"B{i}") for i in range(3)]
    search_id = await _seed_search(pool, session_id, uid, ids, is_listed=False)

    resp = await client.get(f"/v1/results/{search_id}", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert [it["product_id"] for it in data["items"]] == ids  # rank order preserved
    assert [it["rank"] for it in data["items"]] == [1, 2, 3]
    assert data["result_count"] == 3

    # Opening the set must have flipped is_listed → now appears in /v1/results.
    listed = await client.get(f"/v1/results?session_id={session_id}", headers={"Authorization": auth})
    assert len(listed.json()["items"]) == 1


@pytest.mark.asyncio
async def test_get_result_set_page_pagination(client: AsyncClient, pool):
    auth = await _login(client)
    uid = await _user_id(pool)
    session_id = await _seed_session(pool, uid)
    ids = [await _insert_product(pool, brand=f"P{i}") for i in range(3)]
    search_id = await _seed_search(pool, session_id, uid, ids)

    first = await client.get(f"/v1/results/{search_id}?limit=2", headers={"Authorization": auth})
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] == "2"

    second = await client.get(
        f"/v1/results/{search_id}?limit=2&cursor={body['next_cursor']}", headers={"Authorization": auth}
    )
    body2 = second.json()
    assert [it["product_id"] for it in body2["items"]] == [ids[2]]
    assert body2["next_cursor"] is None


@pytest.mark.asyncio
async def test_get_result_set_not_found(client: AsyncClient, pool):
    auth = await _login(client)
    resp = await client.get(f"/v1/results/{uuid4()}", headers={"Authorization": auth})
    assert resp.status_code == 404


# ── GET /v1/history ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_unified_merge_time_ordered(client: AsyncClient, pool):
    auth = await _login(client)
    uid = await _user_id(pool)
    session_id = await _seed_session(pool, uid)
    p1 = await _insert_product(pool)
    now = datetime.now(UTC)
    # search older, view newer → view should come first (DESC)
    await _seed_search(pool, session_id, uid, [p1], is_listed=True, created_at=now - timedelta(minutes=5))
    await _seed_view(pool, session_id, uid, p1, now)

    resp = await client.get(f"/v1/history?session_id={session_id}", headers={"Authorization": auth})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it["type"] for it in items] == ["product", "result_set"]
    assert items[0]["product_id"] == p1
    assert items[1]["search_id"]


@pytest.mark.asyncio
async def test_history_type_filter_product_only(client: AsyncClient, pool):
    auth = await _login(client)
    uid = await _user_id(pool)
    session_id = await _seed_session(pool, uid)
    p1 = await _insert_product(pool)
    now = datetime.now(UTC)
    await _seed_search(pool, session_id, uid, [p1], is_listed=True, created_at=now - timedelta(minutes=1))
    await _seed_view(pool, session_id, uid, p1, now)

    resp = await client.get(f"/v1/history?session_id={session_id}&type=product", headers={"Authorization": auth})
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "product"
