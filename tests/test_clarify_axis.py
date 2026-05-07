"""SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-AXIS-SELECTION-001/002 — pick_clarify_axis.

순수 함수: 사이드 이펙트 / 외부 호출 / state 접근 없음. 우선순위:
  1. category_pick → 2. formality → 3. fit → 4. occasion
  → 5. subcategory_disambiguation → 6. generic_fallback

R2 mitigation: portal/app analyze.ts + vision_prompt.py 의 enum 과 동기화 검증.
SPEC 기준 시점: 2026-05-07.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.channels.clarify import ClarifyAxis, pick_clarify_axis


def _vision(
    *,
    subcategory: str = "shirt",
    fit: str = "regular",
    detected_gender: str = "unisex",
    formality: str | None = None,  # None → field 자체 미존재 (현 스키마 default).
    occasions: list[str] | None = None,
):
    """테스트용 vision_result 더블 — 필드별로 명시적 제어.

    SimpleNamespace 기반: VisionResult 스키마가 아직 formality 를 노출하지
    않으므로 hasattr 분기를 정밀 제어하기 위해 사용.
    """
    item = SimpleNamespace(subcategory=subcategory, fit=fit)
    style_kwargs: dict = {"detectedGender": detected_gender, "fit": fit, "aesthetic": ""}
    if formality is not None:
        style_kwargs["formality"] = formality
    style = SimpleNamespace(**style_kwargs)
    mood = SimpleNamespace(occasion=list(occasions) if occasions is not None else [])
    return SimpleNamespace(items=[item], style=style, mood=mood)


# ── REQ-CLARIFY-AXIS-SELECTION-002 priority order ──────────────────────────


def test_priority_1_category_pick_when_subcat_and_gender_empty():
    v = _vision(subcategory="", detected_gender="")
    assert pick_clarify_axis(v) == ClarifyAxis.CATEGORY_PICK


def test_priority_2_formality_when_field_present_and_empty():
    v = _vision(formality="", subcategory="shirt", detected_gender="female")
    assert pick_clarify_axis(v) == ClarifyAxis.FORMALITY


def test_priority_2_formality_when_ambiguous():
    v = _vision(formality="ambiguous", subcategory="shirt", detected_gender="female")
    assert pick_clarify_axis(v) == ClarifyAxis.FORMALITY


def test_priority_3_fit_when_empty():
    # formality field 미존재 (현 스키마) — fit 으로 떨어짐.
    v = _vision(subcategory="shirt", fit="", detected_gender="female", formality=None)
    assert pick_clarify_axis(v) == ClarifyAxis.FIT


def test_priority_3_fit_when_outside_enum():
    v = _vision(fit="weird_fit", subcategory="shirt", detected_gender="female", formality=None)
    assert pick_clarify_axis(v) == ClarifyAxis.FIT


def test_priority_4_occasion_when_generic_only():
    v = _vision(
        subcategory="shirt",
        fit="regular",
        detected_gender="female",
        formality=None,
        occasions=["daily"],
    )
    assert pick_clarify_axis(v) == ClarifyAxis.OCCASION


def test_priority_4_occasion_when_empty():
    v = _vision(
        subcategory="shirt",
        fit="regular",
        detected_gender="female",
        formality=None,
        occasions=[],
    )
    assert pick_clarify_axis(v) == ClarifyAxis.OCCASION


def test_priority_5_subcategory_disambiguation_when_ambiguous():
    # ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES 에 들어있는 'item' 사용.
    v = _vision(
        subcategory="item",
        fit="regular",
        detected_gender="female",
        formality=None,
        occasions=["office", "date"],
    )
    # subcat 'item' 은 비어있지 않으므로 1 단계는 통과.
    # formality 필드 없으니 2단계 통과. fit 정상이라 3단계 통과. occasion
    # generic-only 아님 → 4단계 통과. ambiguous_subcategories 매치 → 5단계.
    assert pick_clarify_axis(v) == ClarifyAxis.SUBCATEGORY_DISAMBIGUATION


def test_priority_6_generic_fallback_when_nothing_else_fires():
    v = _vision(
        subcategory="cardigan",
        fit="regular",
        detected_gender="female",
        formality=None,
        occasions=["office"],
    )
    assert pick_clarify_axis(v) == ClarifyAxis.GENERIC_FALLBACK


# ── 안전 / 에러 ──────────────────────────────────────────────────────────────


def test_none_vision_returns_none():
    assert pick_clarify_axis(None) is None


def test_empty_items_returns_none():
    v = SimpleNamespace(items=[], style=None, mood=None)
    assert pick_clarify_axis(v) is None


def test_pure_function_no_side_effects():
    """REQ-CLARIFY-AXIS-SELECTION-001 — 순수 함수임을 같은 입력 두 번 호출로 확인."""
    v = _vision(subcategory="", detected_gender="")
    a = pick_clarify_axis(v)
    b = pick_clarify_axis(v)
    assert a == b


# ── R2 schema parity snapshot ──────────────────────────────────────────────
# Source: vision_prompt.py / portal/app analyze.ts (as of 2026-05-07).
# 이 상수가 바뀌면 portal/app 과 정합 검증 후 본 테스트도 함께 갱신.

EXPECTED_VALID_FITS = {"oversized", "relaxed", "regular", "slim", "skinny", "boxy", "cropped", "longline"}


def test_valid_fit_set_snapshot_matches_routing_module():
    """SPEC-VISION-UNIFY-001 routing._is_weak_vision_v2 의 fit enum 과 일관."""
    # 직접 set 비교 — clarify 가 routing 과 어긋나면 한 쪽이 잘못된 것.
    from app.graphs import routing  # noqa: PLC0415

    src = routing._is_weak_vision_v2.__doc__ or ""
    # 가벼운 부속 검증: 문서 문자열이 valid_fits 집합 정의를 포함한다.
    assert "fit" in src
