"""브랜드 팔로우 + 브랜드 홈 API 통합 테스트.

POST/DELETE /v1/brands/{id}/follow · GET /v1/me/follows ·
GET /v1/brands/{id} · GET /v1/brands/{id}/products
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.test_auth.test_curation_onboarding_api import _insert_brand, _insert_product, _login


async def _count_follows(pool, user_id: str) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM ai.user_brand_picks WHERE user_id = %s", (user_id,))
        return (await cur.fetchone())[0]


# ── Follow ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_follow_brand_defaults_to_notify_on(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")

    resp = await client.post(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth}, json={})
    assert resp.status_code == 200
    assert resp.json() == {"following": True, "notify_enabled": True}
    assert await _count_follows(pool, user_id) == 1


@pytest.mark.asyncio
async def test_follow_brand_notify_false(client: AsyncClient, pool):
    auth, _ = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")

    resp = await client.post(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth}, json={"notify": False})
    assert resp.status_code == 200
    assert resp.json()["notify_enabled"] is False


@pytest.mark.asyncio
async def test_refollow_updates_notify_flag(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")

    await client.post(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth}, json={"notify": True})
    resp = await client.post(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth}, json={"notify": False})
    assert resp.json()["notify_enabled"] is False
    # 재호출은 업서트 — 행이 늘지 않는다.
    assert await _count_follows(pool, user_id) == 1


@pytest.mark.asyncio
async def test_follow_unknown_brand_404(client: AsyncClient, pool):
    auth, _ = await _login(client)
    resp = await client.post("/v1/brands/999999/follow", headers={"Authorization": auth}, json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unfollow_removes_row_and_is_idempotent(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await client.post(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth}, json={})

    resp = await client.delete(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.json() == {"following": False}
    assert await _count_follows(pool, user_id) == 0

    # 팔로우 중이 아니어도 200.
    again = await client.delete(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth})
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_follow_requires_auth(client: AsyncClient, pool):
    brand_id = await _insert_brand(pool, "Acme")
    assert (await client.post(f"/v1/brands/{brand_id}/follow", json={})).status_code in (401, 403)
    assert (await client.delete(f"/v1/brands/{brand_id}/follow")).status_code in (401, 403)


# ── /me/follows ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_follows_returns_brand_names(client: AsyncClient, pool):
    auth, _ = await _login(client)
    b1 = await _insert_brand(pool, "Alpha")
    b2 = await _insert_brand(pool, "Beta")
    await client.post(f"/v1/brands/{b1}/follow", headers={"Authorization": auth}, json={})
    await client.post(f"/v1/brands/{b2}/follow", headers={"Authorization": auth}, json={"notify": False})

    resp = await client.get("/v1/me/follows", headers={"Authorization": auth})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {i["brand_name"] for i in items} == {"Alpha", "Beta"}
    by_name = {i["brand_name"]: i for i in items}
    assert by_name["Beta"]["notify_enabled"] is False
    assert by_name["Alpha"]["brand_id"] == b1


@pytest.mark.asyncio
async def test_list_follows_pagination(client: AsyncClient, pool):
    auth, _ = await _login(client)
    for name in ("A", "B", "C"):
        bid = await _insert_brand(pool, name)
        await client.post(f"/v1/brands/{bid}/follow", headers={"Authorization": auth}, json={})

    first = await client.get("/v1/me/follows?limit=2", headers={"Authorization": auth})
    data = first.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is not None

    second = await client.get(f"/v1/me/follows?limit=2&cursor={data['next_cursor']}", headers={"Authorization": auth})
    data2 = second.json()
    assert len(data2["items"]) == 1
    assert data2["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_follows_requires_auth(client: AsyncClient):
    assert (await client.get("/v1/me/follows")).status_code in (401, 403)


# ── Brand home ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_home_anonymous(client: AsyncClient, pool):
    brand_id = await _insert_brand(pool, "Acme")
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id)
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id)

    resp = await client.get(f"/v1/brands/{brand_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == brand_id
    assert data["name"] == "Acme"
    assert data["product_count"] == 2
    assert data["following"] is False
    assert data["notify_enabled"] is False
    assert data["description"] is None
    assert data["store_url"] is None
    assert data["news"] is None


@pytest.mark.asyncio
async def test_brand_home_returns_description_store_url_and_news(client: AsyncClient, pool):
    brand_id = await _insert_brand(
        pool,
        "Acme",
        description="A test brand.",
        homepage_url="https://acme.example",
        news="26SS 컬렉션 8/20 발매",
    )

    resp = await client.get(f"/v1/brands/{brand_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "A test brand."
    assert data["store_url"] == "https://acme.example"
    assert data["news"] == "26SS 컬렉션 8/20 발매"


@pytest.mark.asyncio
async def test_brand_home_reflects_following(client: AsyncClient, pool):
    auth, _ = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await client.post(f"/v1/brands/{brand_id}/follow", headers={"Authorization": auth}, json={"notify": True})

    resp = await client.get(f"/v1/brands/{brand_id}", headers={"Authorization": auth})
    data = resp.json()
    assert data["following"] is True
    assert data["notify_enabled"] is True


@pytest.mark.asyncio
async def test_brand_home_404(client: AsyncClient):
    assert (await client.get("/v1/brands/999999")).status_code == 404


# ── Brand products ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_products_list_and_sale_fields(client: AsyncClient, pool):
    brand_id = await _insert_brand(pool, "Acme")
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id, price=50000)
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id, price=30000)

    resp = await client.get(f"/v1/brands/{brand_id}/products")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {"id", "brand", "name", "price", "original_price", "sale_price", "image_url", "product_url"} <= set(items[0])


@pytest.mark.asyncio
async def test_brand_products_gender_filter(client: AsyncClient, pool):
    brand_id = await _insert_brand(pool, "Acme")
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id, gender=["women"])
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id, gender=["men"])

    men = await client.get(f"/v1/brands/{brand_id}/products?gender=men")
    assert men.status_code == 200
    items = men.json()["items"]
    assert len(items) == 1


@pytest.mark.asyncio
async def test_brand_products_pagination(client: AsyncClient, pool):
    brand_id = await _insert_brand(pool, "Acme")
    for _ in range(3):
        await _insert_product(pool, brand="Acme", brand_node_id=brand_id)

    first = await client.get(f"/v1/brands/{brand_id}/products?limit=2")
    data = first.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is not None

    second = await client.get(f"/v1/brands/{brand_id}/products?limit=2&cursor={data['next_cursor']}")
    data2 = second.json()
    assert len(data2["items"]) == 1
    assert data2["next_cursor"] is None
