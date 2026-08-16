"""알림 배치 통합 테스트 — 실제 Postgres 로 SQL·멱등·워터마크를 검증한다.

순수 판정(임계치/캡/문구)은 `tests/test_notifications/` 가 담당한다. 여기서는
쿼리가 실제 스키마에서 도는지와 하루 단위 멱등이 지켜지는지만 본다.
APNs 자격증명이 없는 테스트 환경에서는 감지·적재까지만 수행된다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.services.notifications import (
    KIND_BRAND_NEW,
    KIND_BRAND_SALE,
    KIND_PRICE_DROP,
    KIND_RESTOCK,
    deliver_pending,
    run_notify_batch,
)
from app.services.push.apns import ApnsResult
from tests.test_auth.test_curation_onboarding_api import _insert_brand, _insert_product, _login


@pytest.fixture(autouse=True)
def _allow_small_catalogs(monkeypatch):
    """이 파일의 픽스처 브랜드는 상품 몇 개짜리다.

    운영 기본값(NOTIFY_BRAND_SALE_MIN_PRODUCTS=10)은 전체 브랜드 스캔에서 잡음을
    거르는 하한이라, 그대로 두면 세일 비율 판정 자체를 검증할 수 없다. 하한 게이트
    동작은 test_brand_sale_ignores_tiny_catalogs 가 실제 기본값으로 따로 본다.
    """
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "NOTIFY_BRAND_SALE_MIN_PRODUCTS", 1)


async def _follow(pool, user_id: str, brand_id: int, *, notify: bool = True) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.user_brand_picks (user_id, brand_id, notify_enabled, source) VALUES (%s, %s, %s, 'follow')",
            (user_id, brand_id, notify),
        )
        await conn.commit()


async def _aged_brand(pool, name: str, *, anchor_gender: list[str] | None = None) -> int:
    """이미 예전부터 수집돼 있던 브랜드를 만든다.

    신상 감지는 브랜드 최초 적재 +NOTIFY_BRAND_ONBOARDING_GRACE_H 이내 상품을 제외한다
    (브랜드를 처음 크롤하면 카탈로그 전체가 같은 배치로 들어와 전부 "신상" 이 되므로).
    테스트는 브랜드를 방금 만들기 때문에, 앵커 상품 1건을 60일 전으로 backdate 해
    "온보딩은 옛날에 끝난 브랜드" 상태로 만든다. 앵커는 14일 후보창 밖이라 신상으로도
    잡히지 않고, 세일 비율에는 영향을 주므로 세일 테스트에서는 쓰지 않는다.
    """
    brand_id = await _insert_brand(pool, name)
    anchor = await _insert_product(pool, brand=name, brand_node_id=brand_id, gender=anchor_gender or ["women"])
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE public.products SET created_at = now() - interval '60 days' WHERE id = %s", (anchor,))
        await conn.commit()
    return brand_id


async def _count(pool, sql: str, params: tuple = ()) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return (await cur.fetchone())[0]


async def _set_product(pool, product_id: int, **columns) -> None:
    assignments = ", ".join(f"{k} = %s" for k in columns)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"UPDATE public.products SET {assignments} WHERE id = %s",  # noqa: S608 -- 테스트 소유 컬럼명
            (*columns.values(), product_id),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_save_records_baseline_and_unsave_removes_it(client: AsyncClient, pool):
    auth, _user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)

    resp = await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    assert resp.status_code == 201
    assert await _count(pool, "SELECT count(*) FROM ai.saved_product_baseline") == 1

    resp = await client.delete(f"/v1/saves/{product_id}", headers={"Authorization": auth})
    assert resp.status_code == 204
    assert await _count(pool, "SELECT count(*) FROM ai.saved_product_baseline") == 0


@pytest.mark.asyncio
async def test_restock_and_price_drop_fire_once_per_day(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000, in_stock=False)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})

    # 품절 상태에서 찜 — 기준 재고가 false 로 남는다.
    baseline = await _count(
        pool,
        "SELECT count(*) FROM ai.saved_product_baseline WHERE baseline_in_stock = false",
    )
    assert baseline == 1

    # 아무것도 변하지 않은 배치는 알림을 만들지 않는다.
    first_run = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)  # 09:00 KST, before the 09:30 send
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run)
    assert report.detected[KIND_RESTOCK] == 0
    assert report.recorded == 0

    await _set_product(pool, product_id, in_stock=True)
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run)
    assert report.detected[KIND_RESTOCK] == 1
    assert report.recorded == 1

    # 같은 배치를 두 번 돌려도 알림이 늘지 않는다 (기준값이 현재로 갱신됐다).
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run)
    assert report.detected[KIND_RESTOCK] == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 1

    await _set_product(pool, product_id, price=80000)
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run)
    assert report.detected[KIND_PRICE_DROP] == 1

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT kind, payload FROM ai.notifications ORDER BY id")
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [KIND_RESTOCK, KIND_PRICE_DROP]
    assert rows[1][1]["drop_pct"] == 20

    # 하락 후 기준가가 내려가 같은 가격으로는 다시 울리지 않는다.
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run)
    assert report.detected[KIND_PRICE_DROP] == 0


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await _set_product(pool, product_id, price=50000)

    report = await run_notify_batch(pool, only_user=UUID(user_id), dry_run=True)

    assert report.detected[KIND_PRICE_DROP] == 1
    assert report.recorded == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notification_job_state") == 0
    assert await _count(pool, "SELECT count(*) FROM ai.saved_product_baseline WHERE baseline_price = 50000") == 0


@pytest.mark.asyncio
async def test_brand_new_products_notify_once_and_respect_gender(client: AsyncClient, pool):
    _auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()

    brand_node_id = await _aged_brand(pool, "Picked")
    await _insert_product(pool, brand="Picked", gender=["women"], brand_node_id=brand_node_id)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.user_brand_picks (user_id, brand_id) VALUES (%s, %s)",
            (user_id, brand_node_id),
        )
        await conn.commit()
    # 같은 브랜드의 남성 상품 — 성별 게이트에서 fail-closed 로 걸러진다.
    await _insert_product(pool, brand="Picked", gender=["men"], brand_node_id=brand_node_id)

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 1
    assert report.recorded == 1

    # 안티조인 — 이미 알림이 나간 (유저, 상품) 은 창 안에 남아 있어도 다시 안 잡힌다.
    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 0

    await _insert_product(pool, brand="Picked", gender=["women"], brand_node_id=brand_node_id)
    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 1


@pytest.mark.asyncio
async def test_user_gender_filled_in_later_is_still_caught(client: AsyncClient, pool):
    """유저 성별이 나중에 채워져도 잡혀야 한다 — 워터마크였다면 영구 유실될 케이스.

    원래 이 테스트는 **상품** 성별이 나중에 채워지는 경우(VLM 단일 출처 시절)를
    고정했다. gender 소유권이 크롤러로 돌아오고 `chk_products_gender_required` 가
    VALIDATE 되면서 products.gender 는 NULL 일 수 없어졌고, 그 시나리오는 도달
    불가능해졌다. 보존창이 막아주는 "나중에 해결" 케이스는 이제 유저 쪽이다 —
    user_profiles.gender 가 비어 있으면 picks CTE 에서 빠진다.
    """
    _auth, user_id = await _login(client)

    # 유저 성별 미설정 — picks 의 `up.gender IN ('female','male')` 에서 탈락한다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = NULL WHERE user_id = %s", (user_id,))
        await conn.commit()

    # 상품은 처음부터 성별을 갖고 들어온다 (크롤러 계약).
    brand_node_id = await _aged_brand(pool, "Picked")
    await _insert_product(pool, brand="Picked", gender=["women"], brand_node_id=brand_node_id)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.user_brand_picks (user_id, brand_id) VALUES (%s, %s)",
            (user_id, brand_node_id),
        )
        await conn.commit()

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 0  # 유저 성별 미해결

    # 유저가 뒤늦게 성별을 설정한다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()

    # 보존창이 살아 있으므로 다시 후보가 된다.
    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 1


@pytest.mark.asyncio
async def test_capped_overflow_carries_over_to_the_next_run(client: AsyncClient, pool, monkeypatch):
    """상한에 걸려 못 나간 신상은 버려지지 않고 다음 회차에 다시 후보가 된다.

    `report.detected` 는 "후보 총량" 이 아니라 **상한 적용 후 배치로 올라온 후보 수** 다
    (_NEW_PRODUCT_SQL `bucketed` — 유저당 최대 2×max_items 행만 가져온다). 그래서 상품 5건에
    max_items=2 면 detected 는 5 가 아니라 4 다. 이 테스트가 지키는 건 그 숫자가 아니라
    **잘린 몫이 다음 회차에 다시 올라와 결국 전부 전달된다**는 성질이다.
    """
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "NOTIFY_BRAND_NEW_MAX_ITEMS", 2)

    _auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()

    brand_node_id = await _aged_brand(pool, "Picked")
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.user_brand_picks (user_id, brand_id) VALUES (%s, %s)",
            (user_id, brand_node_id),
        )
        await conn.commit()
    for _ in range(5):
        await _insert_product(pool, brand="Picked", gender=["women"], brand_node_id=brand_node_id)

    first_run = datetime.now(tz=UTC)
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run)
    assert report.detected[KIND_BRAND_NEW] == 4  # 5건 중 상한(2×max_items)만큼만 올라온다
    assert report.selected[KIND_BRAND_NEW] == 2

    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run + timedelta(days=1))
    assert report.detected[KIND_BRAND_NEW] == 3  # 남은 3건이 다시 후보로 올라온다
    assert report.selected[KIND_BRAND_NEW] == 2

    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run + timedelta(days=2))
    assert report.detected[KIND_BRAND_NEW] == 1
    # 핵심: 잘린 몫이 유실되지 않고 세 회차에 걸쳐 5건 전부 전달됐다.
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 5


@pytest.mark.asyncio
async def test_without_apns_the_durable_outbox_keeps_the_event(client: AsyncClient, pool):
    """Credential absence cannot discard an event after its baseline advances."""
    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await _set_product(pool, product_id, price=50000)

    report = await run_notify_batch(pool, only_user=UUID(user_id))

    assert report.detected[KIND_PRICE_DROP] == 1  # 감지는 그대로 보고한다
    assert report.recorded == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages WHERE status = 'no_recipient'") == 1
    assert await _count(pool, "SELECT count(*) FROM ai.saved_product_baseline WHERE baseline_price = 50000") == 1


@pytest.mark.asyncio
async def test_brand_sale_fires_once_when_ratio_crosses_threshold(client: AsyncClient, pool):
    _auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _follow(pool, user_id, brand_id)

    # 5개 중 2개 세일 → 40% ≥ 30% 임계값.
    on_sale = [await _insert_product(pool, brand="SaleBrand", brand_node_id=brand_id) for _ in range(2)]
    for _ in range(3):
        await _insert_product(pool, brand="SaleBrand", brand_node_id=brand_id)
    for pid in on_sale:
        await _set_product(pool, pid, original_price=100000, sale_price=70000)

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_SALE] == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notifications WHERE kind = 'brand_sale'") == 1
    assert await _count(pool, "SELECT count(*) FROM ai.brand_sale_state WHERE on_sale = true") == 1
    # brand_sale 은 이제 아웃박스 다이제스트를 탄다 — 전용 카테고리 메시지가 생긴다.
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages WHERE category = 'brand_sale_digest'") == 1

    # 상태가 on_sale 로 남아 연속 세일 기간엔 재발송하지 않는다.
    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_SALE] == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notifications WHERE kind = 'brand_sale'") == 1


@pytest.mark.asyncio
async def test_brand_sale_pushes_through_the_durable_outbox(client: AsyncClient, pool, monkeypatch):
    """brand_sale 도 restock/price_drop 처럼 메시지+딜리버리를 만들어 APNs 까지 간다."""
    auth, user_id = await _login(client)
    resp = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={
            "push_token": "tok-brand-sale",
            "provider": "apns",
            "environment": "production",
            "platform": "ios",
        },
    )
    assert resp.status_code == 201

    brand_id = await _insert_brand(pool, "SaleBrand")
    await _follow(pool, user_id, brand_id)
    on_sale = [await _insert_product(pool, brand="SaleBrand", brand_node_id=brand_id) for _ in range(2)]
    for _ in range(3):
        await _insert_product(pool, brand="SaleBrand", brand_node_id=brand_id)
    for pid in on_sale:
        await _set_product(pool, pid, original_price=100000, sale_price=70000)

    batch_at = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)  # 09:00 KST — before the 11:00 브랜드 발송
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=batch_at)
    assert report.detected[KIND_BRAND_SALE] == 1
    assert report.selected[KIND_BRAND_SALE] == 1
    assert report.messages_queued == 1
    assert report.deliveries_queued == 1

    # 전용 카테고리 메시지 + 활성 iOS 기기당 딜리버리 1건.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT m.category, count(d.delivery_id)
            FROM ai.notification_messages m
            JOIN ai.notification_deliveries d ON d.message_id = m.message_id
            WHERE m.user_id = %s
            GROUP BY m.category
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
    assert rows == [("brand_sale_digest", 1)]

    # deliver_pending 가 딜리버리를 클레임해 (스텁) APNs 로 보내고 accepted 로 마감한다.
    sent: list[tuple[str, str]] = []

    async def _fake_send(self, device_token, *, title, body, **kwargs):
        sent.append((title, body))
        return ApnsResult(device_token=device_token, status=200, apns_id=kwargs.get("apns_id"))

    monkeypatch.setattr("app.services.notifications.apns.ApnsClient.send", _fake_send)

    deliver_at = datetime(2026, 8, 2, 5, 0, tzinfo=UTC)  # 14:00 KST — 발송 후, 야간 억제 전
    delivery_report = await deliver_pending(pool, now=deliver_at)
    assert delivery_report.claimed == 1
    assert delivery_report.accepted == 1
    assert len(sent) == 1
    assert "세일" in sent[0][0]  # 브랜드 세일 문구가 실려 나간다

    assert await _count(pool, "SELECT count(*) FROM ai.notification_deliveries WHERE status = 'accepted'") == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages WHERE status = 'accepted'") == 1


@pytest.mark.asyncio
async def test_brand_sale_below_threshold_does_not_fire(client: AsyncClient, pool):
    _auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _follow(pool, user_id, brand_id)

    one = await _insert_product(pool, brand="SaleBrand", brand_node_id=brand_id)
    for _ in range(4):
        await _insert_product(pool, brand="SaleBrand", brand_node_id=brand_id)
    await _set_product(pool, one, original_price=100000, sale_price=70000)  # 1/5 = 20%

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_SALE] == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notifications WHERE kind = 'brand_sale'") == 0


@pytest.mark.asyncio
async def test_brand_sale_skips_notify_disabled_follower(client: AsyncClient, pool):
    _auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _follow(pool, user_id, brand_id, notify=False)

    await _put_brand_on_sale(pool, brand_id)

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_SALE] == 0
    # 팬아웃만 막힌다 — 소식 정본은 그대로 남아 브랜드 홈에 노출된다.
    assert report.brand_sale_news == 1
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_sale' AND ended_at IS NULL") == 1


async def _put_brand_on_sale(pool, brand_id: int, *, brand: str = "SaleBrand") -> None:
    """5개 중 2개 세일 → 40% ≥ 30% 임계값."""
    on_sale = [await _insert_product(pool, brand=brand, brand_node_id=brand_id) for _ in range(2)]
    for _ in range(3):
        await _insert_product(pool, brand=brand, brand_node_id=brand_id)
    for pid in on_sale:
        await _set_product(pool, pid, original_price=100000, sale_price=70000)


@pytest.mark.asyncio
async def test_brand_news_is_written_for_brands_nobody_follows(client: AsyncClient, pool):
    """0027 의 핵심 — 팔로워가 0명인 브랜드도 소식 정본을 남긴다.

    이전 구조에선 _BRAND_SALE_SQL 이 followers CTE 로 JOIN 해서 팔로워 없는 브랜드는
    집계 대상에서 통째로 빠졌고, 그 결과 브랜드 홈이 영영 비어 있었다.
    """
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _put_brand_on_sale(pool, brand_id)

    report = await run_notify_batch(pool)

    assert report.brand_sale_news == 1
    assert report.detected[KIND_BRAND_SALE] == 0  # 팬아웃 대상 없음
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT brand_node_id, kind, payload, ended_at FROM ai.brand_news WHERE kind = 'brand_sale'")
        row = await cur.fetchone()
    assert row[0] == brand_id
    assert row[1] == "brand_sale"
    assert row[2]["max_discount_pct"] == 30
    assert row[3] is None  # 진행 중


@pytest.mark.asyncio
async def test_brand_news_opens_once_and_closes_when_sale_ends(client: AsyncClient, pool):
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _put_brand_on_sale(pool, brand_id)

    await run_notify_batch(pool)
    # 연속 세일 기간엔 소식을 다시 열지 않는다 (uq_brand_news_open + 전환 게이트).
    second = await run_notify_batch(pool)
    assert second.brand_sale_news == 0
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_sale'") == 1

    # 세일이 끝나면 진행 중이던 소식이 닫힌다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE public.products SET sale_price = NULL WHERE brand_node_id = %s", (brand_id,))
        await conn.commit()

    third = await run_notify_batch(pool)
    assert third.brand_sale_news == 1
    assert (
        await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_sale' AND ended_at IS NOT NULL") == 1
    )
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_sale'") == 1


@pytest.mark.asyncio
async def test_inbox_row_points_at_the_canonical_brand_news(client: AsyncClient, pool):
    _auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _follow(pool, user_id, brand_id)
    await _put_brand_on_sale(pool, brand_id)

    await run_notify_batch(pool, only_user=UUID(user_id))

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT n.brand_news_id, bn.id
            FROM ai.notifications n
            JOIN ai.brand_news bn ON bn.id = n.brand_news_id
            WHERE n.kind = 'brand_sale'
            """
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == row[1]


