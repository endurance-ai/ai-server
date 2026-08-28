"""SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-VALUE-MAPPING-001 — clarify 값 매핑 표.

각 (axis, value) 쌍에 대해 검색 입력 보강 정보를 정의한다:

- `keywords_to_boost` — `boost_keywords` 로 흐름(sticky, self-critique fast-path
  생존). 빈 리스트 가능.
- `subcategory_override` — `vision_selected_item.subcategory` 강제 적용용. None 가능.
- `searchQueryKo_augment` — `searchQueryKo` 에 공백 결합. None 가능.
- `label_ko` — 인라인 버튼에 표시되는 한국어 라벨(REQ-CLARIFY-CARD-003,
  16자 권장 / 50자 하드 한도). 이모지/특수문자 금지.

매핑 표는 결정론적이며 LLM 호출이 없다(REQ-CLARIFY-CARD-001).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── 공통 라벨 / 프롬프트 ─────────────────────────────────────────────────────

# REQ-CLARIFY-CARD-002 — 모든 카드의 마지막 버튼.
SKIP_LABEL_KO: str = "건너뛰기"
SKIP_VALUE: str = "skip"


@dataclass(frozen=True)
class ClarifyOption:
    """카드의 버튼 한 개를 정의한다."""

    value: str
    label_ko: str
    keywords_to_boost: list[str] = field(default_factory=list)
    subcategory_override: str | None = None
    # mixedCase 는 kikoai/app `searchQueryKo` 와 의도적으로 일치.
    searchQueryKo_augment: str | None = None  # noqa: N815


# axis 별 본문 프롬프트(REQ-CLARIFY-CARD-001, 60자 이하 한국어).
AXIS_PROMPTS_KO: dict[str, str] = {
    "category_pick": "어떤 종류 옷 찾고 있어?",
    "formality": "이 옷, 어디서 입을 거야?",
    "fit": "어떤 핏 원해?",
    "occasion": "어떤 자리에 어울리면 좋을까?",
    "subcategory_disambiguation": "조금 더 좁혀볼게. 어느 쪽이야?",
    "generic_fallback": "어떤 종류 옷 찾고 있어?",
}


# ── 축별 옵션 매핑 (skip 제외) ──────────────────────────────────────────────
#
# Korean labels: 일반 한글 + 영문 공통 단어만(REQ-CLARIFY-CARD-003 / R4).
# subcategory_override 값은 vision_prompt.py / kikoai/app analyze.ts 의 enum 과
# 일관되도록 영문 snake_case 로 둔다(R2 mitigation: snapshot test).

# 1) category_pick — 단일 아이템이지만 대분류가 약할 때.
CATEGORY_PICK_OPTIONS: list[ClarifyOption] = [
    ClarifyOption(
        value="top",
        label_ko="상의",
        keywords_to_boost=["top"],
        subcategory_override="top",
        searchQueryKo_augment="상의",
    ),
    ClarifyOption(
        value="bottom",
        label_ko="하의",
        keywords_to_boost=["bottom"],
        subcategory_override="bottom",
        searchQueryKo_augment="하의",
    ),
    ClarifyOption(
        value="outer",
        label_ko="아우터",
        keywords_to_boost=["outerwear"],
        subcategory_override="outer",
        searchQueryKo_augment="아우터",
    ),
    ClarifyOption(
        value="dress",
        label_ko="원피스",
        keywords_to_boost=["dress"],
        subcategory_override="dress",
        searchQueryKo_augment="원피스",
    ),
]

# 2) formality — REQ-CLARIFY-AXIS-SELECTION-002 의 핵심 축.
FORMALITY_OPTIONS: list[ClarifyOption] = [
    ClarifyOption(
        value="casual",
        label_ko="캐주얼",
        keywords_to_boost=["casual"],
        searchQueryKo_augment="캐주얼",
    ),
    ClarifyOption(
        value="semi_formal",
        label_ko="세미포멀",
        keywords_to_boost=["semi-formal"],
        searchQueryKo_augment="세미포멀",
    ),
    ClarifyOption(
        value="formal",
        label_ko="포멀",
        keywords_to_boost=["formal"],
        searchQueryKo_augment="포멀",
    ),
    ClarifyOption(
        value="street",
        label_ko="스트릿",
        keywords_to_boost=["street", "streetwear"],
        searchQueryKo_augment="스트릿",
    ),
]

# 3) fit — Vision 의 fit enum 과 정합.
FIT_OPTIONS: list[ClarifyOption] = [
    ClarifyOption(
        value="oversize",
        label_ko="오버사이즈",
        keywords_to_boost=["oversized"],
        searchQueryKo_augment="오버사이즈",
    ),
    ClarifyOption(
        value="regular",
        label_ko="레귤러",
        keywords_to_boost=["regular"],
        searchQueryKo_augment="레귤러 핏",
    ),
    ClarifyOption(
        value="slim",
        label_ko="슬림",
        keywords_to_boost=["slim"],
        searchQueryKo_augment="슬림 핏",
    ),
    ClarifyOption(
        value="cropped",
        label_ko="크롭",
        keywords_to_boost=["cropped"],
        searchQueryKo_augment="크롭",
    ),
]

# 4) occasion — TPO 보강.
OCCASION_OPTIONS: list[ClarifyOption] = [
    ClarifyOption(
        value="daily",
        label_ko="데일리",
        keywords_to_boost=["daily"],
        searchQueryKo_augment="데일리",
    ),
    ClarifyOption(
        value="office",
        label_ko="오피스",
        keywords_to_boost=["office", "work"],
        searchQueryKo_augment="오피스",
    ),
    ClarifyOption(
        value="date",
        label_ko="데이트",
        keywords_to_boost=["date"],
        searchQueryKo_augment="데이트룩",
    ),
    ClarifyOption(
        value="sport",
        label_ko="운동",
        keywords_to_boost=["sport", "athletic"],
        searchQueryKo_augment="운동",
    ),
]

# 5) subcategory_disambiguation — 현재는 가장 흔한 모호 케이스 두 개만 정의.
#    (확장은 별도 SPEC; v1은 generic_fallback 으로 대부분 흡수)
SUBCATEGORY_DISAMBIGUATION_OPTIONS: list[ClarifyOption] = [
    ClarifyOption(
        value="shirt",
        label_ko="셔츠",
        keywords_to_boost=["shirt"],
        subcategory_override="shirt",
        searchQueryKo_augment="셔츠",
    ),
    ClarifyOption(
        value="blouse",
        label_ko="블라우스",
        keywords_to_boost=["blouse"],
        subcategory_override="blouse",
        searchQueryKo_augment="블라우스",
    ),
    ClarifyOption(
        value="tshirt",
        label_ko="티셔츠",
        keywords_to_boost=["t-shirt", "tee"],
        subcategory_override="tshirt",
        searchQueryKo_augment="티셔츠",
    ),
]

# 6) generic_fallback — 위 어떤 축도 안 잡히면 사용하는 안전망.
GENERIC_FALLBACK_OPTIONS: list[ClarifyOption] = [
    ClarifyOption(
        value="coat",
        label_ko="코트",
        keywords_to_boost=["coat"],
        subcategory_override="coat",
        searchQueryKo_augment="코트",
    ),
    ClarifyOption(
        value="shirt",
        label_ko="셔츠",
        keywords_to_boost=["shirt"],
        subcategory_override="shirt",
        searchQueryKo_augment="셔츠",
    ),
    ClarifyOption(
        value="pants",
        label_ko="팬츠",
        keywords_to_boost=["pants"],
        subcategory_override="pants",
        searchQueryKo_augment="팬츠",
    ),
    ClarifyOption(
        value="shoes",
        label_ko="신발",
        keywords_to_boost=["shoes", "footwear"],
        subcategory_override="shoes",
        searchQueryKo_augment="신발",
    ),
]


# axis(snake_case) → 옵션 리스트
AXIS_OPTIONS: dict[str, list[ClarifyOption]] = {
    "category_pick": CATEGORY_PICK_OPTIONS,
    "formality": FORMALITY_OPTIONS,
    "fit": FIT_OPTIONS,
    "occasion": OCCASION_OPTIONS,
    "subcategory_disambiguation": SUBCATEGORY_DISAMBIGUATION_OPTIONS,
    "generic_fallback": GENERIC_FALLBACK_OPTIONS,
}


def get_options(axis: str) -> list[ClarifyOption]:
    """축의 옵션 리스트(skip 제외) 반환. 알 수 없는 축이면 빈 리스트."""
    return list(AXIS_OPTIONS.get(axis, []))


def get_option(axis: str, value: str) -> ClarifyOption | None:
    """(axis, value) 매핑을 찾아 반환. value=='skip'은 None(건너뛰기 의미)."""
    if value == SKIP_VALUE:
        return None
    for opt in AXIS_OPTIONS.get(axis, []):
        if opt.value == value:
            return opt
    return None


__all__ = [
    "AXIS_OPTIONS",
    "AXIS_PROMPTS_KO",
    "CATEGORY_PICK_OPTIONS",
    "ClarifyOption",
    "FIT_OPTIONS",
    "FORMALITY_OPTIONS",
    "GENERIC_FALLBACK_OPTIONS",
    "OCCASION_OPTIONS",
    "SKIP_LABEL_KO",
    "SKIP_VALUE",
    "SUBCATEGORY_DISAMBIGUATION_OPTIONS",
    "get_option",
    "get_options",
]
