"""GET/POST /v1/products/* integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient


async def _login(client: AsyncClient) -> str:
    from app.core.social_auth.google import GoogleClaims

    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/auth/social", json={"provider": "google", "id_token": "t"})
    return f"Bearer {resp.json()['access_token']}"


async def _insert_product(pool, brand_node_id: int | None = None) -> int:
    """Insert a test product and return its id."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.products
                (brand, name, category, price, image_url, product_url, brand_node_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                "TestBrand",
                "Test Product",
                "tops",
                50000,
                "https://img.test/img.jpg",
                f"https://shop.test/{uuid4()}",
                brand_node_id,
            ),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def _insert_brand_node(pool) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO public.brand_nodes (brand_name) VALUES (%s) RETURNING id",
            ("TestBrand",),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


# ── GET /v1/products/{id} ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_product_returns_detail(client: AsyncClient, pool):
    auth = await _login(client)
    product_id = await _insert_product(pool)

    resp = await client.get(f"/v1/products/{product_id}", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == product_id
    assert data["brand"] == "TestBrand"
    assert data["name"] == "Test Product"
    assert data["price"] == 50000.0
    assert data["brand_node"] is None


@pytest.mark.asyncio
async def test_get_product_with_brand_node(client: AsyncClient, pool):
    auth = await _login(client)
    bn_id = await _insert_brand_node(pool)
    product_id = await _insert_product(pool, brand_node_id=bn_id)

    resp = await client.get(f"/v1/products/{product_id}", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["brand_node"] is not None
    assert data["brand_node"]["brand_name"] == "TestBrand"


@pytest.mark.asyncio
async def test_get_product_not_found(client: AsyncClient, pool):
    auth = await _login(client)
    resp = await client.get("/v1/products/9999999", headers={"Authorization": auth})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_product_requires_auth(client: AsyncClient, pool):
    product_id = await _insert_product(pool)
    resp = await client.get(f"/v1/products/{product_id}")
    assert resp.status_code in (401, 403)


# ── POST /v1/products/{id}/view ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_view_succeeds(client: AsyncClient, pool):
    auth = await _login(client)
    product_id = await _insert_product(pool)
    session_id = str(uuid4())

    resp = await client.post(
        f"/v1/products/{product_id}/view",
        json={"session_id": session_id},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recorded"] is True
    assert data["view_id"] is not None


@pytest.mark.asyncio
async def test_record_view_dedup_within_24h(client: AsyncClient, pool):
    auth = await _login(client)
    product_id = await _insert_product(pool)
    session_id = str(uuid4())

    resp1 = await client.post(
        f"/v1/products/{product_id}/view",
        json={"session_id": session_id},
        headers={"Authorization": auth},
    )
    assert resp1.json()["recorded"] is True

    resp2 = await client.post(
        f"/v1/products/{product_id}/view",
        json={"session_id": session_id},
        headers={"Authorization": auth},
    )
    assert resp2.status_code == 200
    assert resp2.json()["recorded"] is False


@pytest.mark.asyncio
async def test_record_view_with_optional_fields(client: AsyncClient, pool):
    auth = await _login(client)
    product_id = await _insert_product(pool)

    resp = await client.post(
        f"/v1/products/{product_id}/view",
        json={"session_id": str(uuid4()), "dwell_ms": 3000, "source_search_id": str(uuid4())},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True


@pytest.mark.asyncio
async def test_record_view_different_users_independent(client: AsyncClient, pool):
    auth1 = await _login(client)
    auth2 = await _login(client)
    product_id = await _insert_product(pool)
    session_id = str(uuid4())

    resp1 = await client.post(
        f"/v1/products/{product_id}/view",
        json={"session_id": session_id},
        headers={"Authorization": auth1},
    )
    resp2 = await client.post(
        f"/v1/products/{product_id}/view",
        json={"session_id": session_id},
        headers={"Authorization": auth2},
    )
    assert resp1.json()["recorded"] is True
    assert resp2.json()["recorded"] is True


@pytest.mark.asyncio
async def test_record_view_requires_auth(client: AsyncClient, pool):
    product_id = await _insert_product(pool)
    resp = await client.post(f"/v1/products/{product_id}/view", json={"session_id": str(uuid4())})
    assert resp.status_code in (401, 403)


# ── GET /v1/products/viewed ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_viewed_empty(client: AsyncClient, pool):
    auth = await _login(client)
    resp = await client.get(f"/v1/products/viewed?session_id={uuid4()}", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_viewed_returns_products(client: AsyncClient, pool):
    auth = await _login(client)
    session_id = str(uuid4())
    p1 = await _insert_product(pool)
    p2 = await _insert_product(pool)

    await client.post(f"/v1/products/{p1}/view", json={"session_id": session_id}, headers={"Authorization": auth})
    await client.post(f"/v1/products/{p2}/view", json={"session_id": session_id}, headers={"Authorization": auth})

    resp = await client.get(f"/v1/products/viewed?session_id={session_id}", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    product_ids = [item["product_id"] for item in data["items"]]
    assert p1 in product_ids
    assert p2 in product_ids


@pytest.mark.asyncio
async def test_list_viewed_requires_session_id(client: AsyncClient, pool):
    auth = await _login(client)
    resp = await client.get("/v1/products/viewed", headers={"Authorization": auth})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_viewed_requires_auth(client: AsyncClient, pool):
    resp = await client.get(f"/v1/products/viewed?session_id={uuid4()}")
    assert resp.status_code in (401, 403)


# ── POST /v1/products/{id}/link-check ───────────────────────────────────────


@pytest.mark.asyncio
async def test_link_check_alive(client: AsyncClient, pool):
    auth = await _login(client)
    product_id = await _insert_product(pool)

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.url = "https://shop.test/product"

    with patch("app.api.products.httpx.AsyncClient") as mock_cls:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_ctx.head = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_ctx

        resp = await client.post(f"/v1/products/{product_id}/link-check", headers={"Authorization": auth})

    assert resp.status_code == 200
    data = resp.json()
    assert data["alive"] is True
    assert data["http_status"] == 200


@pytest.mark.asyncio
async def test_link_check_dead(client: AsyncClient, pool):
    auth = await _login(client)
    product_id = await _insert_product(pool)

    mock_resp = AsyncMock()
    mock_resp.status_code = 404
    mock_resp.url = "https://shop.test/product"

    with patch("app.api.products.httpx.AsyncClient") as mock_cls:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_ctx.head = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_ctx

        resp = await client.post(f"/v1/products/{product_id}/link-check", headers={"Authorization": auth})

    assert resp.status_code == 200
    data = resp.json()
    assert data["alive"] is False


@pytest.mark.asyncio
async def test_link_check_not_found(client: AsyncClient, pool):
    auth = await _login(client)
    resp = await client.post("/v1/products/9999999/link-check", headers={"Authorization": auth})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_link_check_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/products/1/link-check")
    assert resp.status_code in (401, 403)
