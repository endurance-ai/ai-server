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
    KIND_PRICE_DROP,
    KIND_RESTOCK,
    run_notify_batch,
)
from tests.test_auth.test_curation_onboarding_api import _insert_product, _login


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

    women_product = await _insert_product(pool, brand="Picked", gender=["women"])
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT brand_node_id FROM public.products WHERE id = %s", (women_product,))
        brand_node_id = (await cur.fetchone())[0]
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
    product_id = await _insert_product(pool, brand="Picked", gender=["women"])
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT brand_node_id FROM public.products WHERE id = %s", (product_id,))
        brand_node_id = (await cur.fetchone())[0]
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
    """상한에 걸려 못 나간 신상은 버려지지 않고 다음 회차에 다시 후보가 된다."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "NOTIFY_BRAND_NEW_MAX_ITEMS", 2)

    _auth, user_id = await _login(client)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.user_profiles SET gender = 'female' WHERE user_id = %s", (user_id,))
        await conn.commit()

    first = await _insert_product(pool, brand="Picked", gender=["women"])
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT brand_node_id FROM public.products WHERE id = %s", (first,))
        brand_node_id = (await cur.fetchone())[0]
        await cur.execute(
            "INSERT INTO ai.user_brand_picks (user_id, brand_id) VALUES (%s, %s)",
            (user_id, brand_node_id),
        )
        await conn.commit()
    for _ in range(4):
        await _insert_product(pool, brand="Picked", gender=["women"], brand_node_id=brand_node_id)

    first_run = datetime.now(tz=UTC)
    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run)
    assert report.detected[KIND_BRAND_NEW] == 5
    assert report.selected[KIND_BRAND_NEW] == 2

    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run + timedelta(days=1))
    assert report.detected[KIND_BRAND_NEW] == 3  # 남은 3건이 다시 후보로 올라온다
    assert report.selected[KIND_BRAND_NEW] == 2

    report = await run_notify_batch(pool, only_user=UUID(user_id), now=first_run + timedelta(days=2))
    assert report.detected[KIND_BRAND_NEW] == 1
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
    assert report.selected[KIND_PRICE_DROP] == 0
    assert await _count(pool, "SELECT count(*) FROM ai.notifications") == 0