@pytest.mark.asyncio
async def test_brand_new_summary_fills_the_brand_home(client: AsyncClient, pool):
    """브랜드 홈 '신상 N개' 요약 — 팔로우도 성별도 보지 않는 브랜드 단위 집계."""
    brand_id = await _aged_brand(pool, "Acme")
    for _ in range(3):
        await _insert_product(pool, brand="Acme", brand_node_id=brand_id)

    report = await run_notify_batch(pool)
    assert report.brand_new_news == 1

    resp = await client.get(f"/v1/brands/{brand_id}")
    news = resp.json()["news"]
    assert [(n["kind"], n["text"]) for n in news] == [("brand_new", "신상 3개가 새로 들어왔어요")]
    assert news[0]["ended_at"] is None


@pytest.mark.asyncio
async def test_brand_new_summary_refreshes_count_then_closes(client: AsyncClient, pool):
    brand_id = await _aged_brand(pool, "Acme")
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id)
    await run_notify_batch(pool)

    # 개수만 바뀐 갱신은 새 소식이 아니다 — 행도 늘지 않고 카운터도 안 오른다.
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id)
    second = await run_notify_batch(pool)
    assert second.brand_new_news == 0
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_new'") == 1
    news = (await client.get(f"/v1/brands/{brand_id}")).json()["news"]
    assert news[0]["text"] == "신상 2개가 새로 들어왔어요"

    # 윈도우 밖으로 밀려나면 닫힌다 — 3주 전 신상을 계속 걸어두지 않는다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE public.products SET created_at = now() - interval '30 days' WHERE brand_node_id = %s",
            (brand_id,),
        )
        await conn.commit()
    third = await run_notify_batch(pool)
    assert third.brand_new_news == 1
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind='brand_new' AND ended_at IS NOT NULL") == 1


