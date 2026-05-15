"""SPEC-CONVERSATION-LOG-001 / REQ-LOG-CATALOG-001 — 20 TypedDict payload schemas.

각 TypedDict 는 SPEC §Event Type Catalog 의 항목 1-20 과 1:1 대응한다. `total=False`
를 채택해 호출자가 최소 키만 채워도 타입 검사를 통과하도록 두고, 카탈로그 evolution
시 새 키를 한 단계 더 늘려도 호환된다. 20번째 `tool_call` (`ToolCallPayload`) 은
v0.3.0 amendment (SPEC-AGENT-V2-REACT REQ-AGENT-LOG-EVENT-001) 로 추가됐다.

`__all__` 의 길이(=20)는 CI smoke test (`test_20_event_types_smoke.py`) 에서 강제
검증된다.

NOTE: 본 모듈은 *런타임 검증* 을 하지 않는다. 호출자가 잘못된 payload 를 넘기면 mypy /
pyright 단계에서만 잡힌다. payload sanitize / truncation 은 `conversation_log._truncate`
가 책임진다.
"""

# @MX:NOTE: [AUTO] SPEC-CONVERSATION-LOG-001 — 19 event payload TypedDicts (REQ-LOG-CATALOG-001)
# @MX:SPEC: SPEC-CONVERSATION-LOG-001

from __future__ import annotations

from typing import Literal, TypedDict

__all__ = [
    "UserTextPayload",
    "UserPhotoPayload",
    "UserCallbackPayload",
    "IntentRoutedPayload",
    "LinkResolvedPayload",
    "VisionDonePayload",
    "PickItemDonePayload",
    "AskClarifySentPayload",
    "ClarifyAppliedPayload",
    "SearchDonePayload",
    "EvaluatorRunPayload",
    "DiversifyDonePayload",
    "CardSentPayload",
    "CardClickedPayload",
    "OnboardSelectPayload",
    "PinterestIngestPayload",
    "BotTextPayload",
    "TasteUpdatePayload",
    "NodeErrorPayload",
    # SPEC-AGENT-V2-REACT — REQ-AGENT-LOG-EVENT-001 (20th event type).
    "ToolCallPayload",
]

# REQ-LOG-CATALOG-001 — taste_update.source 7-value Literal (catalog #18).
TasteSource = Literal[
    "click",
    "onboard",
    "pinterest",
    "critique",
    "free_text",
    "no_click",
    "re_query",
]


# 1. user_text — webhook intake
class UserTextPayload(TypedDict, total=False):
    text: str
    lang_detected: str  # "ko" | "en"


# 2. user_photo — webhook intake
class UserPhotoPayload(TypedDict, total=False):
    attachment_id: str
    image_url: str | None
    caption: str | None


# 3. user_callback — webhook intake (inline button tap)
class UserCallbackPayload(TypedDict, total=False):
    callback_data: str
    source_message_id: int


# 4. intent_routed — ingest node
class IntentRoutedPayload(TypedDict, total=False):
    intent: str
    critique_delta_summary: str | None


# 5. link_resolved — resolve_image node
class LinkResolvedPayload(TypedDict, total=False):
    input_url: str
    resolved_image_url: str | None
    host: str


# 6. vision_done — vision node (v2 schema)
class VisionDonePayload(TypedDict, total=False):
    style_node_primary: str | None
    style_node_secondary: str | None
    sensitivity_tags: list[str]
    mood: list[str]
    palette: list[str]
    style: str | None
    gender: str | None
    items: list[dict]
    schema_v2_used: bool
    error: str | None


# 7. pick_item_done — pick_item node
class PickItemDonePayload(TypedDict, total=False):
    candidate_items: list[dict]
    picked_index: int
    auto_picked: bool


# 8. ask_clarify_sent — ask_clarify node
class AskClarifySentPayload(TypedDict, total=False):
    axis: str
    options_shown: list[str]


# 9. clarify_applied — apply_clarify node
class ClarifyAppliedPayload(TypedDict, total=False):
    axis: str
    value: str
    boost_keywords_added: list[str]


# 10. search_done — search node (ML-critical, REQ-LOG-PAYLOAD-RICH-001)
class SearchDonePayload(TypedDict, total=False):
    query: dict
    embedding_ref: str | None
    top_k_product_ids: list[str]
    rrf_scores: list[float]
    dense_count: int
    sparse_count: int
    filter_drop_log: list[dict]


# 11. evaluator_run — evaluator node (per iteration)
class EvaluatorRunPayload(TypedDict, total=False):
    iteration_no: int
    score: float
    delta: dict | None
    retry_decision: str
    exhausted: bool


# 12. diversify_done — send_results / diversify stage
class DiversifyDonePayload(TypedDict, total=False):
    input_count: int
    output_count: int
    brand_cap: int
    platform_cap: int


# 13. card_sent — send_results node (per card)
class CardSentPayload(TypedDict, total=False):
    product_id: str
    position: int
    send_ok: bool
    send_elapsed_ms: int
    source_message_id: int  # NEW — callback correlation (plan §4.1)


# 14. card_clicked — critique_apply node (crit:click branch)
class CardClickedPayload(TypedDict, total=False):
    product_id: str
    position: int | None
    dwell_ms: int | None


# 15. onboard_select — SPEC-ONBOARD-CARDS-001 (deferred)
class OnboardSelectPayload(TypedDict, total=False):
    stage: str  # "mood" | "color" | "fit" | "pinterest" | "completion"
    axis: str
    selected_values: list[str]


# 16. pinterest_ingest — resolve_image node (Pinterest branch)
class PinterestIngestPayload(TypedDict, total=False):
    mode: str  # "board" | "profile" | "pin"
    pin_count: int
    vision_results_count: int


# 17. bot_text — respond node (per chunk)
class BotTextPayload(TypedDict, total=False):
    chunk_text: str
    chunk_index: int
    total_chunks: int
    flow: str  # _Flow enum value


# 18. taste_update — TasteProfile mutation event
class TasteUpdatePayload(TypedDict, total=False):
    source: TasteSource
    keywords_delta: dict
    brands_delta: dict


# 19. node_error — node except-block emission
class NodeErrorPayload(TypedDict, total=False):
    node_name: str
    exception_type: str
    message: str
    recovered: bool


# 20. tool_call — SPEC-AGENT-V2-REACT REQ-AGENT-LOG-EVENT-001 + REQ-AGENT-OBS-001.
# Emitted by react_loop after every tool dispatch (success or failure).
# Note: args / result_summary are SHAPE indicators only — full payloads pass through
# `_truncate` cap (REQ-AGENT-SEC-PAYLOAD-001 / REQ-LOG-PAYLOAD-CAP-001).
class ToolCallPayload(TypedDict, total=False):
    tool_name: str
    iteration_no: int
    latency_ms: int
    error: str | None
    args_summary: dict
    result_summary: dict
