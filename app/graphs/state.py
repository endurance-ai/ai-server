"""SPEC-AGENT-001 — Three-tier Pydantic state for the LangGraph fashion bot.

Layered narrowest → widest:
    InputState     — what the webhook hands to the graph (read-only for nodes).
    WorkingState   — per-turn scratchpad threaded through every node.
    OutputState    — what the webhook reads back at graph completion.

Reducers (REQ-STATE-002):
    messages     — `langgraph.graph.message.add_messages` so respond/ask_clarify
                   see in-turn assistant/system messages from upstream nodes
                   (vision_node, critique_apply, search_node — see plan.md Q4).
    log_events   — `operator.add` so any node can append a structured breadcrumb
                   without overwriting prior nodes' entries.

Session and TasteProfile remain OUTSIDE the graph (REQ-STATE-005). Nodes read /
write the existing in-memory stores via the existing module APIs.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field

from app.channels.critique import CritiqueDelta
from app.channels.router import RoutedDecision
from app.channels.schemas import ChannelMessage
from app.infrastructure.memory.session import SessionState

__all__ = ["InputState", "OutputState", "WorkingState"]


# Reducer aliases — kept here for readability inside the field declarations
# below. langgraph's `add_messages` accumulates messages with smart merging;
# `operator.add` is plain list concatenation.
_MESSAGES_REDUCER = add_messages
_LIST_ADD = operator.add


class InputState(BaseModel):
    """REQ-STATE-003: what the webhook hands to the graph.

    Read-only for nodes — they consume but never mutate. Pydantic v2
    `extra="forbid"` rejects unknown keys (so the SPEC's input contract
    cannot drift silently).
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    message: ChannelMessage
    chat_id: int
    from_user_id: int | None = None

    # SPEC-CONVERSATION-LOG-001 / REQ-LOG-THREAD-001 / REQ-LOG-TURN-001 —
    # thread_id seeds at webhook intake (or copied from prior `card_sent` row
    # for callback Updates per REQ-LOG-THREAD-CALLBACK-001). turn_no increments
    # node-by-node (see plan §4 mapping table).
    thread_id: UUID = Field(default_factory=uuid4)
    turn_no: int = 0

    # 앱 이미지 인풋(스테이징)에서 사용자가 이미 항목을 골랐을 때 True — 이미지가
    # 첨부돼도 pick_item(1,2,3,4) 재선택을 건너뛰고 바로 검색으로 간다.
    skip_item_pick: bool = False

    # Per-request search filters from the consumer mobile filter UI (chat API).
    # Threaded into the tool dispatch ctx by react_loop._build_ctx so
    # search_products / refine_search can apply them. Telegram intake leaves
    # these None (defaults) → behavior unchanged on that path.
    # req_gender: normalized taste token (men/women/unisex) — per-request only,
    # never persisted to the taste profile (SPEC-GENDER-PIN-001).
    # req_price_max: upper price bound in KRW integer 원 (None → no ceiling).
    req_gender: str | None = None
    req_price_max: int | None = None

    # SPEC-DAILY-TOKEN-CAP-001 — 일일 토큰 캡을 청구할 주체.
    #
    # `chat_id` 를 쓰면 안 된다. 91a8c1a(앱 채팅 세션 격리) 이후 앱/웹 경로의
    # `chat_id` 는 '사람' 이 아니라 '대화' 를 뜻한다 — 대화마다 값이 바뀌므로
    # 거기 캡을 얹으면 카운터가 세션별로 흩어져 영구히 한도에 닿지 않는다.
    # 앱/웹(chat_service)은 계정 파생 id 를 명시적으로 넣고, Telegram(레거시)은
    # None 으로 두어 `chat_id` 폴백을 쓴다 — 그 경로에선 chat_id 가 곧 사람이다.
    cap_subject_id: int | None = None


class WorkingState(InputState):
    """REQ-STATE-001/REQ-STATE-002: per-turn scratchpad threaded through nodes.

    Each node consumes the narrowest type it needs and returns a state delta
    (a dict — the StateGraph merges it into the running WorkingState).

    The two reducer fields (`messages`, `log_events`) are append-only across
    nodes; all other fields are last-writer-wins (default Pydantic merge).
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    decision: RoutedDecision | None = None
    image_url: str | None = None
    detected_items: list[dict[str, Any]] = Field(default_factory=list)
    selected_item_index: int | None = None
    critique_delta: CritiqueDelta | None = None
    candidates: list[Any] = Field(default_factory=list)
    sent_candidates: list[Any] = Field(default_factory=list)
    response_text: str | None = None
    presearch_summary: str | None = None
    # SPEC-VISION-UNIFY-001 / REQ-VISION-STATE-001 — rich Vision context.
    # Stored as `Any` to avoid eager import of vision module at state-construction.
    vision_result: Any | None = None
    vision_selected_item: Any | None = None
    vision_outfit_style_node_primary: str | None = None
    vision_outfit_style_node_secondary: str | None = None
    vision_outfit_mood_tags: list[str] = Field(default_factory=list)
    vision_outfit_gender: str | None = None
    # SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-RETRY-002 — self-critique loop state.
    # `Any` chosen to avoid eager import of evaluator_models; the node validates types.
    critique_retry_count: int = 0
    critique_trail: Annotated[list[dict[str, Any]], _LIST_ADD] = Field(default_factory=list)
    critique_pending_delta: Any | None = None
    critique_exhausted: bool = False
    critique_started_at_ms: float | None = None
    # Previous iteration's candidates — kept so REQ-CRITIQUE-LOOP-SAFETY-002a
    # can ship the better-scoring set on score regression.
    critique_previous_candidates: list[Any] | None = None
    critique_previous_score: float | None = None
    # SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-STATE-002 — clarify card 추적용 스크래치패드.
    # `Any`는 ClarifyAxis / ClarifyDelta 의 forward import 사이클을 피하기 위함.
    clarify_axis: Any | None = None
    clarify_value: str | None = None
    clarify_delta: Any | None = None
    # plan.md Q4: capped at 3 producers (vision/critique/search).
    messages: Annotated[list[BaseMessage], _MESSAGES_REDUCER] = Field(default_factory=list)
    log_events: Annotated[list[str], _LIST_ADD] = Field(default_factory=list)

    # SPEC-ONBOARD-LITE-001 — the onboarding scratchpad fields
    # (onboard_stage, onboard_selections, onboard_card_message_id,
    # continuous_origin, onboard_pin_weights) were removed with the card
    # subgraph. `onboarded_at` lives on `Session` (first-touch discriminator).

    # SPEC-AGENT-V2-REACT / REQ-AGENT-COMPAT-STATE-001 — ReAct loop scratchpad.
    # Three additive fields, no existing field altered. `tool_call_history` uses
    # `_LIST_ADD` so LangGraph merges across iterations append-only (matches
    # `critique_trail` reducer pattern).
    agent_iterations: int = 0
    tool_call_history: Annotated[list[dict[str, Any]], _LIST_ADD] = Field(default_factory=list)
    agent_status: Literal["running", "done", "exhausted"] = "running"


class OutputState(BaseModel):
    """REQ-STATE-004: what the webhook reads back to update SessionStore.

    Mirrors the per-turn outcome — counts and the final SessionState to persist.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    sent_count: int = 0
    final_state: SessionState = SessionState.IDLE
    response_text: str | None = None
    # SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-RETRY-003 — softens respond reply.
    critique_exhausted: bool = False