@pytest.mark.asyncio
async def test_brand_new_summary_stays_out_of_the_inbox(client: AsyncClient, pool):
    """알림함엔 상품별 brand_new_product 행이 따로 들어온다 — 요약까지 끼면 중복이다."""
    auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()
    brand_id = await _aged_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id, gender=["women"])

    await run_notify_batch(pool, only_user=UUID(user_id))

    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_new'") == 1
    feed = (await client.get("/v1/notifications", headers={"Authorization": auth})).json()
    # 상품별 알림 1건만. 브랜드 단위 요약은 브랜드 홈 전용이다.
    assert [i["type"] for i in feed["items"]] == ["brand_new_product"]


@pytest.mark.asyncio
async def test_brand_new_consent_off_keeps_the_inbox_row_but_not_the_retry_pool(client: AsyncClient, pool):
    """동의 off 는 영구 억제라 기록해도 안전하다 — 캡/초과분과 구분되는 지점."""
    auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()
    brand_id = await _aged_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    await _insert_product(pool, brand="Acme", brand_node_id=brand_id, gender=["women"])
    await client.patch(
        "/v1/me/notifications", json={"categories": {"release_alerts": False}}, headers={"Authorization": auth}
    )

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.selected[KIND_BRAND_NEW] == 0
    assert report.push_suppressed_consent == 1
    # 알림함엔 남는다.
    feed = (await client.get("/v1/notifications", headers={"Authorization": auth})).json()
    assert [i["type"] for i in feed["items"]] == ["brand_new_product"]
    # 푸시 경로는 없다.
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages") == 0


