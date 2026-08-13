"""curation 성별 매칭 — products.gender 단일 출처.

이력: VLM(product_features) 우선 3단 다리 → 크롤러 정본 우선 → 단일 출처.
gender 소유권이 크롤러로 돌아왔고 migration 104 가
`chk_products_gender_required` 를 VALIDATE 해 products.gender 는
non-NULL · non-empty · canonical 이 보장된다. **VLM 은 더 이상 읽지 않는다.**

`search_products_v6` 와 **술어가 같다** — unisex 는 남녀 양쪽에 노출된다.
예전엔 큐레이션만 unisex 를 잘라내는 비대칭이었는데, 카탈로그 상당수가
unisex 라벨이라 브랜드 구좌가 통째로 비어 대칭으로 되돌렸다.

product_features 조인은 남아 있다 — primary_color 와 품질 게이트가 쓴다.
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
#
# VLM 값은 이제 **무시된다** — 앞의 두 픽스처가 그걸 고정한다. products.gender 와
# 어긋나는 VLM 을 심어 두고, 결과가 products.gender 를 따르는지 본다.
_FIXTURES = [
    ("vlm-women-ignored-products-men", "women", ["men"]),
    ("vlm-unisex-ignored-products-women", "unisex", ["women"]),
    ("women", None, ["women"]),
    ("women-plus-unisex", None, ["women", "unisex"]),
    ("men", None, ["men"]),
    # 아래 둘은 chk_products_gender_required 때문에 운영에서는 존재할 수 없다.
    # 술어가 방어적으로 동작하는지만 확인한다.
    ("no-gender", None, None),
    ("empty-array", None, []),
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


async def test_women_reads_products_gender_and_ignores_vlm(pool) -> None:
    await _seed(pool)
    # vlm-unisex-ignored-products-women: VLM 이 unisex 라도 products.gender 가
    # ['women'] 이므로 잡힌다 — 술어는 products.gender 만 본다.
    assert await _matched(pool, "women") == [
        "vlm-unisex-ignored-products-women",
        "women",
        "women-plus-unisex",
    ]


async def test_men_reads_products_gender_and_ignores_vlm(pool) -> None:
    await _seed(pool)
    # vlm-women-ignored-products-men: VLM 이 women 이라고 해도 products.gender 가
    # ['men'] 이므로 men 에 잡힌다. 예전 3단 다리에서는 빠졌던 케이스다.
    assert await _matched(pool, "men") == [
        "men",
        "vlm-women-ignored-products-men",
        "women-plus-unisex",
    ]


async def test_unisex_is_included_in_both(pool) -> None:
    """검색(v6)과 동일 — unisex 라벨은 남녀 양쪽 피드에 노출된다."""
    await _seed(pool)
    for gender in ("women", "men"):
        assert "women-plus-unisex" in await _matched(pool, gender)


async def test_missing_gender_is_excluded_from_both(pool) -> None:
    """운영에서는 chk_products_gender_required 가 막지만 술어도 방어한다."""
    await _seed(pool)
    excluded = {"no-gender", "empty-array"}
    assert excluded.isdisjoint(await _matched(pool, "women"))
    assert excluded.isdisjoint(await _matched(pool, "men"))
