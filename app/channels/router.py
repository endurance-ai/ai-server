"""V1 라우팅 제거 후 남은 타입 정의 전용 모듈 (2026-08-27 죽은코드 정리).

`route_text`(V1 LLM 4-way 텍스트 라우터)는 V2 ReAct 에이전트의 추론이 대체하여
어떤 경로에서도 호출되지 않는 죽은 코드였다(`ingest` 노드가 자인). 그 함수와
전용 헬퍼/프롬프트를 삭제하고, 아직 참조되는 타입만 남긴다:
`app/graphs/state.py` 가 `RoutedDecision` 을 `WorkingState.decision`(항상 None)의
타입으로 import 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.channels.critique import CritiqueDelta


class RoutedIntent(StrEnum):
    """Subset of trigger semantics produced by the (removed) router. Retained as
    the value space of `RoutedDecision.intent`."""

    NEW_SEARCH_REQUEST = "new_search_request"
    CRITIQUE_TEXT = "critique_text"
    TASTE_UPDATE = "taste_update"
    OFF_TOPIC = "off_topic"


@dataclass
class TasteUpdate:
    liked_brands: list[str] = field(default_factory=list)
    disliked_brands: list[str] = field(default_factory=list)
    liked_keywords: list[str] = field(default_factory=list)
    disliked_keywords: list[str] = field(default_factory=list)


@dataclass
class RoutedDecision:
    intent: RoutedIntent
    critique_delta: CritiqueDelta | None = None
    taste_update: TasteUpdate | None = None
