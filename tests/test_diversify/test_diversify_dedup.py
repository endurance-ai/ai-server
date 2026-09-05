"""SPEC-AGENT-UX-P0-001 / REQ-UX-001 — diversify dedup-by-id 가드 unit tests.

캡 루프 진입부에 `seen_ids: set[str]` + falsy-id bypass + `drops_dup` 카운터를
검증한다. RED phase 에서는 미구현 — GREEN phase 에서 통과.
"""

from __future__ import annotations

import logging

import pytest

from app.models.request import AnalyzedItem, RecommendRequest
from app.pipeline.state import PipelineState
from app.services.diversify_service import diversify_service


def _state(
    raw: list[dict],
    *,
    tolerance: float = 0.5,
    final_limit: int | None = None,
    brand_filter: list[str] | None = None,
    relax_diversity: bool = False,
) -> PipelineState:
    item = AnalyzedItem(
        id="item-1",
        category="top",
        subcategory="knit",
        searchQuery="q",
        searchQueryKo="ko",
    )
    req = RecommendRequest(
        item=item,
        imageUrl="https://example.com/x.jpg",
        brandFilter=brand_filter,
        tolerance=tolerance,
        finalLimit=final_limit,
        relaxDiversity=relax_diversity,
    )
    st = PipelineState(request=req)
    st.raw_candidates = raw
    return st


# ── REQ-UX-001 acceptance ────────────────────────────────────────────────


async def test_dedup_drops_duplicate_id() -> None:
    """동일 id 가 두 번 등장하면 두 번째 카드는 drop. 결과는 unique id 4개."""
    raw = [
        {"id": "prod-A", "brand": "b1", "platform": "p1"},
        {"id": "prod-B", "brand": "b2", "platform": "p2"},
        {"id": "prod-A", "brand": "b3", "platform": "p3"},  # dup
        {"id": "prod-C", "brand": "b4", "platform": "p4"},
        {"id": "prod-D", "brand": "b5", "platform": "p5"},
    ]
    state = _state(raw, final_limit=10)
    out = await diversify_service(state)
    ids = [c["id"] for c in out.final_candidates]
    assert len(ids) == 4
    assert len(set(ids)) == 4
    assert ids.count("prod-A") == 1


async def test_missing_id_bypass_dedup() -> None:
    """`id=None` / `id=""` 은 dedup 우회 — collapse 되지 않고 모두 통과."""
    raw = [
        {"id": None, "brand": "b1", "platform": "p1"},
        {"id": "", "brand": "b2", "platform": "p2"},
        {"id": "prod-X", "brand": "b3", "platform": "p3"},
    ]
    state = _state(raw, final_limit=10)
    out = await diversify_service(state)
    # 누락 id 두 개 + prod-X = 3 개 모두 통과 (brand/platform 각각 unique).
    assert len(out.final_candidates) == 3


async def test_byte_identical_on_unique_ids() -> None:
    """unique id input — pre-SPEC 결과와 동일해야 함 (brand/platform cap regression)."""
    raw = [{"id": f"r{i}", "brand": f"b{i}", "platform": "p1", "score": 0.5} for i in range(5)]
    state = _state(raw, final_limit=10)
    out = await diversify_service(state)
    ids = [c["id"] for c in out.final_candidates]
    # 5 unique id × unique brand → platform cap (8, 2026-06-17 relax) 미발동.
    # 모두 통과.
    assert len(out.final_candidates) == 5
    assert ids == ["r0", "r1", "r2", "r3", "r4"]


async def test_brand_filter_relaxes_caps_single_brand() -> None:
    """브랜드 지정 검색은 단일 브랜드/플랫폼이라도 캡에 안 걸리고 다 통과.

    관측 버그: '글로니 상의' 처럼 brand_filter 가 있으면 결과가 전부 같은
    브랜드(=같은 platform, 같은 vibe/silhouette first-token)라 brand/platform
    캡이 겹쳐 5개로 잘렸다. brand_filter 활성 시 캡을 풀어 target 까지 채운다.
    """
    raw = [{"id": f"g{i}", "brand": "GLOWNY", "platform": "glowny"} for i in range(12)]
    # brand_filter 없음 → brand_cap=3 로 3개만.
    capped = await diversify_service(_state(raw, final_limit=40))
    assert len(capped.final_candidates) == 3
    # brand_filter 있음 → 캡 완화, target(40)까지 = 12개 전부.
    relaxed = await diversify_service(_state(raw, final_limit=40, brand_filter=["GLOWNY"]))
    assert len(relaxed.final_candidates) == 12


async def test_relax_diversity_anchor_similarity_relaxes_caps() -> None:
    """②(윤영 P1): 특정 상품 앵커 "더 비슷하게"는 relax_diversity 로 캡을 풀어
    PDP 유사상품처럼 순수 유사도 상단(=같은 브랜드/결이라도)이 살아남는다.
    다양성 캡이 제일 닮은 상품을 잘라내던 게 "더비슷하게 < PDP" 원인이었다."""
    raw = [{"id": f"g{i}", "brand": "GLOWNY", "platform": "glowny"} for i in range(12)]
    # relax_diversity 없음 → brand_cap=3.
    capped = await diversify_service(_state(raw, final_limit=40))
    assert len(capped.final_candidates) == 3
    # relax_diversity=True → 캡 완화, 12개 전부(가장 닮은 것 보존).
    relaxed = await diversify_service(_state(raw, final_limit=40, relax_diversity=True))
    assert len(relaxed.final_candidates) == 12


async def test_drops_dup_in_log_line(caplog: pytest.LogCaptureFixture) -> None:
    """`drops_dup=N` 이 [STEP 4.8] 로그 라인에 포함."""
    raw = [
        {"id": "p1", "brand": "b1", "platform": "p1"},
        {"id": "p1", "brand": "b2", "platform": "p2"},  # dup
    ]
    state = _state(raw, final_limit=10)
    with caplog.at_level(logging.INFO, logger="app.services.diversify_service"):
        await diversify_service(state)
    assert "drops_dup=1" in caplog.text
    # 기존 카운터들도 같은 라인에 보존.
    assert "drops_brand=" in caplog.text
    assert "drops_platform=" in caplog.text


async def test_dedup_does_not_mutate_input() -> None:
    """가드는 `state.raw_candidates` 리스트를 mutate 하지 않음."""
    raw = [
        {"id": "prod-A", "brand": "b1", "platform": "p1"},
        {"id": "prod-A", "brand": "b2", "platform": "p2"},
    ]
    state = _state(list(raw), final_limit=10)
    before = list(state.raw_candidates)
    await diversify_service(state)
    assert state.raw_candidates == before
