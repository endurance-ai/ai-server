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
from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field

from app.channels.critique import CritiqueDelta
from app.channels.router import RoutedDecision
from app.channels.schemas import ChannelMessage
from app.channels.session import SessionState

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
    # plan.md Q4: capped at 3 producers (vision/critique/search).
    messages: Annotated[list[BaseMessage], _MESSAGES_REDUCER] = Field(default_factory=list)
    log_events: Annotated[list[str], _LIST_ADD] = Field(default_factory=list)


class OutputState(BaseModel):
    """REQ-STATE-004: what the webhook reads back to update SessionStore.

    Mirrors the per-turn outcome — counts and the final SessionState to persist.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    sent_count: int = 0
    final_state: SessionState = SessionState.IDLE
    response_text: str | None = None
