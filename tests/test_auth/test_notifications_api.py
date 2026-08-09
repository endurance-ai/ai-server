"""GET /v1/notifications · PATCH /v1/notifications/read integration tests.

알림함이 두 소스를 합쳐 정확히 읽고 읽음 처리하는지 본다 (migration 0027/0028).
  source `n` — ai.notifications: 유저별 행, 행마다 read_at
  source `b` — ai.brand_news: 공유 정본, 워터마크 + 예외 테이블로 읽음 판정
행은 직접 insert 한다 (배치 감지 경로는 test_notify_batch 가 담당).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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


async def _follow(pool, user_id: str, brand_id: int, *, notify: bool = True, at: datetime | None = None) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_brand_picks (user_id, brand_id, notify_enabled, source, created_at)
            VALUES (%s, %s, %s, 'follow', coalesce(%s, now()))
            """,
            (user_id, brand_id, notify, at),
        )
        await conn.commit()


async def _insert_brand_news(pool, brand_id: int, *, payload: dict | None = None, at: datetime | None = None) -> int:
    """소식 1건 추가. 열려 있던 이전 소식은 닫는다.

    uq_brand_news_open 이 브랜드당 진행 중 소식을 1건으로 강제하므로, 실제 배치와
    같은 순서(이전 세일 종료 → 새 세일 시작)를 테스트에서도 지켜야 한다.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE ai.brand_news SET ended_at = started_at WHERE brand_node_id = %s AND ended_at IS NULL",
            (brand_id,),
        )
        await cur.execute(
            """
            INSERT INTO ai.brand_news (brand_node_id, kind, payload, started_at)
            VALUES (%s, 'brand_sale', %s::jsonb, coalesce(%s, now()))
            RETURNING id
            """,
            (brand_id, json.dumps(payload or {}), at),
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
    assert [i["id"] for i in data["items"]] == [f"n:{third}", f"n:{third - 1}", f"n:{third - 2}"]
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


# ── source `b` — 브랜드 소식 read fan-out ─────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_news_appears_in_feed_via_read_fanout(client: AsyncClient, pool):
    """유저별 알림 행 없이 ai.brand_news 정본만으로 피드에 뜬다."""
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    news_id = await _insert_brand_news(pool, brand_id, payload={"brand": "Acme", "max_discount_pct": 40})

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    data = resp.json()
    assert [i["id"] for i in data["items"]] == [f"b:{news_id}"]
    item = data["items"][0]
    assert item["type"] == "brand_sale"
    assert item["brand_id"] == brand_id
    assert item["product_id"] is None
    assert item["read"] is False
    # 피드 문구는 브랜드 홈과 달리 "팔로우한" 을 붙인다 (맥락이 없으므로).
    assert item["text"] == "팔로우한 Acme 세일 시작했어요"
    assert item["sub"] == "최대 40% 싸요"
    assert data["unread_count"] == 1

    # ai.notifications 에는 아무 행도 없다 — 팬아웃하지 않았다는 증거.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM ai.notifications")
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_brand_news_hidden_from_non_followers(client: AsyncClient, pool):
    auth_a, _user_a = await _login(client)
    _auth_b, user_b = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_b, brand_id)
    await _insert_brand_news(pool, brand_id)

    resp = await client.get("/v1/notifications", headers={"Authorization": auth_a})
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_brand_news_hidden_when_notify_disabled(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id, notify=False)
    await _insert_brand_news(pool, brand_id)

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_brand_news_before_follow_is_not_backfilled(client: AsyncClient, pool):
    """팔로우 이전 소식은 보여주지 않는다 — write fan-out 시절 동작을 유지한다."""
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    now = datetime.now(tz=UTC)
    await _follow(pool, user_id, brand_id, at=now)
    await _insert_brand_news(pool, brand_id, at=now - timedelta(days=3))
    recent = await _insert_brand_news(pool, brand_id, at=now + timedelta(minutes=1))

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    assert [i["id"] for i in resp.json()["items"]] == [f"b:{recent}"]


@pytest.mark.asyncio
async def test_push_ledger_rows_are_excluded_from_the_feed(client: AsyncClient, pool):
    """ai.notifications 의 brand_sale 행은 아웃박스 앵커다 — 피드에 중복으로 뜨면 안 된다."""
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    news_id = await _insert_brand_news(pool, brand_id, payload={"brand": "Acme"})
    await _insert_notification(pool, user_id, "brand_sale", brand_node_id=brand_id, payload={"brand": "Acme"})

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    data = resp.json()
    assert [i["id"] for i in data["items"]] == [f"b:{news_id}"]
    assert data["unread_count"] == 1


@pytest.mark.asyncio
async def test_feed_merges_both_sources_in_time_order(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    now = datetime.now(tz=UTC)
    await _follow(pool, user_id, brand_id, at=now - timedelta(days=10))

    old_news = await _insert_brand_news(pool, brand_id, at=now - timedelta(hours=3))
    middle = await _insert_notification(pool, user_id, "restock", product_id=1, payload={"brand": "A"})
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE ai.notifications SET created_at = %s WHERE id = %s", (now - timedelta(hours=2), middle)
        )
        await conn.commit()
    new_news = await _insert_brand_news(pool, brand_id, at=now - timedelta(hours=1))

    resp = await client.get("/v1/notifications", headers={"Authorization": auth})
    assert [i["id"] for i in resp.json()["items"]] == [f"b:{new_news}", f"n:{middle}", f"b:{old_news}"]


@pytest.mark.asyncio
async def test_pagination_across_both_sources(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    now = datetime.now(tz=UTC)
    await _follow(pool, user_id, brand_id, at=now - timedelta(days=10))
    for i in range(3):
        await _insert_brand_news(pool, brand_id, at=now - timedelta(hours=i))
    for i in range(3):
        await _insert_notification(pool, user_id, "restock", product_id=i, payload={"brand": "A"})

    seen: list[str] = []
    cursor = None
    for _ in range(4):  # 6건을 limit=2 로 3페이지 + 종료 확인
        url = "/v1/notifications?limit=2" + (f"&cursor={cursor}" if cursor else "")
        data = (await client.get(url, headers={"Authorization": auth})).json()
        seen.extend(i["id"] for i in data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert len(seen) == 6
    assert len(set(seen)) == 6  # 페이지 경계에서 중복/유실 없음


# ── 읽음 처리 ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notifications_are_user_scoped(client: AsyncClient, pool):
    auth_a, _user_a = await _login(client)
    _auth_b, user_b = await _login(client)
    await _insert_notification(pool, user_b, "restock", product_id=1, payload={"brand": "A"})

    resp = await client.get("/v1/notifications", headers={"Authorization": auth_a})
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_mark_read_by_ids(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    n1 = await _insert_notification(pool, user_id, "restock", product_id=1, payload={"brand": "A"})
    await _insert_notification(pool, user_id, "restock", product_id=2, payload={"brand": "B"})

    resp = await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={"ids": [f"n:{n1}"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["marked"] == 1
    assert data["unread_count"] == 1


@pytest.mark.asyncio
async def test_mark_brand_news_read_by_id_uses_the_exception_table(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    first = await _insert_brand_news(pool, brand_id)
    await _insert_brand_news(pool, brand_id, payload={"brand": "Acme"})

    resp = await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={"ids": [f"b:{first}"]})
    assert resp.json() == {"marked": 1, "unread_count": 1}

    feed = (await client.get("/v1/notifications", headers={"Authorization": auth})).json()
    read_state = {i["id"]: i["read"] for i in feed["items"]}
    assert read_state[f"b:{first}"] is True

    # 공유 행이라 다른 유저의 읽음 상태에 영향을 주지 않는다.
    auth_b, user_b = await _login(client)
    await _follow(pool, user_b, brand_id)
    other = (await client.get("/v1/notifications", headers={"Authorization": auth_b})).json()
    assert all(i["read"] is False for i in other["items"])


@pytest.mark.asyncio
async def test_mark_read_all_covers_both_sources(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    for i in range(2):
        await _insert_notification(pool, user_id, "restock", product_id=i, payload={"brand": "A"})
    await _insert_brand_news(pool, brand_id)

    resp = await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={"all": True})
    assert resp.status_code == 200
    assert resp.json() == {"marked": 3, "unread_count": 0}

    feed = (await client.get("/v1/notifications", headers={"Authorization": auth})).json()
    assert all(i["read"] for i in feed["items"])

    # 워터마크가 전부 덮으므로 예외 행은 정리된다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM ai.feed_reads WHERE user_id = %s", (user_id,))
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_news_after_the_watermark_is_unread_again(client: AsyncClient, pool):
    """'전체 읽음' 이후 도착한 소식은 다시 미읽음이어야 한다 (워터마크 회귀 가드)."""
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    await _insert_brand_news(pool, brand_id)

    await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={"all": True})
    later = await _insert_brand_news(pool, brand_id, at=datetime.now(tz=UTC) + timedelta(minutes=1))

    data = (await client.get("/v1/notifications", headers={"Authorization": auth})).json()
    assert data["unread_count"] == 1
    assert {i["id"]: i["read"] for i in data["items"]}[f"b:{later}"] is False


@pytest.mark.asyncio
async def test_mark_read_ignores_unowned_and_malformed_ids(client: AsyncClient, pool):
    auth, _user_a = await _login(client)
    _auth_b, user_b = await _login(client)
    brand_id = await _insert_brand(pool, "Acme")
    await _follow(pool, user_b, brand_id)
    foreign_news = await _insert_brand_news(pool, brand_id)
    foreign_note = await _insert_notification(pool, user_b, "restock", product_id=1)

    resp = await client.patch(
        "/v1/notifications/read",
        headers={"Authorization": auth},
        json={"ids": [f"b:{foreign_news}", f"n:{foreign_note}", "garbage", "x:1", "n:notanumber"]},
    )
    assert resp.status_code == 200
    assert resp.json()["marked"] == 0


@pytest.mark.asyncio
async def test_mark_read_requires_exactly_one_selector(client: AsyncClient, pool):
    auth, _ = await _login(client)
    both = await client.patch(
        "/v1/notifications/read", headers={"Authorization": auth}, json={"all": True, "ids": ["n:1"]}
    )
    neither = await client.patch("/v1/notifications/read", headers={"Authorization": auth}, json={})
    assert both.status_code == 422
    assert neither.status_code == 422


@pytest.mark.asyncio
async def test_notifications_require_auth(client: AsyncClient):
    assert (await client.get("/v1/notifications")).status_code in (401, 403)
    assert (await client.patch("/v1/notifications/read", json={"all": True})).status_code in (401, 403)
