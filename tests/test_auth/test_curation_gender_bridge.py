"""curation 성별 3단 다리 — VLM → products.gender → 제외.

`search_products_v6` 와 같은 계단을 쓰지만 **마지막 칸이 반대다**: 검색은 성별
정보가 없으면 fail-open(양쪽 노출), 큐레이션은 fail-closed(제외). 이 비대칭이
의도된 것임을 여기서 고정한다 — 메인 피드에 성별이 어긋난 상품을 띄우느니
VLM 배치가 돌 때까지 기다린다.

products.gender 를 DROP 할 때 2번째 칸과 그 케이스들을 함께 지운다.
"""

from __future__ import annotations

import pytest

from app.services.curation_refresh import GENDER_MATCH_SQL, PRODUCT_FEATURES_JOIN

pytestmark = pytest.mark.asyncio

_MATCH_SQL = f"""
    SELECT p.name
    FROM public.products p
    {PRODUCT_FEATURES_JOIN}
    WHERE {GENDER_MATCH_SQL}
    ORDER BY p.name
"""

# (name, VLM gender, products.gender)
_FIXTURES = [
    ("vlm-women-overrides-legacy-men", "women", ["men"]),
    ("vlm-unisex-excluded", "unisex", ["women"]),
    ("legacy-women", None, ["women"]),
    ("legacy-women-plus-unisex-excluded", None, ["women", "unisex"]),
    ("legacy-men", None, ["men"]),
    ("no-gender-anywhere", None, None),
    ("legacy-empty-array", None, []),
]


async def _seed(pool) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        for name, vlm_gender, legacy in _FIXTURES:
            await cur.execute(
                """
                INSERT INTO public.products (brand, name, product_url, gender)
                VALUES ('b', %s, %s, %s) RETURNING id
                """,
                (name, f"https://example.test/{name}", legacy),
            )
            product_id = (await cur.fetchone())[0]
            if vlm_gender is not None:
                await cur.execute(
                    """
                    INSERT INTO public.product_features (product_id, feature_metadata)
                    VALUES (%s, jsonb_build_object('gender', %s::text))
                    """,
                    (product_id, vlm_gender),
                )
        await conn.commit()


async def _matched(pool, gender: str) -> list[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_MATCH_SQL, {"gender": gender})
        return [r[0] for r in await cur.fetchall()]


async def test_women_matches_vlm_first_then_legacy(pool) -> None:
    await _seed(pool)
    assert await _matched(pool, "women") == [
        "legacy-women",
        "vlm-women-overrides-legacy-men",
    ]


async def test_men_falls_back_to_legacy_and_loses_the_vlm_override(pool) -> None:
    await _seed(pool)
    # legacy 가 ['men'] 이어도 VLM 이 women 이라고 하면 men 에서 빠진다.
    assert await _matched(pool, "men") == ["legacy-men"]


async def test_missing_gender_is_excluded_from_both(pool) -> None:
    """fail-closed. 검색(v6)이라면 이 둘은 양쪽에 노출된다 — 큐레이션은 아니다."""
    await _seed(pool)
    excluded = {"no-gender-anywhere", "legacy-empty-array"}
    assert excluded.isdisjoint(await _matched(pool, "women"))
    assert excluded.isdisjoint(await _matched(pool, "men"))
