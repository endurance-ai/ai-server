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


async def _insert_brand(
    pool,
    name: str,
    node_id: int | None = None,
    normalized: str | None = None,
    *,
    description: str | None = None,
    homepage_url: str | None = None,
) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.brand_nodes (brand_name, brand_name_normalized, primary_style_node_id, description, wiki)
            VALUES (
                %s, %s, %s, %s,
                jsonb_strip_nulls(jsonb_build_object('homepage_url', %s::text))
            )
            RETURNING id
            """,
            (name, normalized, node_id, description, homepage_url),
        )
        row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def _insert_product(
    pool,
    *,
    brand: str = "TestBrand",
    price: float | None = 50000.0,
    original_price: float | None = None,
    sale_price: float | None = None,
    gender: list[str] | None = None,
    brand_node_id: int | None = None,
    in_stock: bool = True,
    name: str = "Test Product",
    category: str = "tops",
    review_count: int = 0,
) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        if brand_node_id is None:
            await cur.execute(
                """
                INSERT INTO public.brand_nodes (brand_name, wiki, gender_scope)
                VALUES (%s, '{"status":"ok"}'::jsonb, %s)
                RETURNING id
                """,
                (brand, gender or ["women"]),
            )
            brand_node_id = (await cur.fetchone())[0]
        await cur.execute(
            """
            INSERT INTO public.products
                (brand, name, price, original_price, sale_price, gender,
                 image_url, product_url, brand_node_id, in_stock, category, review_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                brand,
                name,
                price,
                original_price,
                sale_price,
                gender or ["women"],
                "https://img.test/i.jpg",
                f"https://shop.test/{uuid4()}",
                brand_node_id,
                in_stock,
                category,
                review_count,
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
                (section_id, gender, slot_type, display_type, title, subtitle,
                 product_ids, sort_order, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (section_id, gender) DO UPDATE SET
                slot_type = EXCLUDED.slot_type,
                display_type = EXCLUDED.display_type,
                title = EXCLUDED.title,
                subtitle = EXCLUDED.subtitle,
                product_ids = EXCLUDED.product_ids,
                sort_order = EXCLUDED.sort_order,
                is_active = EXCLUDED.is_active
            """,
            (
                section_id,
                gender,
                kw.get("slot_type", "auto"),
                kw.get("display_type", "default"),
                kw.get("title", "섹션"),
                kw.get("subtitle"),
                product_ids,
                kw.get("sort_order", 0),
                kw.get("is_active", True),
            ),
        )
        await conn.commit()


async def _insert_auto_sections(pool, gender: str = "women") -> None:
    for sort_order, section_id in enumerate(("trending-search", "under-100"), start=1):
        await _insert_section(
            pool,
            section_id=section_id,
            gender=gender,
            product_ids=[],
            sort_order=sort_order,
        )


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
    assert resp.headers["cache-control"] == "private, no-store"
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
async def test_curation_sections_randomize_products_and_filter(client: AsyncClient, pool):
    p1 = await _insert_product(pool, brand="A")
    p2 = await _insert_product(pool, brand="B", original_price=70000, sale_price=45000)
    p_out = await _insert_product(pool, brand="C", in_stock=False)
    p_men = await _insert_product(pool, brand="D", gender=["men"])
    await _insert_section(
        pool,
        section_id="trending-search",
        gender="women",
        product_ids=[p2, p_out, p_men, p1],
        sort_order=1,
    )
    await _insert_section(pool, section_id="hidden", gender="women", product_ids=[p1], is_active=False)
    await _insert_section(pool, section_id="men-only", gender="men", product_ids=[p1])

    with patch("app.api.curation.shuffle", side_effect=lambda products: products.reverse()) as shuffled:
        resp = await client.get("/v1/curation", params={"gender": "women"})
    sections = resp.json()["sections"]
    assert [s["id"] for s in sections] == ["trending-search"]  # inactive/타 gender 제외
    products = sections[0]["products"]
    ids = [p["product_id"] for p in products]
    assert ids == [p1, p2]  # 품절·반대 성별 제외 후 API 응답 직전 섞기
    shuffled.assert_called_once()
    by_id = {product["product_id"]: product for product in products}
    assert by_id[p2]["original_price"] == 70000.0
    assert by_id[p2]["sale_price"] == 45000.0
    assert by_id[p1]["original_price"] is None
    assert by_id[p1]["sale_price"] is None


@pytest.mark.asyncio
async def test_curation_caps_editorial_section_at_one_hundred(client: AsyncClient, pool):
    """저장된 editorial 목록도 API에서는 앞 100개까지만 반환한다."""
    product_ids = [await _insert_product(pool, brand=f"Brand{i}", name=f"P{i}") for i in range(105)]
    await _insert_section(
        pool,
        section_id="editorial-long",
        gender="women",
        product_ids=product_ids,
        slot_type="editorial",
        sort_order=1,
    )

    resp = await client.get("/v1/curation", params={"gender": "women"})
    products = resp.json()["sections"][0]["products"]
    assert len(products) == 100
    assert {p["product_id"] for p in products} == set(product_ids[:100])


@pytest.mark.asyncio
async def test_curation_profile_gender_overrides_param(client: AsyncClient, pool):
    auth, _user_id = await _login(client)
    await client.post(
        "/v1/onboarding", json={"gender": "men", "selected_brand_ids": []}, headers={"Authorization": auth}
    )
    resp = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    assert resp.json()["gender"] == "men"  # 로그인은 프로필 우선


@pytest.mark.asyncio
async def test_curation_all_slots_exclude_opposite_gender_and_include_unisex(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    await client.post(
        "/v1/onboarding",
        json={"gender": "women", "selected_brand_ids": []},
        headers={"Authorization": auth},
    )

    products: dict[str, tuple[int, int]] = {}
    for gender in ("women", "men", "unisex"):
        brand_id = await _insert_brand(pool, f"{gender} Brand", node_id=17)
        product_id = await _insert_product(
            pool,
            brand=f"{gender} Brand",
            brand_node_id=brand_id,
            gender=[gender],
            original_price=100000,
            sale_price=70000,
        )
        products[gender] = (product_id, brand_id)

    all_product_ids = [product_id for product_id, _brand_id in products.values()]
    await _insert_section(
        pool,
        section_id="trending-search",
        gender="women",
        product_ids=all_product_ids,
        sort_order=1,
    )
    for sort_order, section_id in enumerate(
        ("recently-viewed", "saved-on-sale", "taste-picks", "favorite-brands"),
        start=2,
    ):
        await _insert_section(pool, section_id=section_id, gender="women", product_ids=[], sort_order=sort_order)

    async with pool.connection() as conn, conn.cursor() as cur:
        for product_id, brand_id in products.values():
            await cur.execute(
                "INSERT INTO ai.product_views (user_id, product_id, session_id) VALUES (%s, %s, %s)",
                (user_id, product_id, str(uuid4())),
            )
            await cur.execute(
                "INSERT INTO ai.saves (user_id, product_id) VALUES (%s, %s)",
                (user_id, str(product_id)),
            )
            await cur.execute(
                """
                INSERT INTO ai.user_brand_picks (user_id, brand_id, style_node_id, source)
                VALUES (%s, %s, 17, 'follow')
                """,
                (user_id, brand_id),
            )
        await cur.execute(
            """
            INSERT INTO ai.user_style_scores (user_id, style_node_id, score, signal_count)
            VALUES (%s, 17, 5, 1)
            """,
            (user_id,),
        )
        await conn.commit()

    response = await client.get(
        "/v1/curation",
        params={"gender": "men"},
        headers={"Authorization": auth},
    )
    assert response.status_code == 200
    assert response.json()["gender"] == "women"
    sections = {section["id"]: section for section in response.json()["sections"]}
    expected = {products["women"][0], products["unisex"][0]}
    for section_id in ("trending-search", "recently-viewed", "saved-on-sale", "taste-picks", "favorite-brands"):
        assert {product["product_id"] for product in sections[section_id]["products"]} == expected


@pytest.mark.asyncio
async def test_curation_invalid_token_treated_as_guest(client: AsyncClient):
    resp = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 200  # 만료/무효 토큰이 메인 화면을 막지 않는다


@pytest.mark.asyncio
async def test_recently_viewed_is_conditional_authenticated_and_newest_first(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    older = await _insert_product(pool, brand="Older")
    newer = await _insert_product(pool, brand="Newer")
    await _insert_section(pool, section_id="recently-viewed", gender="women", product_ids=[], sort_order=4)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.product_views (user_id, product_id, session_id, viewed_at)
            VALUES (%s, %s, %s, now() - interval '1 day'),
                   (%s, %s, %s, now())
            """,
            (user_id, older, str(uuid4()), user_id, newer, str(uuid4())),
        )
        await conn.commit()

    guest = await client.get("/v1/curation", params={"gender": "women"})
    assert "recently-viewed" not in {section["id"] for section in guest.json()["sections"]}

    response = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    section = {item["id"]: item for item in response.json()["sections"]}["recently-viewed"]
    assert [product["product_id"] for product in section["products"]] == [newer, older]


@pytest.mark.asyncio
async def test_recently_viewed_caps_at_one_hundred_and_keeps_newest(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    product_ids = [await _insert_product(pool, brand=f"Viewed {index}") for index in range(105)]
    await _insert_section(pool, section_id="recently-viewed", gender="women", product_ids=[], sort_order=4)

    async with pool.connection() as conn, conn.cursor() as cur:
        for index, product_id in enumerate(product_ids):
            await cur.execute(
                """
                INSERT INTO ai.product_views (user_id, product_id, session_id, viewed_at)
                VALUES (%s, %s, %s, now() - make_interval(secs => %s))
                """,
                (user_id, product_id, str(uuid4()), 105 - index),
            )
        await conn.commit()

    response = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    section = {item["id"]: item for item in response.json()["sections"]}["recently-viewed"]
    assert [product["product_id"] for product in section["products"]] == list(reversed(product_ids[5:]))


@pytest.mark.asyncio
async def test_saved_on_sale_only_contains_discounted_saves(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    discounted = await _insert_product(pool, brand="Sale", original_price=100000, sale_price=70000)
    full_price = await _insert_product(pool, brand="Full", original_price=100000, sale_price=100000)
    not_saved = await _insert_product(pool, brand="Other", original_price=100000, sale_price=50000)
    await _insert_section(pool, section_id="recently-viewed", gender="women", product_ids=[], sort_order=4)
    await _insert_section(pool, section_id="saved-on-sale", gender="women", product_ids=[], sort_order=5)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.saves (user_id, product_id) VALUES (%s, %s), (%s, %s)",
            (user_id, str(discounted), user_id, str(full_price)),
        )
        await cur.execute(
            "INSERT INTO ai.product_views (user_id, product_id, session_id) VALUES (%s, %s, %s)",
            (user_id, discounted, str(uuid4())),
        )
        await conn.commit()

    response = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    sections = {item["id"]: item for item in response.json()["sections"]}
    assert {product["product_id"] for product in sections["recently-viewed"]["products"]} == {discounted}
    section = sections["saved-on-sale"]
    ids = {product["product_id"] for product in section["products"]}
    assert ids == {discounted}
    assert not_saved not in ids


@pytest.mark.asyncio
async def test_taste_picks_requires_positive_taste_and_matches_style(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    liked_brand = await _insert_brand(pool, "Liked Style", node_id=17)
    other_brand = await _insert_brand(pool, "Other Style", node_id=18)
    liked = await _insert_product(pool, brand="Liked Style", brand_node_id=liked_brand)
    other = await _insert_product(pool, brand="Other Style", brand_node_id=other_brand)
    await _insert_section(pool, section_id="taste-picks", gender="women", product_ids=[], sort_order=6)

    empty = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    assert "taste-picks" not in {section["id"] for section in empty.json()["sections"]}

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_style_scores (user_id, style_node_id, score, signal_count)
            VALUES (%s, 17, 5, 1)
            """,
            (user_id,),
        )
        await conn.commit()

    response = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    section = {item["id"]: item for item in response.json()["sections"]}["taste-picks"]
    ids = {product["product_id"] for product in section["products"]}
    assert ids == {liked}
    assert other not in ids


@pytest.mark.asyncio
async def test_taste_picks_caps_at_one_hundred_products(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    weak_brand_id = await _insert_brand(pool, "Weak Taste", node_id=17)
    strong_brand_id = await _insert_brand(pool, "Strong Taste", node_id=18)
    weak_product_ids = [
        await _insert_product(
            pool,
            brand="Weak Taste",
            brand_node_id=weak_brand_id,
            name=f"Weak Taste {index}",
            review_count=100,
        )
        for index in range(101)
    ]
    strong_product_id = await _insert_product(
        pool,
        brand="Strong Taste",
        brand_node_id=strong_brand_id,
        name="Strong Taste Product",
        review_count=0,
    )
    await _insert_section(pool, section_id="taste-picks", gender="women", product_ids=[], sort_order=6)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_style_scores (user_id, style_node_id, score, signal_count)
            VALUES (%s, 17, 1, 1), (%s, 18, 10, 1)
            """,
            (user_id, user_id),
        )
        await conn.commit()

    response = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    section = {item["id"]: item for item in response.json()["sections"]}["taste-picks"]
    assert len(section["products"]) == 100
    selected_ids = {product["product_id"] for product in section["products"]}
    assert strong_product_id in selected_ids
    assert len(selected_ids & set(weak_product_ids)) == 99


@pytest.mark.asyncio
async def test_taste_picks_applies_negative_feature_scores_to_total(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Feature Taste", node_id=17)
    blocked = await _insert_product(pool, brand="Feature Taste", brand_node_id=brand_id, name="Blocked")
    allowed = await _insert_product(pool, brand="Feature Taste", brand_node_id=brand_id, name="Allowed")
    await _insert_section(pool, section_id="taste-picks", gender="women", product_ids=[], sort_order=6)

    async with pool.connection() as conn, conn.cursor() as cur:
        for product_id, color in ((blocked, "BLACK"), (allowed, "WHITE")):
            await cur.execute(
                """
                INSERT INTO public.product_features
                    (product_id, retrieval_text, feature_metadata, feature_version, vlm_model)
                VALUES (%s, '', jsonb_build_object('primary_color', %s::text), 'test', 'test')
                """,
                (product_id, color),
            )
        await cur.execute(
            """
            INSERT INTO ai.user_style_scores (user_id, style_node_id, score, signal_count)
            VALUES (%s, 17, 3, 1)
            """,
            (user_id,),
        )
        await cur.execute(
            """
            INSERT INTO ai.user_feature_scores (user_id, axis, value, score, signal_count)
            VALUES (%s, 'color', 'BLACK', -4, 1)
            """,
            (user_id,),
        )
        await conn.commit()

    response = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    section = {item["id"]: item for item in response.json()["sections"]}["taste-picks"]
    assert {product["product_id"] for product in section["products"]} == {allowed}


@pytest.mark.asyncio
async def test_favorite_brands_caps_at_one_hundred_products(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    brand_id = await _insert_brand(pool, "Favorite Brand")
    product_ids = [
        await _insert_product(pool, brand="Favorite Brand", brand_node_id=brand_id, name=f"Favorite {index}")
        for index in range(105)
    ]
    await _insert_section(pool, section_id="favorite-brands", gender="women", product_ids=[], sort_order=7)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_brand_picks (user_id, brand_id, style_node_id, source)
            VALUES (%s, %s, NULL, 'follow')
            """,
            (user_id, brand_id),
        )
        await conn.commit()

    response = await client.get("/v1/curation", params={"gender": "women"}, headers={"Authorization": auth})
    section = {item["id"]: item for item in response.json()["sections"]}["favorite-brands"]
    assert len(section["products"]) == 100
    assert {product["product_id"] for product in section["products"]} == set(product_ids[-100:])


@pytest.mark.asyncio
async def test_curation_impressions_require_auth_and_deduplicate(client: AsyncClient, pool):
    product_style_node = 17
    brand_id = await _insert_brand(pool, "Impression Brand", node_id=product_style_node)
    product_id = await _insert_product(pool, brand="Impression Brand", brand_node_id=brand_id)
    await _insert_section(pool, section_id="trending-search", gender="women", product_ids=[product_id])
    payload = {"items": [{"section_id": "trending-search", "product_id": product_id, "position": 0}]}

    unauthenticated = await client.post("/v1/curation/impressions", json=payload)
    assert unauthenticated.status_code in (401, 403)

    auth, user_id = await _login(client)
    first = await client.post(
        "/v1/curation/impressions",
        json=payload,
        headers={"Authorization": auth},
    )
    duplicate = await client.post(
        "/v1/curation/impressions",
        json=payload,
        headers={"Authorization": auth},
    )
    assert first.status_code == 200
    assert first.json() == {"recorded": 1}
    assert duplicate.json() == {"recorded": 0}

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT section_id, product_id, style_node_id, position
            FROM ai.curation_impressions
            WHERE user_id = %s
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
    assert rows == [("trending-search", product_id, product_style_node, 0)]


# ── curation refresher (auto sections) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_trending_ranks_brands_by_top_product_engagement(client: AsyncClient, pool):
    from app.services.curation_refresh import refresh_auto_sections

    await _insert_section(pool, section_id="trending-search", gender="women", product_ids=[])
    brand_a = await _insert_brand(pool, "Broad Interest")
    brand_b = await _insert_brand(pool, "Single Hit")
    products_a = [
        await _insert_product(pool, brand="Broad Interest", brand_node_id=brand_a, name=f"A{i}") for i in range(3)
    ]
    product_b = await _insert_product(pool, brand="Single Hit", brand_node_id=brand_b, name="B")
    users = [(await _login(client))[1] for _ in range(3)]

    async with pool.connection() as conn, conn.cursor() as cur:
        for user_id, product_id in zip(users, products_a, strict=True):
            await cur.execute(
                "INSERT INTO ai.product_views (user_id, product_id, session_id) VALUES (%s, %s, %s)",
                (user_id, product_id, str(uuid4())),
            )
        for user_id in users[:2]:
            await cur.execute(
                "INSERT INTO ai.product_views (user_id, product_id, session_id) VALUES (%s, %s, %s)",
                (user_id, product_b, str(uuid4())),
            )
        await conn.commit()

    assert await refresh_auto_sections(pool, ("trending-search",), strict_quota=False) == 1
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT product_ids FROM ai.curation_sections WHERE section_id = 'trending-search' AND gender = 'women'"
        )
        trending_ids = list((await cur.fetchone())[0])

    assert trending_ids[0] in products_a
    assert product_b == trending_ids[1]
    assert len(set(products_a) & set(trending_ids)) == 3


@pytest.mark.asyncio
async def test_refresh_auto_sections_trending_and_under100(client: AsyncClient, pool):
    from app.services.curation_refresh import refresh_auto_sections

    await _insert_auto_sections(pool)
    _auth, user_id = await _login(client)
    cheap = await _insert_product(pool, brand="Cheap", price=42000)
    pricey = await _insert_product(pool, brand="Pricey", price=250000)
    viewed = await _insert_product(pool, brand="Hot", price=120000)
    async with pool.connection() as conn, conn.cursor() as cur:
        for _ in range(3):
            await cur.execute(
                "INSERT INTO ai.product_views (user_id, product_id, session_id) VALUES (%s, %s, %s)",
                (user_id, viewed, str(uuid4())),
            )
        await conn.commit()

    written = await refresh_auto_sections(pool, ("trending-search",), strict_quota=False)
    assert written == 1

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT product_ids FROM ai.curation_sections WHERE section_id = 'trending-search' AND gender = 'women'"
        )
        stored_trending = list((await cur.fetchone())[0])
    assert stored_trending[0] == viewed

    resp = await client.get("/v1/curation", params={"gender": "women"})
    sections = {s["id"]: s for s in resp.json()["sections"]}
    trending = [p["product_id"] for p in sections["trending-search"]["products"]]
    assert set(trending) == {cheap, pricey, viewed}

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE ai.curation_sections SET is_active = false WHERE section_id = 'trending-search'")
        await conn.commit()
    written = await refresh_auto_sections(pool, ("under-100",), strict_quota=False)
    assert written == 1
    resp = await client.get("/v1/curation", params={"gender": "women"})
    sections = {s["id"]: s for s in resp.json()["sections"]}
    under = [p["product_id"] for p in sections["under-100"]["products"]]
    # 상한(15만원) 위인 pricey만 탈락. under-100 은 trending 과 달리 조회수 점수가
    # 없어(score=0::float) 순서는 md5(product_id||current_date) 로만 섞인다 —
    # 날짜마다 바뀌므로 순서(under[0])는 단언하지 않고 membership 만 확인한다.
    assert cheap in under and viewed in under and pricey not in under


@pytest.mark.asyncio
async def test_refresh_under100_filters_bad_data(client: AsyncClient, pool):
    """구좌 데이터 품질 가드 — 크롤 이상치가 under-100 구좌에 노출되지 않는다."""
    from app.services.curation_refresh import refresh_auto_sections

    await _insert_auto_sections(pool)
    ok = await _insert_product(pool, brand="Clean", price=42000)
    unconverted_fx = await _insert_product(pool, brand="FxBug", price=145)  # $145가 KRW로 오탐
    mixed_gender = await _insert_product(pool, brand="Mixed", price=99000, gender=["women", "unisex"])
    unisex = await _insert_product(pool, brand="Unisex", price=99000, gender=["unisex"])
    dupes = [await _insert_product(pool, brand="SameBrand", price=50000 + i) for i in range(3)]

    await refresh_auto_sections(pool, ("under-100",), strict_quota=False)

    resp = await client.get("/v1/curation", params={"gender": "women"})
    sections = {s["id"]: s for s in resp.json()["sections"]}
    under = [p["product_id"] for p in sections["under-100"]["products"]]

    assert ok in under
    assert unconverted_fx not in under  # 가격 하한(5000원) 가드
    # unisex 는 검색(v6)과 같이 남녀 양쪽에 노출된다 — GENDER_MATCH_SQL 참조.
    for included in (mixed_gender, unisex):
        assert included in under
    assert len([pid for pid in dupes if pid in under]) == 3


@pytest.mark.asyncio
async def test_refresh_auto_sections_never_repeats_products_across_slots(client: AsyncClient, pool):
    """Earlier auto slots own their products; later slots refill from unused candidates."""
    from app.services.curation_refresh import refresh_auto_sections

    await _insert_auto_sections(pool)
    for index in range(200):
        await _insert_product(pool, brand=f"Distinct Brand {index}", price=50000 + index)

    written = await refresh_auto_sections(pool)
    assert written == 2

    response = await client.get("/v1/curation", params={"gender": "women"})
    sections = {section["id"]: section for section in response.json()["sections"]}
    product_sets = [
        {product["product_id"] for product in sections[section_id]["products"]}
        for section_id in ("trending-search", "under-100")
    ]

    assert [len(product_ids) for product_ids in product_sets] == [100, 100]
    assert product_sets[0].isdisjoint(product_sets[1])


@pytest.mark.asyncio
async def test_curation_exposes_display_type_for_renderer_split(client: AsyncClient, pool):
    """앱이 트렌딩 구좌를 전용 디자인으로 그린다 — slot_type 과 별개 축 (migration 0030).

    트렌딩 구좌도 slot_type 은 editorial 이라, 이 필드가 없으면 렌더러를 나눌 수 없다.
    """
    product_id = await _insert_product(pool, brand="B", price=42000)
    await _insert_section(
        pool,
        section_id="editorial-summer-vacation",
        gender="women",
        product_ids=[product_id],
        slot_type="editorial",
        display_type="trending",
        sort_order=10,
    )
    await _insert_section(
        pool,
        section_id="editorial-brand-picks",
        gender="women",
        product_ids=[product_id],
        slot_type="editorial",
        sort_order=20,
    )

    resp = await client.get("/v1/curation", params={"gender": "women"})
    assert resp.status_code == 200
    sections = {s["id"]: s for s in resp.json()["sections"]}
    assert sections["editorial-summer-vacation"]["display_type"] == "trending"
    # 값을 주지 않은 구좌는 기존 그리드 그대로 — 컬럼 추가가 기존 구좌를 바꾸지 않는다.
    assert sections["editorial-brand-picks"]["display_type"] == "default"
    assert sections["editorial-summer-vacation"]["slot_type"] == "editorial"
