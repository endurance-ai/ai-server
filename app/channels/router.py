"""RoutedDecision type holder (SPEC-AGENT-V2-CLEANUP-001 follow-up).

The V1 LLM text router (`route_text` + `_ROUTER_SYSTEM_PROMPT` + JSON parser)
was removed: the ReAct agent's reasoning subsumes deterministic text routing
and `route_text` was no longer called by any code path. This module is retained
ONLY for the `RoutedDecision`/`RoutedIntent`/`TasteUpdate` types, which
`app/graphs/state.py` still references as the type of the (always-None)
`WorkingState.decision` field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.channels.critique import CritiqueDelta


class RoutedIntent(StrEnum):
    """Subset of trigger semantics. Kept for the RoutedDecision type."""

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
