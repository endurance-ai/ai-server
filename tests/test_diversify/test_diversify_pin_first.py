"""지목 상품 다양화 보호 (2026-10-17).

상품명을 그대로 적어 지목한 상품이 다양화에서 빠진 7건(dev 측정)의 원인: 같은 브랜드 ·
이름 · 가격의 다른 행이 먼저 나와 중복 제거 3건, 브랜드 캡 2건, 목표 개수 도달 1건.
pin_first_id 로 넘긴 상품은 캡 · 중복 제거 없이 맨 앞에 둔다.
"""

from __future__ import annotations

from app.models.request import AnalyzedItem, RecommendRequest
from app.pipeline.state import PipelineState
from app.services.diversify_service import diversify_service


def _state(raw: list[dict], *, final_limit: int = 5, pin_first_id: str | None = None) -> PipelineState:
    item = AnalyzedItem(id="item-1", category="top", subcategory="knit", searchQuery="q", searchQueryKo="ko")
    req = RecommendRequest(
        item=item,
        imageUrl="https://example.com/x.jpg",
        finalLimit=final_limit,
        pinFirstId=pin_first_id,
    )
    st = PipelineState(request=req)
    st.raw_candidates = raw
    return st


def _brand_heavy() -> list[dict]:
    # 같은 브랜드 4개가 앞에 있고 지목 상품(target)이 5번째 — 브랜드 캡 3에 걸린다.
    raw = [{"id": f"s{i}", "brand": "SUADE", "name": f"Tee {i}", "platform": f"p{i}"} for i in range(4)]
    raw.insert(4, {"id": "target", "brand": "SUADE", "name": "HEROIN STAR LONG SLEEVE", "platform": "p9"})
    raw += [{"id": f"o{i}", "brand": f"B{i}", "name": f"Other {i}", "platform": f"q{i}"} for i in range(4)]
    return raw


async def test_brand_cap_drops_target_without_pin():
    out = await diversify_service(_state(_brand_heavy()))
    assert "target" not in [c["id"] for c in out.final_candidates]


async def test_pinned_target_survives_brand_cap_and_goes_first():
    out = await diversify_service(_state(_brand_heavy(), pin_first_id="target"))
    ids = [c["id"] for c in out.final_candidates]
    assert ids[0] == "target"
    assert len(ids) == 5


async def test_pinned_target_wins_over_same_content_twin():
    # 카탈로그에 같은 상품이 다른 id 로 두 번 있다(브랜드 · 이름 · 가격 동일). 지목한 id 를 남긴다.
    raw = [
        {"id": "twin", "brand": "TEKET", "name": "Mindsweep L/S Tee Blue", "price": 59000, "platform": "a"},
        {"id": "target", "brand": "TEKET", "name": "Mindsweep L/S Tee Blue", "price": 59000, "platform": "b"},
        {"id": "x", "brand": "X", "name": "Other", "platform": "c"},
    ]
    out = await diversify_service(_state(raw, pin_first_id="target"))
    ids = [c["id"] for c in out.final_candidates]
    assert ids[0] == "target"
    assert "twin" not in ids


async def test_missing_pin_id_keeps_existing_order():
    raw = [{"id": f"o{i}", "brand": f"B{i}", "name": f"N{i}", "platform": f"p{i}"} for i in range(3)]
    out = await diversify_service(_state(raw, pin_first_id="absent"))
    assert [c["id"] for c in out.final_candidates] == ["o0", "o1", "o2"]
