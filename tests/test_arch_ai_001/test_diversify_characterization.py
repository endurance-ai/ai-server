"""Net (2) -- diversify_step cap / tolerance / order characterization.

No mocks: diversify_step is pure over state.raw_candidates + settings, so we
construct PipelineState + RecommendRequest directly and lock the exact output
`id` order plus the full counts dict.

Settings defaults exercised (app/core/config.py, 2026-06-17 relax v2):
    SEARCH_BRAND_CAP    = 3   (brand_filter active -> max(*3, target))
    SEARCH_PLATFORM_CAP = 8
tolerance -> target_count = int(round(10 + clamp(t,0,1)*10))  [banker's round]
"""

from __future__ import annotations

import pytest

from app.models.request import AnalyzedItem, RecommendRequest
from app.pipeline.diversify import diversify_step
from app.pipeline.state import PipelineState


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


def _row(i: int, brand: str | None, platform: str = "p1") -> dict:
    d: dict = {"id": f"r{i}", "platform": platform, "score": 0.5}
    # brand=None means key present with None (diversify uses `or ""`).
    d["brand"] = brand
    return d


# ── Case 1: brand_cap (default 3) — only first 3 of brand "A" survive ───────
async def test_characterize_diversify_brand_cap_default():
    raw = [
        _row(0, "A"),
        _row(1, "A"),
        _row(2, "A"),
        _row(3, "B"),
        _row(4, "A"),
        _row(5, "C"),
        _row(6, "A"),
        _row(7, "B"),
    ]
    # platform all "p1", platform_cap=8 → not hit. brand "A" cap=3 → first 3 A
    # (r0,r1,r2) survive; r4 and r6 (4th/5th A) are dropped. B x2, C x1 under cap.
    out = await diversify_step(_state(raw, tolerance=0.5))
    ids = [c["id"] for c in out.final_candidates]
    assert ids == ["r0", "r1", "r2", "r3", "r5", "r7"]
    assert out.counts == {"after_diversify": 6, "final": 6}


# ── Case 2: brand_filter widening — cap 15, all 8 "A" survive ───────────────
async def test_characterize_diversify_brand_filter_widens_cap():
    raw = [_row(i, "A", platform=f"pf{i}") for i in range(8)]
    # distinct platforms so only the brand cap matters; brand_filter -> cap 15.
    out = await diversify_step(_state(raw, tolerance=0.5, brand_filter=["A"]))
    ids = [c["id"] for c in out.final_candidates]
    assert ids == ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7"]
    assert out.counts == {"after_diversify": 8, "final": 8}


# ── Case 3: platform_cap (default 8) — skew one platform, cap not hit ──────
async def test_characterize_diversify_platform_cap():
    raw = [
        _row(0, "B0", platform="X"),
        _row(1, "B1", platform="X"),
        _row(2, "B2", platform="X"),
        _row(3, "B3", platform="X"),
        _row(4, "B4", platform="X"),
        _row(5, "B5", platform="Y"),
    ]
    out = await diversify_step(_state(raw, tolerance=0.5))
    ids = [c["id"] for c in out.final_candidates]
    # platform X count 5 < cap 8 → no drops. All 6 kept.
    assert ids == ["r0", "r1", "r2", "r3", "r4", "r5"]
    assert out.counts == {"after_diversify": 6, "final": 6}


# ── Case 4: tolerance -> target_count (incl. banker's rounding) ────────────
@pytest.mark.parametrize(
    ("tolerance", "expected_target"),
    [
        (0.0, 10),
        (0.5, 15),
        (1.0, 20),
        # 10 + 0.05*10 = 10.5 -> round() banker's -> 10 (even).
        (0.05, 10),
        # 10 + 0.15*10 = 11.5 -> round() banker's -> 12 (even).
        (0.15, 12),
    ],
)
async def test_characterize_diversify_tolerance_to_target(tolerance, expected_target):
    # 30 unique-brand unique-platform rows so no cap ever bites; only target
    # truncation drives the output length -> proves the round() formula.
    raw = [_row(i, f"B{i}", platform=f"pf{i}") for i in range(30)]
    out = await diversify_step(_state(raw, tolerance=tolerance))
    ids = [c["id"] for c in out.final_candidates]
    assert ids == [f"r{i}" for i in range(expected_target)]
    assert out.counts == {"after_diversify": expected_target, "final": expected_target}


# ── Case 5: final_limit override beats tolerance ───────────────────────────
async def test_characterize_diversify_final_limit_override():
    raw = [_row(i, f"B{i}", platform=f"pf{i}") for i in range(30)]
    out = await diversify_step(_state(raw, tolerance=1.0, final_limit=5))
    ids = [c["id"] for c in out.final_candidates]
    assert ids == ["r0", "r1", "r2", "r3", "r4"]
    assert out.counts == {"after_diversify": 5, "final": 5}


# ── Case 6: blank brand / platform — None and "" share the "" counter ──────
async def test_characterize_diversify_blank_brand_shares_bucket():
    raw = [
        _row(0, None, platform="pa"),
        {"id": "r1", "score": 0.5, "platform": "pb"},  # brand key absent
        _row(2, "", platform="pc"),
        _row(3, None, platform="pd"),
    ]
    out = await diversify_step(_state(raw, tolerance=0.5))
    ids = [c["id"] for c in out.final_candidates]
    # All four collapse to brand "" -> brand_cap 3 → 4 > 3, drop the 4th (r3).
    assert ids == ["r0", "r1", "r2"]
    assert out.counts == {"after_diversify": 3, "final": 3}


# ── Case 7: break mid-iteration — tail rows absent once target reached ─────
async def test_characterize_diversify_break_mid_iteration():
    raw = [_row(i, f"B{i}", platform=f"pf{i}") for i in range(12)]
    out = await diversify_step(_state(raw, tolerance=0.0))  # target 10
    ids = [c["id"] for c in out.final_candidates]
    assert ids == [f"r{i}" for i in range(10)]
    # r10, r11 never appended (loop broke at len(out) >= 10).
    assert "r10" not in ids and "r11" not in ids
    assert out.counts == {"after_diversify": 10, "final": 10}