@pytest.mark.asyncio
async def test_brand_new_inbox_backlog_is_capped_per_run(client: AsyncClient, pool):
    """동의 off 라도 14일 창의 후보 전체를 한 배치에 쏟지 않는다."""
    from app.core.config import settings as app_settings

    auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()
    brand_id = await _aged_brand(pool, "Acme")
    await _follow(pool, user_id, brand_id)
    for _ in range(app_settings.NOTIFY_BRAND_NEW_MAX_ITEMS + 4):
        await _insert_product(pool, brand="Acme", brand_node_id=brand_id, gender=["women"])
    await client.patch(
        "/v1/me/notifications", json={"categories": {"release_alerts": False}}, headers={"Authorization": auth}
    )

    first = await run_notify_batch(pool, only_user=UUID(user_id))
    assert first.recorded == app_settings.NOTIFY_BRAND_NEW_MAX_ITEMS
    # 잘린 몫은 기록되지 않아 다음 배치에서 다시 후보가 된다.
    second = await run_notify_batch(pool, only_user=UUID(user_id))
    assert second.recorded == 4


@pytest.mark.asyncio
async def test_onboarding_import_is_not_treated_as_new_arrivals(client: AsyncClient, pool):
    """브랜드를 처음 수집하면 카탈로그 전체가 같은 배치로 들어온다 — 신상이 아니다.

    실측: 한 브랜드 775건이 2초 안에 적재됐고, 최근 14일 신상 후보의 13% 가 이런
    온보딩 통짜 적재분이었다. 걸러내지 않으면 팔로워는 구형 카탈로그를 "신상" 으로 받는다.
    """
    _auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()

    # 방금 온보딩된 브랜드 — 카탈로그 전체가 지금 적재된다.
    brand_id = await _insert_brand(pool, "JustOnboarded")
    await _follow(pool, user_id, brand_id)
    for _ in range(6):
        await _insert_product(pool, brand="JustOnboarded", brand_node_id=brand_id, gender=["women"])

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 0
    assert report.brand_new_news == 0  # 브랜드 홈 요약도 뜨지 않는다

    # 유예시간이 지난 뒤 들어온 상품부터는 진짜 신상이다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE public.products SET created_at = now() - interval '3 days' WHERE brand_node_id = %s",
            (brand_id,),
        )
        await conn.commit()
    await _insert_product(pool, brand="JustOnboarded", brand_node_id=brand_id, gender=["women"])

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 1
    assert report.brand_new_news == 1


@pytest.mark.asyncio
async def test_one_brand_does_not_take_the_whole_day_of_new_arrivals(client: AsyncClient, pool):
    """통짜 적재된 브랜드가 하루 몫을 독식하지 않는다 — 팔로우한 다른 브랜드도 나온다."""
    _auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()

    loud = await _aged_brand(pool, "Loud")
    quiet = await _aged_brand(pool, "Quiet")
    await _follow(pool, user_id, loud)
    await _follow(pool, user_id, quiet)

    # Quiet 을 먼저 적재하고 Loud 를 나중에 — 최신순으로는 Loud 가 앞을 다 차지한다.
    await _insert_product(pool, brand="Quiet", brand_node_id=quiet, gender=["women"])
    for _ in range(8):
        await _insert_product(pool, brand="Loud", brand_node_id=loud, gender=["women"])

    await run_notify_batch(pool, only_user=UUID(user_id))

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT brand_node_id, count(*) FROM ai.notifications WHERE kind = %s GROUP BY 1",
            (KIND_BRAND_NEW,),
        )
        by_brand = dict(await cur.fetchall())
    # 나중에 적재된 Loud 가 5칸을 독식하지 않고 Quiet 도 자리를 얻는다.
    assert by_brand.get(quiet) == 1
    assert by_brand.get(loud) == 4  # 브랜드당 2개 + 남은 슬롯 backfill
    assert sum(by_brand.values()) == 5


