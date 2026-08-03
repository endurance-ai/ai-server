"""POST/GET/DELETE /v1/saves integration tests."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

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


async def _insert_product(pool) -> int:
    """Insert a test product into the catalog and return its numeric id."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.products (brand, name, category, price, image_url, product_url)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            ("TestBrand", "Test Product", "tops", 50000, "https://img.test/x.jpg", f"https://shop.test/{uuid4()}"),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


# ── POST /v1/saves ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_save_succeeds(client: AsyncClient):
    auth = await _login(client)
    resp = await client.post("/v1/saves", json={"product_id": "prod-001"}, headers={"Authorization": auth})
    assert resp.status_code == 201
    data = resp.json()
    assert data["product_id"] == "prod-001"
    assert "save_id" in data
    assert "created_at" in data


@pytest.mark.asyncio
async def test_add_save_duplicate_returns_409(client: AsyncClient):
    auth = await _login(client)
    await client.post("/v1/saves", json={"product_id": "prod-dup"}, headers={"Authorization": auth})
    resp = await client.post("/v1/saves", json={"product_id": "prod-dup"}, headers={"Authorization": auth})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_add_save_empty_product_id_rejected(client: AsyncClient):
    auth = await _login(client)
    resp = await client.post("/v1/saves", json={"product_id": "  "}, headers={"Authorization": auth})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_add_save_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/saves", json={"product_id": "prod-001"})
    assert resp.status_code in (401, 403)


# ── GET /v1/saves ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_saves_empty(client: AsyncClient):
    auth = await _login(client)
    resp = await client.get("/v1/saves", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_saves_returns_saved_items(client: AsyncClient, pool):
    auth = await _login(client)
    pid = await _insert_product(pool)
    await client.post("/v1/saves", json={"product_id": str(pid)}, headers={"Authorization": auth})
    await client.post("/v1/saves", json={"product_id": "prod-B"}, headers={"Authorization": auth})

    resp = await client.get("/v1/saves", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    # numeric product_id joins to the catalog → nested product object present
    joined = [i for i in data["items"] if i["product"] is not None]
    assert len(joined) == 1
    assert joined[0]["product"]["id"] == pid
    assert joined[0]["product"]["brand"] == "TestBrand"
    assert joined[0]["product"]["price"] == 50000.0
    # non-numeric / missing-from-catalog product → product is None
    missing = [i for i in data["items"] if i["product"] is None]
    assert len(missing) == 1


@pytest.mark.asyncio
async def test_list_saves_pagination(client: AsyncClient):
    auth = await _login(client)
    for i in range(3):
        await client.post("/v1/saves", json={"product_id": f"prod-{i}"}, headers={"Authorization": auth})

    resp = await client.get("/v1/saves?limit=2", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is not None

    resp2 = await client.get(f"/v1/saves?limit=2&cursor={data['next_cursor']}", headers={"Authorization": auth})
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["items"]) == 1
    assert data2["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_saves_isolated_between_users(client: AsyncClient):
    auth1 = await _login(client)
    auth2 = await _login(client)
    await client.post("/v1/saves", json={"product_id": "prod-X"}, headers={"Authorization": auth1})

    resp = await client.get("/v1/saves", headers={"Authorization": auth2})
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_saves_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/saves")
    assert resp.status_code in (401, 403)


# ── DELETE /v1/saves/{product_id} ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_save_succeeds(client: AsyncClient):
    auth = await _login(client)
    await client.post("/v1/saves", json={"product_id": "prod-del"}, headers={"Authorization": auth})
    resp = await client.delete("/v1/saves/prod-del", headers={"Authorization": auth})
    assert resp.status_code == 204

    saves = await client.get("/v1/saves", headers={"Authorization": auth})
    assert saves.json()["total"] == 0


@pytest.mark.asyncio
async def test_remove_save_not_found(client: AsyncClient):
    auth = await _login(client)
    resp = await client.delete("/v1/saves/nonexistent", headers={"Authorization": auth})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_save_other_user_returns_404(client: AsyncClient):
    auth1 = await _login(client)
    auth2 = await _login(client)
    await client.post("/v1/saves", json={"product_id": "prod-Y"}, headers={"Authorization": auth1})
    resp = await client.delete("/v1/saves/prod-Y", headers={"Authorization": auth2})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_save_requires_auth(client: AsyncClient):
    resp = await client.delete("/v1/saves/prod-001")
    assert resp.status_code in (401, 403)
