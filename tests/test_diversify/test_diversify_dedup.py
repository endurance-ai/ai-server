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
    # 5 unique id × unique brand → platform cap (3) 만 작동.
    # SEARCH_PLATFORM_CAP=3 이므로 first 3 entries 만 통과.
    assert len(out.final_candidates) == 3
    assert ids == ["r0", "r1", "r2"]


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