@pytest.mark.asyncio
async def test_brand_sale_ignores_tiny_catalogs(client: AsyncClient, pool, monkeypatch):
    """전체 브랜드 스캔의 잡음 하한 — 상품 2개 중 1개 할인은 비율 50% 지만 소식이 아니다."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "NOTIFY_BRAND_SALE_MIN_PRODUCTS", 10)

    brand_id = await _insert_brand(pool, "TinyBrand")
    one = await _insert_product(pool, brand="TinyBrand", brand_node_id=brand_id)
    await _insert_product(pool, brand="TinyBrand", brand_node_id=brand_id)
    await _set_product(pool, one, original_price=100000, sale_price=50000)

    report = await run_notify_batch(pool)

    assert report.brand_sale_news == 0
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_sale'") == 0
    # 상태도 쓰지 않는다 — 집계에서 아예 빠지므로 전환 판정 대상이 아니다.
    assert await _count(pool, "SELECT count(*) FROM ai.brand_sale_state") == 0


@pytest.mark.asyncio
async def test_brand_sale_respects_brand_consent(client: AsyncClient, pool):
    """동의를 꺼도 소식 정본은 남는다 — 억제되는 건 푸시뿐이다.

    0028 이후 인박스는 ai.brand_news 를 조회하므로, 게이트에 막힌 유저의
    ai.notifications 행은 아예 만들지 않는다(아웃박스 앵커가 필요 없으니까).
    유저가 소식을 못 보는 게 아니라 푸시를 안 받는 것이다.
    """
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _follow(pool, user_id, brand_id)
    await _put_brand_on_sale(pool, brand_id)

    # brand_sale 은 brand_new_product 동의를 공유한다 — 이를 끄면 푸시만 억제된다.
    await client.patch(
        "/v1/me/notifications",
        json={"categories": {"brand_new_product": False}},
        headers={"Authorization": auth},
    )

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_SALE] == 1
    assert report.selected[KIND_BRAND_SALE] == 0  # 푸시 게이트 통과분은 0
    assert report.push_suppressed_consent == 1
    # 소식 정본은 남는다 — 인박스와 브랜드 홈이 이걸 읽는다.
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_sale'") == 1
    # 아웃박스 앵커 행은 만들지 않는다.
    assert await _count(pool, "SELECT count(*) FROM ai.notifications WHERE kind = 'brand_sale'") == 0
    # 푸시 경로(메시지/딜리버리)는 만들어지지 않는다.
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages WHERE category = 'brand_sale_digest'") == 0

    # 그래도 알림함에는 보인다 — read fan-out 의 핵심.
    feed = await client.get("/v1/notifications", headers={"Authorization": auth})
    assert [i["type"] for i in feed.json()["items"]] == ["brand_sale"]


@pytest.mark.asyncio
async def test_brand_sale_respects_weekly_cap(client: AsyncClient, pool):
    """주간 캡에 걸려도 소식 정본은 남는다 — 억제되는 건 푸시뿐이다."""
    from app.services.notifications import BRAND_SALE_CATEGORY

    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "SaleBrand")
    await _follow(pool, user_id, brand_id)
    await _put_brand_on_sale(pool, brand_id)

    # 캡(주 3회)을 이미 채운 것처럼 최근 7일 내 accepted 메시지 3건을 직접 심는다.
    now = datetime.now(tz=UTC)
    async with pool.connection() as conn, conn.cursor() as cur:
        for day_offset in range(3):
            await cur.execute(
                """
                INSERT INTO ai.notification_messages
                    (user_id, category, scheduled_on, scheduled_at, expires_at, title, body, status)
                VALUES (%s, %s, %s, %s, %s, 'x', 'x', 'accepted')
                """,
                (
                    user_id,
                    BRAND_SALE_CATEGORY,
                    (now - timedelta(days=day_offset)).date(),
                    now,
                    now + timedelta(hours=1),
                ),
            )
        await conn.commit()

    report = await run_notify_batch(pool, only_user=UUID(user_id), now=now)
    assert report.detected[KIND_BRAND_SALE] == 1
    assert report.selected[KIND_BRAND_SALE] == 0  # 캡에 걸려 푸시 게이트 통과분은 0
    assert report.push_suppressed_cap == 1
    assert await _count(pool, "SELECT count(*) FROM ai.brand_news WHERE kind = 'brand_sale'") == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notifications WHERE kind = 'brand_sale'") == 0
    # 캡 이전에 심어둔 3건 외에 새 메시지가 추가되지 않는다.
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages WHERE category = 'brand_sale_digest'") == 3

    # 캡은 푸시만 막는다 — 알림함에는 그대로 보인다.
    feed = await client.get("/v1/notifications", headers={"Authorization": auth})
    assert [i["type"] for i in feed.json()["items"]] == ["brand_sale"]


@pytest.mark.asyncio
async def test_opt_out_suppresses_only_that_kind(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await _set_product(pool, product_id, price=50000)

    resp = await client.patch(
        "/v1/me/notifications",
        json={"categories": {"price_drop": False}},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    categories = resp.json()["categories"]
    assert categories["price_drop"] is False
    # 기존 3키는 그대로 (모바일 알림 화면 회귀 가드).
    assert categories["system"] is True
    assert categories["release_alerts"] is True

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_PRICE_DROP] == 1
    assert report.selected[KIND_PRICE_DROP] == 0  # 푸시 게이트 통과분 없음
    assert report.push_suppressed_consent == 1
    # 인박스 행은 남는다 — 동의를 끈 건 푸시지 소식이 아니다.
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 1
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT suppressed_reason FROM ai.notifications")
        assert (await cur.fetchone())[0] == "consent_off"
    # 푸시 경로는 만들어지지 않는다.
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages") == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notification_deliveries") == 0

    # 알림함에서 실제로 보인다.
    feed = (await client.get("/v1/notifications", headers={"Authorization": auth})).json()
    assert [i["type"] for i in feed["items"]] == ["price_drop"]
    assert feed["unread_count"] == 1


@pytest.mark.asyncio
async def test_consent_off_saved_alert_is_not_lost_when_the_baseline_advances(client: AsyncClient, pool):
    """동의 off 로 놓친 하락이 되돌릴 수 없던 문제의 회귀 가드.

    detect_save_events 는 발동과 동시에 기준가를 내리므로, 그때 인박스 행을 안 남기면
    같은 하락은 다시는 감지되지 않는다 — 나중에 동의를 켜도 영영 못 본다.
    """
    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await client.patch(
        "/v1/me/notifications", json={"categories": {"price_drop": False}}, headers={"Authorization": auth}
    )
    await _set_product(pool, product_id, price=50000)

    await run_notify_batch(pool, only_user=UUID(user_id))

    # 기준가는 전진했다 — 재감지는 기대할 수 없다.
    assert await _count(pool, "SELECT count(*) FROM ai.saved_product_baseline WHERE baseline_price = 50000") == 1
    # 그래서 그 순간 남긴 인박스 행이 유일한 기록이고, 동의를 다시 켜도 그대로 있다.
    await client.patch(
        "/v1/me/notifications", json={"categories": {"price_drop": True}}, headers={"Authorization": auth}
    )
    second = await run_notify_batch(pool, only_user=UUID(user_id))
    assert second.detected[KIND_PRICE_DROP] == 0  # 기준가 전진으로 재감지 안 됨
    feed = (await client.get("/v1/notifications", headers={"Authorization": auth})).json()
    assert [(i["type"], i["old_price"], i["new_price"]) for i in feed["items"]] == [("price_drop", 100000, 50000)]


# ── 스케일 가드 ───────────────────────────────────────────────────────────────
#
# 아웃박스 적재를 배치 전체 한 트랜잭션으로 묶으면, 유저·카테고리마다 잡는
# pg_advisory_xact_lock 이 커밋까지 누적돼 공유 락 테이블(max_locks_per_transaction ×
# max_connections)을 고갈시킨다. 아래 두 테스트는 청킹이 실제로 트랜잭션을 끊는지와,
# 끊은 결과 부분 성공이 올바른 방향(이벤트 보존)으로 남는지를 고정한다.


@pytest.mark.asyncio
async def test_outbox_commits_per_chunk_instead_of_all_or_nothing(client: AsyncClient, pool, monkeypatch):
    """청크 하나가 실패해도 앞서 커밋된 청크는 살아남는다."""
    from app.core.config import settings as app_settings
    from app.services import notifications as notif

    auth, user_id = await _login(client)
    first = await _insert_product(pool, price=100000)
    second = await _insert_product(pool, price=100000)
    for product_id in (first, second):
        await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await run_notify_batch(pool, only_user=UUID(user_id))  # 기준가 백필
    await _set_product(pool, first, price=50000)
    await _set_product(pool, second, price=50000)

    monkeypatch.setattr(app_settings, "NOTIFY_OUTBOX_CHUNK_USERS", 1)
    events = [
        notif.Event(user_id=UUID(user_id), kind=KIND_PRICE_DROP, product_id=first, payload={"price": 50000}),
    ]
    other_user = UUID("00000000-0000-0000-0000-0000000000ff")  # user_profiles 에 없음 → FK 위반
    doomed = [
        notif.Event(user_id=other_user, kind=KIND_PRICE_DROP, product_id=second, payload={"price": 50000}),
    ]

    calls: list[UUID] = []
    original = notif._persist_user_messages

    async def _tracking(cur, uid, user_events, now):
        calls.append(uid)
        return await original(cur, uid, user_events, now)

    monkeypatch.setattr(notif, "_persist_user_messages", _tracking)

    with pytest.raises(Exception):
        await notif._persist_outbox(
            pool,
            selected={UUID(user_id): events, other_user: doomed},
            inbox_only=[],
            baselines=[],
            now=datetime(2026, 8, 2, 0, 0, tzinfo=UTC),
        )

    assert calls == [UUID(user_id), other_user]
    # 첫 청크는 이미 커밋됐다 — 전체 롤백이었다면 0건이다.
    assert await _count(pool, "SELECT count(*) FROM ai.notifications WHERE kind = 'price_drop'") == 1


@pytest.mark.asyncio
async def test_events_are_committed_before_baselines_advance(client: AsyncClient, pool):
    """기준값 전진이 실패해도 이벤트는 남는다 — 유실보다 중복 쪽으로 기운다.

    반대 순서였다면 기준가가 먼저 커밋된 뒤 이벤트 적재가 깨졌을 때 그 하락은 소비된
    채 영영 재감지되지 않는다 (detect_save_events 가 발동과 동시에 기준을 전진시킨다).
    """
    from app.services import notifications as notif

    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})

    events = [
        notif.Event(user_id=UUID(user_id), kind=KIND_PRICE_DROP, product_id=product_id, payload={"price": 50000}),
    ]
    orphan = notif.BaselineWrite(
        user_id=UUID("00000000-0000-0000-0000-0000000000ff"),  # FK 위반 → 기준값 단계에서 실패
        product_id=product_id,
        price=50000.0,
        in_stock=True,
    )

    with pytest.raises(Exception):
        await notif._persist_outbox(
            pool,
            selected={UUID(user_id): events},
            inbox_only=[],
            baselines=[orphan],
            now=datetime(2026, 8, 2, 0, 0, tzinfo=UTC),
        )

    assert await _count(pool, "SELECT count(*) FROM ai.notifications WHERE kind = 'price_drop'") == 1


@pytest.mark.asyncio
async def test_delivery_cycle_drains_beyond_one_batch(client: AsyncClient, pool, monkeypatch):
    """사이클당 deliver_pending 1회면 처리량이 배치크기÷폴간격으로 묶인다.

    _drain_deliveries 는 아웃박스가 빌 때까지 반복하되 사이클 예산에서 멈춘다.
    """
    from app.core.config import settings as app_settings
    from app.workers.notification_worker import _drain_deliveries

    auth, user_id = await _login(client)
    for _ in range(3):
        product_id = await _insert_product(pool, price=100000)
        await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await run_notify_batch(pool, only_user=UUID(user_id))
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT product_id FROM ai.saved_product_baseline WHERE user_id = %s", (user_id,))
        saved = [r[0] for r in await cur.fetchall()]
    for product_id in saved:
        await _set_product(pool, product_id, price=50000)

    # 유저 한 명당 다이제스트 1건이라 딜리버리를 늘리려면 기기를 늘린다.
    async with pool.connection() as conn, conn.cursor() as cur:
        for index in range(3):
            await cur.execute(
                """
                INSERT INTO ai.devices (user_id, push_token, platform, provider, environment, topic,
                                        status, last_seen_at)
                VALUES (%s, %s, 'ios', 'apns', 'production', 'com.kikoai.app', 'active', now())
                """,
                (user_id, f"drain-token-{index}"),
            )
        await conn.commit()

    await run_notify_batch(pool, only_user=UUID(user_id), now=datetime(2026, 8, 2, 0, 0, tzinfo=UTC))
    queued = await _count(pool, "SELECT count(*) FROM ai.notification_deliveries WHERE status = 'pending'")
    assert queued >= 3

    async def _fake_send(self, device_token, **kwargs):
        return ApnsResult(device_token=device_token, status=200, apns_id=kwargs.get("apns_id"))

    monkeypatch.setattr("app.services.notifications.apns.ApnsClient.send", _fake_send)
    # 배치 크기를 1로 낮춰도 한 사이클에서 전부 소진돼야 한다.
    monkeypatch.setattr(app_settings, "NOTIFY_DELIVERY_BATCH_SIZE", 1)

    report = await _drain_deliveries(pool, now=datetime(2026, 8, 2, 5, 0, tzinfo=UTC))
    assert report.claimed == queued
    assert report.accepted == queued
    assert await _count(pool, "SELECT count(*) FROM ai.notification_deliveries WHERE status = 'pending'") == 0


@pytest.mark.asyncio
async def test_delivery_cycle_stops_at_its_budget(client: AsyncClient, pool, monkeypatch):
    """재시도가 계속 due 로 돌아와도 사이클이 굶지 않는다."""
    from app.core.config import settings as app_settings
    from app.workers.notification_worker import _drain_deliveries

    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await run_notify_batch(pool, only_user=UUID(user_id))
    await _set_product(pool, product_id, price=50000)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.devices (user_id, push_token, platform, provider, environment, topic,
                                    status, last_seen_at)
            VALUES (%s, 'budget-token', 'ios', 'apns', 'production', 'com.kikoai.app', 'active', now())
            """,
            (user_id,),
        )
        await conn.commit()
    await run_notify_batch(pool, only_user=UUID(user_id), now=datetime(2026, 8, 2, 0, 0, tzinfo=UTC))

    async def _fake_send(self, device_token, **kwargs):
        return ApnsResult(device_token=device_token, status=200, apns_id=kwargs.get("apns_id"))

    monkeypatch.setattr("app.services.notifications.apns.ApnsClient.send", _fake_send)
    monkeypatch.setattr(app_settings, "NOTIFY_DELIVERY_BATCH_SIZE", 1)
    monkeypatch.setattr(app_settings, "NOTIFY_DELIVERY_MAX_PER_CYCLE", 1)

    report = await _drain_deliveries(pool, now=datetime(2026, 8, 2, 5, 0, tzinfo=UTC))
    assert report.claimed == 1  # 예산에서 정확히 멈춘다


