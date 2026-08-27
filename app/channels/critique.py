"""Critique 타입 정의 전용 모듈 (2026-08-27 죽은코드 정리).

`parse_callback`(버튼 탭 → delta)·`parse_text`(LLM 자유텍스트 파서)는 평행 모듈
`app/channels/clarify.py` 로 대체되어 어디서도 import 되지 않는 죽은 코드였다
(live `parse_callback` 은 clarify.py 것이고 `apply_clarify` 노드가 그것을 쓴다).
두 파서 + 전용 헬퍼/프롬프트를 삭제하고, 아직 참조되는 타입만 남긴다:
`CritiqueDelta`/`AnchorRef` (state.py · apply_clarify · router.py 가 import).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AnchorRef:
    """Reference to a card from the previous result set."""

    idx: int
    product_id: str | None
    brand: str | None
    name: str | None
    price: int | None
    keywords: list[str] = field(default_factory=list)


@dataclass
class CritiqueDelta:
    """Structured refinement to layer on top of the previous search.

    All fields are optional — handler applies whichever are populated.
    """

    op: str  # "more" | "less" | "cheap" | "free_text" | "noop"
    anchor: AnchorRef | None = None
    exclude_brands: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    boost_keywords: list[str] = field(default_factory=list)
    max_price: int | None = None
    min_price: int | None = None
    color: str | None = None
    extra_intent: str | None = None  # additional natural-language hint to layer on intent
