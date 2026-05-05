"""SPEC-AGENT-001 / REQ-STATE-001..004 — three-tier Pydantic state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import SystemMessage
from pydantic import ValidationError

from app.channels.schemas import ChannelMessage
from app.channels.session import SessionState
from app.graphs.state import InputState, OutputState, WorkingState


def _msg(chat_id: int = 42) -> ChannelMessage:
    return ChannelMessage(chat_id=chat_id, received_at=datetime.now(tz=UTC))


# ── InputState ─────────────────────────────────────────────────────────────


def test_input_state_basic_construction():
    s = InputState(message=_msg(), chat_id=42, from_user_id=7)
    assert s.chat_id == 42
    assert s.from_user_id == 7


def test_input_state_rejects_extra_fields():
    """REQ-STATE-003 acceptance #2 — extra='forbid'."""
    with pytest.raises(ValidationError):
        InputState(message=_msg(), chat_id=42, bogus_key="x")


# ── WorkingState defaults ──────────────────────────────────────────────────


def test_working_state_defaults_match_spec():
    """REQ-STATE-002 — every non-Input field defaults per the field table."""
    s = WorkingState(message=_msg(), chat_id=42)
    assert s.decision is None
    assert s.image_url is None
    assert s.detected_items == []
    assert s.selected_item_index is None
    assert s.critique_delta is None
    assert s.candidates == []
    assert s.sent_candidates == []
    assert s.response_text is None
    assert s.presearch_summary is None
    assert s.messages == []
    assert s.log_events == []


# ── log_events reducer ─────────────────────────────────────────────────────


def test_log_events_reducer_concatenates_across_deltas():
    """REQ-STATE-002 acceptance #4 — `operator.add` reducer concatenates lists.

    Two state deltas appending log_events from different nodes must produce a
    concatenated list (not overwrite).
    """
    from langgraph.graph import START, StateGraph

    async def node_a(state: WorkingState) -> dict:
        return {"log_events": ["a:done"]}

    async def node_b(state: WorkingState) -> dict:
        return {"log_events": ["b:done"]}

    builder = StateGraph(WorkingState)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    graph = builder.compile()

    import asyncio

    final = asyncio.run(graph.ainvoke(InputState(message=_msg(), chat_id=42)))
    assert final["log_events"] == ["a:done", "b:done"]


def test_messages_reducer_uses_add_messages():
    """REQ-STATE-002 — `messages: Annotated[..., add_messages]`."""
    from langgraph.graph import START, StateGraph

    async def node_a(state: WorkingState) -> dict:
        return {"messages": [SystemMessage(content="a")]}

    async def node_b(state: WorkingState) -> dict:
        return {"messages": [SystemMessage(content="b")]}

    builder = StateGraph(WorkingState)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    graph = builder.compile()

    import asyncio

    final = asyncio.run(graph.ainvoke(InputState(message=_msg(), chat_id=42)))
    contents = [m.content for m in final["messages"]]
    assert contents == ["a", "b"]


# ── OutputState ────────────────────────────────────────────────────────────


def test_output_state_defaults():
    """REQ-STATE-004 — sent_count + final_state + response_text."""
    o = OutputState(sent_count=1, final_state=SessionState.RESULTS_SENT, response_text="ok")
    assert o.sent_count == 1
    assert o.final_state == SessionState.RESULTS_SENT
    assert o.response_text == "ok"


def test_output_state_rejects_extra_fields():
    with pytest.raises(ValidationError):
        OutputState(sent_count=1, final_state=SessionState.IDLE, bogus=True)