@pytest.mark.asyncio
async def test_expiry_sweep_uses_an_index_instead_of_scanning_every_message(client: AsyncClient, pool):
    """만료 스윕은 30초마다 돈다 — 누적 메시지 전체를 훑으면 안 된다 (migration 0032).

    메시지 행은 유저·카테고리·날짜당 1건씩 영구히 쌓인다. 인덱스가 없으면 스윕 비용이
    누적 행 수에 정비례하고, 유저 1만이면 연 100만 행 규모가 된다.
    """
    _auth, user_id = await _login(client)

    # 플래너가 인덱스를 고를 만큼 행을 채운다. 유니크 키가 (user_id, category, scheduled_on)
    # 이라 날짜를 흘려 20,000건을 한 문장으로 만든다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.notification_messages
                (user_id, category, scheduled_on, scheduled_at, expires_at, title, body, status)
            SELECT %s, 'saved_product_digest',
                   DATE '2020-01-01' + i,
                   (DATE '2020-01-01' + i)::timestamptz,
                   (DATE '2020-01-01' + i)::timestamptz + interval '12 hours',
                   't', 'b',
                   -- 대부분은 이미 마감된 행이라 부분 인덱스에서 빠진다.
                   CASE WHEN mod(i, 1000) = 0 THEN 'pending' ELSE 'accepted' END
            FROM generate_series(1, 20000) AS i
            """,
            (user_id,),
        )
        await cur.execute("ANALYZE ai.notification_messages")
        await conn.commit()

        await cur.execute(
            """
            EXPLAIN UPDATE ai.notification_messages m
            SET status = 'expired', completed_at = now()
            WHERE m.expires_at <= now()
              AND m.status IN ('pending', 'processing')
              AND NOT EXISTS (
                  SELECT 1 FROM ai.notification_deliveries d
                  WHERE d.message_id = m.message_id
                    AND d.status IN ('pending', 'retry', 'processing')
              )
            """
        )
        plan = "\n".join(row[0] for row in await cur.fetchall())
        await conn.rollback()

    assert "idx_notification_messages_expiry" in plan, plan
    assert "Seq Scan on notification_messages" not in plan, plan


# ── 보존 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retention_dry_run_counts_without_deleting(client: AsyncClient, pool, monkeypatch):
    """되돌릴 수 없는 삭제라 dry-run 이 규모 확인의 정식 경로다."""
    from app.core.config import settings as app_settings
    from app.services.notifications import purge_expired_notifications

    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await _set_product(pool, product_id, price=50000)
    await run_notify_batch(pool, only_user=UUID(user_id))
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.notifications SET created_at = now() - interval '400 days'")
        await cur.execute("UPDATE ai.notification_messages SET created_at = now() - interval '400 days'")
        await conn.commit()

    monkeypatch.setattr(app_settings, "NOTIFY_RETENTION_FEED_D", 180)
    report = await purge_expired_notifications(pool, dry_run=True)

    assert report.dry_run is True
    assert report.notifications == 1
    assert report.messages == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 1  # 아무것도 지우지 않았다
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages") == 1


@pytest.mark.asyncio
async def test_retention_keeps_rows_inside_the_window(client: AsyncClient, pool, monkeypatch):
    from app.core.config import settings as app_settings
    from app.services.notifications import purge_expired_notifications

    auth, user_id = await _login(client)
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await _set_product(pool, product_id, price=50000)
    await run_notify_batch(pool, only_user=UUID(user_id))

    monkeypatch.setattr(app_settings, "NOTIFY_RETENTION_FEED_D", 180)
    report = await purge_expired_notifications(pool)

    assert report.notifications == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 1


@pytest.mark.asyncio
async def test_retention_cascades_to_deliveries_and_message_events(client: AsyncClient, pool, monkeypatch):
    """메시지를 지우면 딜리버리·연결행이 FK 캐스케이드로 함께 사라진다."""
    from app.core.config import settings as app_settings
    from app.services.notifications import purge_expired_notifications

    auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.devices (user_id, push_token, platform, provider, environment, topic,
                                    status, last_seen_at)
            VALUES (%s, 'retention-token', 'ios', 'apns', 'production', 'com.kikoai.app', 'active', now())
            """,
            (user_id,),
        )
        await conn.commit()
    product_id = await _insert_product(pool, price=100000)
    await client.post("/v1/saves", json={"product_id": str(product_id)}, headers={"Authorization": auth})
    await _set_product(pool, product_id, price=50000)
    await run_notify_batch(pool, only_user=UUID(user_id), now=datetime(2026, 8, 2, 0, 0, tzinfo=UTC))

    assert await _count(pool, "SELECT count(*) FROM ai.notification_deliveries") == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notification_message_events") == 1

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.notification_messages SET created_at = now() - interval '400 days'")
        await cur.execute("UPDATE ai.notifications SET created_at = now() - interval '400 days'")
        await conn.commit()
    monkeypatch.setattr(app_settings, "NOTIFY_RETENTION_FEED_D", 180)
    await purge_expired_notifications(pool)

    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages") == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notification_deliveries") == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notification_message_events") == 0
    # 기기는 남는다 — device_id 는 ON DELETE SET NULL 이고 딜리버리만 사라진다.
    assert await _count(pool, "SELECT count(*) FROM ai.devices") == 1


