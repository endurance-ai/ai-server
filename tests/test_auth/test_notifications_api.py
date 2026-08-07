"""GET /v1/notifications · PATCH /v1/notifications/read integration tests.

배치가 적재한 ai.notifications 를 알림함이 정확히 읽고 읽음 처리하는지 본다.
행은 직접 insert 한다 (배치 감지 경로는 test_notify_batch 가 담당).
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from tests.test_auth.test_curation_onboarding_api import _insert_brand, _login


async def _insert_notification(
    pool,
    user_id: str,
    kind: str,
    *,
    product_id: int | None = None,
    brand_node_id: int | None = None,
    payload: dict | None = None,
    read: bool = False,
) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.notifications (user_id, kind, product_id, brand_node_id, payload, read_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, CASE WHEN %s THEN now() ELSE NULL END)
            RETURNING id
            """,
            (user_id, kind, product_id, brand_node_id, json.dumps(payload or {}), read),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


@pytest.mark.asyncio
async def test_list_notifications_empty(client: AsyncClient, pool):
    auth, _ = await _login(client)
    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["unread_count"] == 0
    assert data["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_notifications_newest_first_with_unread_count(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    await _insert_notification(pool, user_id, "restock", product_id=1, payload={"brand": "A", "name": "셔츠"})
    await _insert_notification(pool, user_id, "brand_new_product", brand_node_id=None, payload={"brand": "B"})
    third = await _insert_notification(pool, user_id, "restock", product_id=2, payload={"brand": "C"}, read=True)

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert [i["id"] for i in data["items"]] == [str(third), str(third - 1), str(third - 2)]
    assert data["items"][0]["read"] is True
    assert data["items"][1]["read"] is False
    # 3건 중 1건만 읽음 → unread 2.
    assert data["unread_count"] == 2


@pytest.mark.asyncio
async def test_price_drop_exposes_old_and_new_price(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    await _insert_notification(
        pool,
        user_id,
        "price_drop",
        product_id=7,
        payload={"brand": "MAISON", "name": "린넨 셔츠", "price": 80000, "baseline_price": 100000, "drop_pct": 20},
    )

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    item = resp.json()["items"][0]
    assert item["type"] == "price_drop"
    assert item["old_price"] == 100000
    assert item["new_price"] == 80000
    assert item["brand"] == "MAISON"
    assert "할인" in item["text"]
    assert "20%" in item["sub"]
    assert item["product_id"] == 7


@pytest.mark.asyncio
async def test_brand_sale_copy_and_brand_join(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _insert_notification(
        pool, user_id, "brand_sale", brand_node_id=brand_id, payload={"brand": "Acme", "sale_pct": 40}
    )

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    item = resp.json()["items"][0]
    assert item["type"] == "brand_sale"
    assert item["brand_id"] == brand_id
    assert item["product_id"] is None
    assert item["old_price"] is None
    assert "세일" in item["text"]
    assert "Acme" in item["text"]


@pytest.mark.asyncio
async def test_list_notifications_pagination(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    for i in range(3):
        await _insert_notification(pool, user_id, "restock", product_id=i, payload={"brand": "A"})

    first = await client.get("/v1/notifications?limit=2", headers={"Authorization": auth})
    data = first.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is not None

    second = await client.get(
        f"/v1/notifications?limit=2&cursor={data['next_cursor']}", headers={"Authorization": auth}
    )
    data2 = second.json()
    assert len(data2["items"]) == 1
    assert data2["next_cursor"] is None


@pytest.mark.asyncio
async def test_notifications_are_user_scoped(client: AsyncClient, pool):
    auth_a, user_a = await _login(client)
    _auth_b, user_b = await _login(client)
    await _insert_notification(pool, user_b, "restock", product_id=1, payload={"brand": "A"})

    resp = await client.get("/v1/notifications", headers={"Authorization": auth_a})
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_mark_read_by_ids(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    n1 = await _insert_notification(pool, user_id, "restock", product_id=1, payload={"brand": "A"})
    await _insert_notification(pool, user_id, "restock", product_id=2, payload={"brand": "B"})

    resp = await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={"ids": [str(n1)]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["marked"] == 1
    assert data["unread_count"] == 1


@pytest.mark.asyncio
async def test_mark_read_all(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    for i in range(3):
        await _insert_notification(pool, user_id, "restock", product_id=i, payload={"brand": "A"})

    resp = await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={"all": True})
    assert resp.status_code == 200
    assert resp.json()["unread_count"] == 0
    assert resp.json()["marked"] == 3


@pytest.mark.asyncio
async def test_mark_read_requires_exactly_one_selector(client: AsyncClient, pool):
    auth, _ = await _login(client)
    both = await client.patch(
        "/v1/notifications/read", headers={"Authorization": auth}, json={"all": True, "ids": ["1"]}
    )
    neither = await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={})
    assert both.status_code == 422
    assert neither.status_code == 422


@pytest.mark.asyncio
async def test_notifications_require_auth(client: AsyncClient):
    assert (await client.get("/v1/notifications")).status_code in (401, 403)
    assert (await client.patch("/v1/notifications/read", json={"all": True})).status_code in (401, 403)
