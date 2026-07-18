"""GET /v1/brands/search · POST /v1/onboarding · GET /v1/curation integration tests."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient


async def _login(client: AsyncClient) -> tuple[str, str]:
    from app.core.social_auth.google import GoogleClaims

    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})
    data = resp.json()
    return f"Bearer {data['access_token']}", data["user_id"]


async def _insert_brand(pool, name: str, node_id: int | None = None, normalized: str | None = None) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.brand_nodes (brand_name, brand_name_normalized, primary_style_node_id)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (name, normalized, node_id),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def _insert_product(
    pool,
    *,
    brand: str = "TestBrand",
    price: float | None = 50.0,
    gender: list[str] | None = None,
    brand_node_id: int | None = None,
    in_stock: bool = True,
) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.products (brand, name, price, gender, image_url, product_url, brand_node_id, in_stock)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                brand,
                "Test Product",
                price,
                gender or ["women"],
                "https://img.test/i.jpg",
                f"https://shop.test/{uuid4()}",
                brand_node_id,
                in_stock,
            ),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def _insert_section(pool, *, section_id: str, gender: str, product_ids: list[int], **kw) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.curation_sections
                (section_id, gender, slot_type, title, subtitle, product_ids, sort_order, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                section_id,
                gender,
                kw.get("slot_type", "auto"),
                kw.get("title", "섹션"),
                kw.get("subtitle"),
                product_ids,
                kw.get("sort_order", 0),
                kw.get("is_active", True),
            ),
        )
        await conn.commit()


# ── GET /v1/brands/search ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_search_matches_and_ranks_normalized_prefix(client: AsyncClient, pool):
    await _insert_brand(pool, "1017 ALYX 9SM", node_id=5, normalized="1017alyx9sm")
    await _insert_brand(pool, "Alyxia Studio", node_id=7, normalized="alyxiastudio")
    resp = await client.get("/v1/brands/search", params={"q": "alyx"})
    assert resp.status_code == 200
    brands = resp.json()["brands"]
    names = [b["name"] for b in brands]
    assert "1017 ALYX 9SM" in names and "Alyxia Studio" in names
    # 정규화 prefix 매치('alyxiastudio')가 substring 매치보다 먼저
    assert names[0] == "Alyxia Studio"
    assert next(b for b in brands if b["name"] == "1017 ALYX 9SM")["node_id"] == 5


@pytest.mark.asyncio
async def test_brand_search_limit_8(client: AsyncClient, pool):
    for i in range(10):
        await _insert_brand(pool, f"Limitless {i}", normalized=f"limitless{i}")
    resp = await client.get("/v1/brands/search", params={"q": "limitless"})
    assert len(resp.json()["brands"]) == 8


# ── POST /v1/onboarding ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onboarding_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/onboarding", json={"gender": "women", "selected_brand_ids": []})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_onboarding_saves_gender_and_style_brand_pairs(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Pickable", node_id=7)
    nodeless = await _insert_brand(pool, "Nodeless")  # primary_style_node_id 미매핑 브랜드
    resp = await client.post(
        "/v1/onboarding",
        json={"gender": "women", "selected_brand_ids": [brand_id, nodeless, 99999999]},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["saved_brand_ids"] == [brand_id, nodeless]  # 존재하지 않는 id는 제외

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT gender, onboarded_at FROM ai.user_profiles WHERE user_id = %s", (user_id,))
        gender, onboarded_at = await cur.fetchone()
        await cur.execute(
            "SELECT brand_id, style_node_id FROM ai.user_brand_picks WHERE user_id = %s ORDER BY brand_id",
            (user_id,),
        )
        picks = {r[0]: r[1] for r in await cur.fetchall()}
    assert gender == "female"  # 앱 'women' → DB 'female' 매핑
    assert onboarded_at is not None
    # {스타일, 브랜드} 쌍 저장 — 노드는 primary_style_node_id 서버 유도, 미매핑은 None
    assert picks == {brand_id: 7, nodeless: None}


@pytest.mark.asyncio
async def test_onboarding_relogin_replaces_picks(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    b1 = await _insert_brand(pool, "FirstPick")
    b2 = await _insert_brand(pool, "SecondPick")
    await client.post(
        "/v1/onboarding", json={"gender": "men", "selected_brand_ids": [b1]}, headers={"Authorization": auth}
    )
    resp = await client.post(
        "/v1/onboarding", json={"gender": "men", "selected_brand_ids": [b2]}, headers={"Authorization": auth}
    )
    assert resp.json()["saved_brand_ids"] == [b2]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT brand_id FROM ai.user_brand_picks WHERE user_id = %s", (user_id,))
        picks = [r[0] for r in await cur.fetchall()]
        await cur.execute("SELECT gender FROM ai.user_profiles WHERE user_id = %s", (user_id,))
        (gender,) = await cur.fetchone()
    assert picks == [b2]  # 재확인 = 전체 교체
    assert gender == "male"


# ── GET /v1/curation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_curation_guest_requires_gender_param(client: AsyncClient):
    resp = await client.get("/v1/curation")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_curation_guest_empty_sections_ok_with_women_chips(client: AsyncClient):
    resp = await client.get("/v1/curation", params={"gender": "women"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["gender"] == "women"
    assert data["sections"] == []  # 빈 구좌여도 200 (클라 캐시 폴백)
    chips = data["chips"]
    assert len(chips) == 5
    assert {c["id"] for c in chips} == {"chip-w1", "chip-w2", "chip-w3", "chip-w4", "chip-w5"}


@pytest.mark.asyncio
async def test_curation_men_chips_empty_until_goldenset(client: AsyncClient):
    resp = await client.get("/v1/curation", params={"gender": "men"})
    assert resp.json()["chips"] == []


@pytest.mark.asyncio
async def test_curation_sections_hydrate_in_order_and_filter(client: AsyncClient, pool):
    p1 = await _insert_product(pool, brand="A")
    p2 = await _insert_product(pool, brand="B")
    p_out = await _insert_product(pool, brand="C", in_stock=False)
    await _insert_section(pool, section_id="popular", gender="women", product_ids=[p2, p_out, p1], sort_order=1)
    await _insert_section(pool, section_id="hidden", gender="women", product_ids=[p1], is_active=False)
    await _insert_section(pool, section_id="men-only", gender="men", product_ids=[p1])

    resp = await client.get("/v1/curation", params={"gender": "women"})
    sections = resp.json()["sections"]
    assert [s["id"] for s in sections] == ["popular"]  # inactive/타 gender 제외
    ids = [p["product_id"] for p in sections[0]["products"]]
    assert ids == [p2, p1]  # product_ids 순서 보존, 품절 제외


@pytest.mark.asyncio
async def test_curation_profile_gender_overrides_param(client: AsyncClient, pool):
    auth, _user_id = await _login(client)
    await client.post(
        "/v1/onboarding", json={"gender": "men", "selected_brand_ids": []}, headers={"Authorization": auth}
    )
    resp = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    assert resp.json()["gender"] == "men"  # 로그인은 프로필 우선


@pytest.mark.asyncio
async def test_curation_invalid_token_treated_as_guest(client: AsyncClient):
    resp = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 200  # 만료/무효 토큰이 메인 화면을 막지 않는다


# ── curation refresher (auto sections) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_auto_sections_popular_and_under100(client: AsyncClient, pool):
    from app.services.curation_refresh import refresh_auto_sections

    _auth, user_id = await _login(client)
    cheap = await _insert_product(pool, brand="Cheap", price=42.0)
    pricey = await _insert_product(pool, brand="Pricey", price=250.0)
    viewed = await _insert_product(pool, brand="Hot", price=120.0)
    async with pool.connection() as conn, conn.cursor() as cur:
        for _ in range(3):
            await cur.execute(
                "INSERT INTO ai.product_views (user_id, product_id, session_id) VALUES (%s, %s, %s)",
                (user_id, viewed, str(uuid4())),
            )
        await conn.commit()

    # usd_krw=1.0 로 고정 — 실 환율(수백~수천 KRW/USD)을 쓰면 위 테스트용 가격들이
    # 전부 임계값 아래로 들어가 버려 under-100 분리 검증이 무의미해진다. 실시간 FX
    # 조회 자체(네트워크 호출)를 이 단위 테스트 범위 밖으로 둬 결정론적으로 유지.
    with patch("app.services.curation_refresh._fetch_usd_to_krw", return_value=1.0):
        written = await refresh_auto_sections(pool)
    assert written >= 2  # women: popular + under-100 (trending은 신호 없음 → skip)

    resp = await client.get("/v1/curation", params={"gender": "women"})
    sections = {s["id"]: s for s in resp.json()["sections"]}
    assert [p["product_id"] for p in sections["popular"]["products"]] == [viewed]
    under = [p["product_id"] for p in sections["under-100"]["products"]]
    assert cheap in under and pricey not in under and viewed not in under