@pytest.mark.asyncio
async def test_retention_does_not_resurrect_brand_new_notifications(client: AsyncClient, pool, monkeypatch):
    """brand_new_product 의 '평생 1회' 는 안티조인 행이 사라져도 유지된다.

    후보 조건이 `products.created_at > now - NOTIFY_NEW_PRODUCT_WINDOW_D` 라, 지울 나이가
    된 알림이 가리키는 상품은 이미 창 밖이다. 이 성질이 깨지면 보존 잡이 옛날 상품을
    전부 신상으로 재발송한다 — 이 테스트가 그 회귀 가드다.
    """
    from app.core.config import settings as app_settings
    from app.services.notifications import purge_expired_notifications

    _auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()
    brand_node_id = await _aged_brand(pool, "Picked")
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.user_brand_picks (user_id, brand_id) VALUES (%s, %s)",
            (user_id, brand_node_id),
        )
        await conn.commit()
    product_id = await _insert_product(pool, brand="Picked", gender=["women"], brand_node_id=brand_node_id)

    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 1

    # 알림과 상품을 함께 나이 먹인다 — 보존 잡이 도는 시점의 실제 상태다.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.notifications SET created_at = now() - interval '400 days'")
        await cur.execute(
            "UPDATE public.products SET created_at = now() - interval '400 days' WHERE id = %s",
            (product_id,),
        )
        await conn.commit()

    monkeypatch.setattr(app_settings, "NOTIFY_RETENTION_FEED_D", 180)
    purged = await purge_expired_notifications(pool)
    assert purged.notifications == 1
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 0

    # 안티조인 행이 사라졌지만 상품이 14일 창 밖이라 다시 후보가 되지 않는다.
    report = await run_notify_batch(pool, only_user=UUID(user_id))
    assert report.detected[KIND_BRAND_NEW] == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 0


