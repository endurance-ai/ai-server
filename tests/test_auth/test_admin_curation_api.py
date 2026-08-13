"""큐레이션 어드민 API — 노션을 대체하는 구좌 편집 경로.

핵심 계약 세 가지를 고정한다:
  1) 저장이 곧 반영이다 (GET /v1/curation 이 즉시 바뀐다)
  2) auto 구좌의 product_ids 는 어드민이 못 건드린다 (리프레셔 소유)
  3) preview 는 앞 구좌와 겹쳐 잘리는 분까지 반영한 실제 노출 수를 준다
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.test_auth.test_curation_onboarding_api import _insert_product, _insert_section

pytestmark = pytest.mark.asyncio


async def test_upsert_creates_then_updates_and_feeds_public_api(client: AsyncClient, pool):
    product_id = await _insert_product(pool, brand="AdminBrand", price=42000)
    payload = {
        "section_id": "editorial-admin-made",
        "gender": "women",
        "slot_type": "editorial",
        "display_type": "trending",
        "title": "어드민이 만든 구좌",
        "subtitle": "부제",
        "sort_order": 5,
        "is_active": True,
        "product_ids": [product_id, product_id],  # 중복은 저장 시 접힌다
    }
    resp = await client.put("/admin/curation/sections", json=payload)
    assert resp.status_code == 200
    saved = resp.json()
    assert saved["product_ids"] == [product_id]
    assert saved["live_count"] == 1

    # 저장이 곧 반영 — 별도 동기화 단계가 없다.
    public = await client.get("/v1/curation", params={"gender": "women"})
    section = {s["id"]: s for s in public.json()["sections"]}["editorial-admin-made"]
    assert section["display_type"] == "trending"
    assert section["title"] == "어드민이 만든 구좌"
    assert [p["product_id"] for p in section["products"]] == [product_id]

    # 같은 (section_id, gender) 로 다시 저장하면 업데이트.
    payload["title"] = "제목 수정"
    payload["is_active"] = False
    resp = await client.put("/admin/curation/sections", json=payload)
    assert resp.json()["title"] == "제목 수정"
    public = await client.get("/v1/curation", params={"gender": "women"})
    assert "editorial-admin-made" not in {s["id"] for s in public.json()["sections"]}


async def test_auto_section_product_ids_are_not_editable(client: AsyncClient, pool):
    """리프레셔 소유 필드 — 어드민이 써도 기존값이 남아야 한다."""
    keeper = await _insert_product(pool, brand="Keeper", price=42000)
    intruder = await _insert_product(pool, brand="Intruder", price=42000)
    await _insert_section(pool, section_id="popular", gender="women", product_ids=[keeper], slot_type="auto")

    resp = await client.put(
        "/admin/curation/sections",
        json={
            "section_id": "popular",
            "gender": "women",
            "slot_type": "auto",
            "title": "지금 인기",
            "sort_order": 1,
            "is_active": True,
            "product_ids": [intruder],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["product_ids"] == [keeper]  # 제목/순서는 반영, 상품은 보존


async def test_auto_slot_type_rejects_unknown_section_id(client: AsyncClient):
    """리프레셔가 계산할 수 없는 auto 구좌는 만들 수 없다 — 영원히 빈 구좌가 된다."""
    resp = await client.put(
        "/admin/curation/sections",
        json={
            "section_id": "made-up-auto",
            "gender": "women",
            "slot_type": "auto",
            "title": "직접 만든 auto",
            "is_active": True,
        },
    )
    assert resp.status_code == 422


async def test_preview_reflects_cross_section_dedupe(client: AsyncClient, pool):
    """구좌 단독 live_count 로는 설명 안 되는 '앞 구좌가 먼저 가져감' 을 드러낸다."""
    shared = await _insert_product(pool, brand="Shared", price=42000)
    only_later = await _insert_product(pool, brand="Later", price=42000)
    await _insert_section(
        pool, section_id="popular", gender="women", product_ids=[shared], slot_type="auto", sort_order=1
    )
    await _insert_section(
        pool,
        section_id="editorial-brand-picks",
        gender="women",
        product_ids=[shared, only_later],
        slot_type="editorial",
        sort_order=20,
    )

    listing = (await client.get("/admin/curation/sections", params={"gender": "women"})).json()
    by_id = {s["section_id"]: s for s in listing["sections"]}
    assert by_id["editorial-brand-picks"]["live_count"] == 2  # 구좌 단독으로는 2개

    preview = (await client.get("/admin/curation/preview", params={"gender": "women"})).json()
    shown = {s["section_id"]: s["shown"] for s in preview["sections"]}
    assert shown["popular"] == 1
    assert shown["editorial-brand-picks"] == 1  # shared 를 popular 가 먼저 가져갔다


async def test_product_lookup_flags_ineligible_and_missing(client: AsyncClient, pool):
    ok = await _insert_product(pool, brand="Ok", price=42000)
    sold_out = await _insert_product(pool, brand="Gone", price=42000, in_stock=False)
    resp = await client.get(
        "/admin/curation/products",
        params={"gender": "women", "ids": f"{ok}, {sold_out} 999999999"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["missing"] == [999999999]
    eligible = {p["product_id"]: p["eligible"] for p in data["products"]}
    assert eligible == {ok: True, sold_out: False}
    # 붙여넣은 순서 유지 — 운영자가 정렬 의도를 갖고 넣는다.
    assert [p["product_id"] for p in data["products"]] == [ok, sold_out]


async def test_delete_removes_row(client: AsyncClient, pool):
    await _insert_section(pool, section_id="editorial-doomed", gender="women", product_ids=[], slot_type="editorial")
    resp = await client.delete("/admin/curation/sections/editorial-doomed/women")
    assert resp.json()["deleted"] is True
    listing = (await client.get("/admin/curation/sections", params={"gender": "women"})).json()
    assert "editorial-doomed" not in {s["section_id"] for s in listing["sections"]}
    # 없는 행 삭제는 조용히 false.
    assert (await client.delete("/admin/curation/sections/editorial-doomed/women")).json()["deleted"] is False
