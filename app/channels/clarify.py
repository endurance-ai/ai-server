"""SPEC-CLARIFY-CARDS-001 — Inline-keyboard clarify cards.

`app/channels/critique.py` 의 평행 모듈. weak-vision 분기에서 ask_clarify 노드가
어떤 축을 물을지 결정하고, 사용자가 탭한 결과 콜백을 구조화된 보강 정보로
변환한다. LLM 호출 없음.

Public surface:
    - `ClarifyAxis` enum — 7개 축 (color 는 에이전트 tool 경유 개인화 카드)
    - `ClarifyDelta` dataclass — 콜백을 풀어 적용 가능한 상태로 만든 표현
    - `parse_callback(callback_data, vision_result) -> ClarifyDelta | None`
    - `pick_clarify_axis(vision_result) -> ClarifyAxis | None`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.channels.clarify_values import AXIS_OPTIONS, SKIP_VALUE, get_option
from app.core.config import settings

logger = logging.getLogger(__name__)


class ClarifyAxis(StrEnum):
    """REQ-CLARIFY-AXIS-SELECTION-001 — 한 턴에 한 축. snake_case 가 콜백 페이로드."""

    CATEGORY_PICK = "category_pick"
    FORMALITY = "formality"
    FIT = "fit"
    OCCASION = "occasion"
    SUBCATEGORY_DISAMBIGUATION = "subcategory_disambiguation"
    GENERIC_FALLBACK = "generic_fallback"
    COLOR = "color"


@dataclass(frozen=True)
class ClarifyDelta:
    """REQ-CLARIFY-VALUE-MAPPING-001 — 콜백을 풀어 검색 입력 보강용으로 직렬화."""

    axis: ClarifyAxis
    value: str
    keywords_to_boost: list[str] = field(default_factory=list)
    subcategory_override: str | None = None
    # mixedCase 는 kikoai/app `searchQueryKo` 와 의도적으로 일치.
    searchQueryKo_augment: str | None = None  # noqa: N815
    raw_callback: str = ""
    is_skip: bool = False


# REQ-CLARIFY-CALLBACK-001 — `clarify:<axis>:<value>` 의 strict 파서.
def parse_callback(callback_data: str, vision_result: Any | None = None) -> ClarifyDelta | None:
    """REQ-CLARIFY-CALLBACK-001 — `clarify:<axis>:<value>` 콜백을 ClarifyDelta 로
    파싱. 형식이 잘못됐거나 알 수 없는 축/값이면 None 을 반환한다.

    `vision_result` 인자는 SPEC 의 시그니처(평행 모듈 critique.parse_callback 과
    동일한 모양)를 위해 받지만, v1에서는 결정론적 매핑만 적용하므로 사용하지
    않는다(future scope).
    """
    if not callback_data or not isinstance(callback_data, str):
        return None
    if not callback_data.startswith("clarify:"):
        return None
    parts = callback_data.split(":", 2)
    if len(parts) != 3:
        return None
    _, axis_raw, value_raw = parts
    axis_raw = axis_raw.strip().lower()
    value_raw = value_raw.strip().lower()
    if not axis_raw or not value_raw:
        return None

    try:
        axis = ClarifyAxis(axis_raw)
    except ValueError:
        return None

    # skip — 모든 축에서 유효한 종결 신호.
    if value_raw == SKIP_VALUE:
        return ClarifyDelta(axis=axis, value=SKIP_VALUE, raw_callback=callback_data, is_skip=True)

    opt = get_option(axis.value, value_raw)
    if opt is None:
        return None

    return ClarifyDelta(
        axis=axis,
        value=value_raw,
        keywords_to_boost=list(opt.keywords_to_boost),
        subcategory_override=opt.subcategory_override,
        searchQueryKo_augment=opt.searchQueryKo_augment,
        raw_callback=callback_data,
        is_skip=False,
    )


# ── pick_clarify_axis — 우선순위 기반 axis 선택 ──────────────────────────────


def _is_generic_only(occasions: list[str]) -> bool:
    """occasion 리스트가 generic 토큰만 포함하면 True. (R2: ['daily'] 단독)"""
    if not occasions:
        return True
    generic = {"daily", "casual", "general", "everyday"}
    return all((o or "").strip().lower() in generic for o in occasions)


# @MX:ANCHOR: [AUTO] Vision 결과 → clarify axis 선택 결정 경계.
# @MX:SPEC: SPEC-CLARIFY-CARDS-001
# @MX:REASON: REQ-CLARIFY-AXIS-SELECTION-001/002 — ask_clarify 노드 + 단위/E2E
#   테스트가 의존하는 결정론적 함수. 우선순위 변경은 SPEC 수정 신호.
def pick_clarify_axis(vision_result: Any | None) -> ClarifyAxis | None:
    """REQ-CLARIFY-AXIS-SELECTION-002 — Vision 결과의 빈 칸을 우선순위대로 검사,
    첫 매치를 반환. 외부 호출 / 어댑터 / 세션 접근 없음(순수 함수).

    우선순위:
      1. category_pick                — 단일 아이템이지만 대분류 자체 추측 약함.
      2. formality                    — formality 비거나 ambiguous.
      3. fit                          — fit 비거나 enum 밖.
      4. occasion                     — mood.occasion generic-only.
      5. subcategory_disambiguation   — subcategory 가 알려진 모호 후보.
      6. generic_fallback             — weak-vision 인데 위 어디에도 안 잡힘.

    None 을 반환하면 호출자(ask_clarify)는 자유 텍스트 폴백 경로를 탄다
    (REQ-CLARIFY-COMPAT-001).
    """
    if vision_result is None:
        return None

    # 단일 아이템 가정(REQ-CLARIFY 분기 자체가 weak-vision single-item).
    items: list[Any] = []
    try:
        items = list(getattr(vision_result, "items", []) or [])
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return None
    item = items[0]

    subcat = (getattr(item, "subcategory", "") or "").strip().lower()
    fit = (getattr(item, "fit", "") or "").strip().lower()
    style = getattr(vision_result, "style", None)
    # `formality` 는 v1 스키마(SPEC-VISION-UNIFY-001)에 아직 없음 — Pydantic 모델은
    # 정의되지 않은 속성을 노출하지 않으므로 `hasattr` 로 명시 체크.
    has_formality_field = hasattr(style, "formality") if style is not None else False
    formality = (getattr(style, "formality", "") or "").strip().lower() if has_formality_field else ""
    detected_gender = (getattr(style, "detectedGender", "") or "").strip().lower() if style is not None else ""

    mood = getattr(vision_result, "mood", None)
    occasions: list[str] = []
    if mood is not None:
        occasion_field = getattr(mood, "occasion", None)
        if isinstance(occasion_field, list):
            occasions = [str(o) for o in occasion_field if o]
        elif isinstance(occasion_field, str) and occasion_field.strip():
            occasions = [occasion_field.strip()]

    valid_fits = {"oversized", "relaxed", "regular", "slim", "skinny", "boxy", "cropped", "longline"}
    ambiguous_subcats = set(settings.ask_clarify_ambiguous_subcategories)

    # 1) category_pick — subcategory 와 detectedGender 가 둘 다 비거나 약하면.
    if not subcat and not detected_gender:
        return ClarifyAxis.CATEGORY_PICK

    # 2) formality — VisionStyle 에 `formality` 필드가 실제로 존재할 때만 검사.
    #    현 스키마는 미정의 → 자동으로 다음 우선순위로 진행.
    if has_formality_field and (not formality or formality == "ambiguous"):
        return ClarifyAxis.FORMALITY

    # 3) fit
    if not fit or fit not in valid_fits:
        return ClarifyAxis.FIT

    # 4) occasion
    if _is_generic_only(occasions):
        return ClarifyAxis.OCCASION

    # 5) subcategory_disambiguation
    if subcat in ambiguous_subcats:
        return ClarifyAxis.SUBCATEGORY_DISAMBIGUATION

    # 6) generic_fallback
    return ClarifyAxis.GENERIC_FALLBACK


__all__ = [
    "AXIS_OPTIONS",
    "ClarifyAxis",
    "ClarifyDelta",
    "parse_callback",
    "pick_clarify_axis",
]