@pytest.mark.asyncio
async def test_retention_never_closes_a_live_brand_news(client: AsyncClient, pool, monkeypatch):
    """진행 중(ended_at IS NULL) 소식은 아무리 오래돼도 지우지 않는다.

    브랜드 홈과 알림함이 지금 노출하고 있는 행이라 지우면 화면에서 사라진다.
    """
    from app.core.config import settings as app_settings
    from app.services.notifications import purge_expired_notifications

    brand_id = await _insert_brand(pool, "LiveNews")
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.brand_news (brand_node_id, kind, payload, started_at, ended_at, created_at)
            VALUES (%s, 'brand_sale', '{}'::jsonb, now() - interval '400 days', NULL, now() - interval '400 days'),
                   (%s, 'brand_new',  '{}'::jsonb, now() - interval '400 days',
                    now() - interval '399 days', now() - interval '400 days')
            """,
            (brand_id, brand_id),
        )
        await conn.commit()

    monkeypatch.setattr(app_settings, "NOTIFY_RETENTION_FEED_D", 180)
    report = await purge_expired_notifications(pool)

    assert report.brand_news == 1  # 끝난 소식만
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT kind, ended_at IS NULL FROM ai.brand_news")
        assert await cur.fetchall() == [("brand_sale", True)]


@pytest.mark.asyncio
async def test_retention_deletes_in_batches(client: AsyncClient, pool, monkeypatch):
    """한 문장으로 수백만 행을 지우면 아웃박스가 겪은 긴 트랜잭션 문제가 그대로 난다."""
    from app.core.config import settings as app_settings
    from app.services.notifications import purge_expired_notifications

    _auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.notification_messages
                (user_id, category, scheduled_on, scheduled_at, expires_at, title, body, status, created_at)
            SELECT %s, 'saved_product_digest', DATE '2020-01-01' + i,
                   (DATE '2020-01-01' + i)::timestamptz,
                   (DATE '2020-01-01' + i)::timestamptz + interval '12 hours',
                   't', 'b', 'accepted', now() - interval '400 days'
            FROM generate_series(1, 250) AS i
            """,
            (user_id,),
        )
        await conn.commit()

    monkeypatch.setattr(app_settings, "NOTIFY_RETENTION_FEED_D", 180)
    monkeypatch.setattr(app_settings, "NOTIFY_RETENTION_BATCH", 100)  # 3 배치 + 잔여
    report = await purge_expired_notifications(pool)

    assert report.messages == 250
    assert await _count(pool, "SELECT count(*) FROM ai.notification_messages") == 0
