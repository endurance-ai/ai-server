"""SPEC-AGENT-V2-REACT — react_loop core engine tests.

Mocks LLM via langchain message stubs. Verifies:
- Iteration cap (REQ-AGENT-LOOP-ITERATION-001)
- Termination on respond (REQ-AGENT-LOOP-TERMINATION-001)
- Exhaustion fallback (REQ-AGENT-LOOP-EXHAUSTION-001)
- Tool exception caught (REQ-AGENT-FAILURE-TOOL-001)
- Infinite-loop guard (REQ-AGENT-FAILURE-INFINITE-001)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.schemas import ChannelMessage
from app.channels.session import Session, SessionState
from app.graphs.state import WorkingState


class _FakeAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.usage_metadata = {"total_tokens": 100}


class _FakeLLM:
    """LLM stub. Each `ainvoke` returns the next pre-canned AIMessage."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def ainvoke(self, messages):
        if not self._responses:
            return _FakeAIMessage([])
        return self._responses.pop(0)


def _make_state(text: str = "hello") -> WorkingState:
    from datetime import UTC, datetime

    msg = ChannelMessage(chat_id=42, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _make_session() -> Session:
    return Session(chat_id=42, state=SessionState.IDLE)


@pytest.mark.asyncio
async def test_respond_tool_terminates(monkeypatch):
    from app.agents import react_loop as rl

    fake = _FakeLLM([_FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "1"}])])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    # Mock adapter for the respond tool dispatcher.
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    state = _make_state()
    sess = _make_session()
    delta = await rl.run_react_loop(state, sess)
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 1
    mock_adapter.send_text.assert_awaited()


@pytest.mark.asyncio
async def test_iteration_cap_exhausts(monkeypatch):
    from app.agents import react_loop as rl

    # 7 responses of refine_search → never terminates → exhaustion.
    fake = _FakeLLM(
        [_FakeAIMessage([{"name": "refine_search", "args": {"action": "broaden"}, "id": str(i)}]) for i in range(10)]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(
        "app.agents.tools.refine_search.dispatch",
        AsyncMock(
            return_value={
                "ok": False,
                "error": "missing_image_url_in_ctx",
                "candidates_count": 0,
                "top_candidates": [],
            }
        ),
    )
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_MAX_ITERATIONS", 3, raising=False)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"
    assert delta["agent_iterations"] == 3


@pytest.mark.asyncio
async def test_fail_closed_when_llm_missing(monkeypatch):
    from app.agents import react_loop as rl

    monkeypatch.setattr(rl, "get_llm", lambda: None)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"
    assert delta["response_text"]  # fallback text emitted


@pytest.mark.asyncio
async def test_infinite_loop_guard(monkeypatch):
    """3 consecutive identical (tool_name, args) → exhausted."""
    from app.agents import react_loop as rl

    same_call = {"name": "get_recent_history", "args": {"n": 5}, "id": "x"}
    fake = _FakeLLM([_FakeAIMessage([same_call]) for _ in range(6)])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"


@pytest.mark.asyncio
async def test_json_malform_no_tool_calls(monkeypatch):
    from app.agents import react_loop as rl

    # Two empty tool_calls → exhaustion (2-strike).
    fake = _FakeLLM([_FakeAIMessage([]), _FakeAIMessage([])])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"


@pytest.mark.asyncio
async def test_assistant_tooluse_precedes_toolmessage_on_2nd_ainvoke(monkeypatch):
    """Regression guard for the Bedrock Nova multi-turn 400.

    Before the fix, the assistant tool_use AIMessage was never appended to
    `messages`, so the 2nd ainvoke saw [system, user, tool_result] with a
    tool_result that had no preceding assistant tool_use turn. Bedrock Nova
    (via LiteLLM) 400s: "Expected toolResult blocks ... for the following Ids".

    This test asserts that, at the point of the 2nd ainvoke, `messages`
    contains the assistant AIMessage (carrying tool_calls) IMMEDIATELY
    followed by a ToolMessage whose tool_call_id equals the tool call's id.
    """
    from langchain_core.messages import ToolMessage

    from app.agents import react_loop as rl

    captured: list[list] = []

    class _SpyLLM:
        def __init__(self, responses):
            self._responses = list(responses)

        async def ainvoke(self, messages):
            captured.append(list(messages))
            return self._responses.pop(0) if self._responses else _FakeAIMessage([])

    first = _FakeAIMessage([{"name": "get_recent_history", "args": {"n": 5}, "id": "tooluse_ABC"}])
    second = _FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "tooluse_DEF"}])
    fake = _SpyLLM([first, second])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "done"

    # The 2nd ainvoke's message list is what Bedrock would have rejected.
    assert len(captured) >= 2, "expected a 2nd ainvoke (multi-step turn)"
    msgs_2nd = captured[1]
    # Find the assistant tool_use turn (the raw ai_msg, carrying tool_calls)
    # appended before its tool result. In production this is a langchain
    # AIMessage; the fake mirrors its duck-typed shape (.tool_calls).
    ai_idx = next(
        i
        for i, m in enumerate(msgs_2nd)
        if not isinstance(m, (dict, ToolMessage)) and getattr(m, "tool_calls", None)
    )
    nxt = msgs_2nd[ai_idx + 1]
    assert isinstance(nxt, ToolMessage), "ToolMessage must immediately follow assistant tool_use"
    tc_id = first.tool_calls[0]["id"]
    assert nxt.tool_call_id == tc_id, (
        f"tool_call_id must match the tool call id exactly "
        f"(got {nxt.tool_call_id!r}, expected {tc_id!r})"
    )


@pytest.mark.asyncio
async def test_invalid_args_recorded_not_dispatched(monkeypatch):
    from app.agents import react_loop as rl

    bad_then_respond = [
        _FakeAIMessage([{"name": "respond", "args": {"bogus_field": True}, "id": "1"}]),
        _FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "2"}]),
    ]
    fake = _FakeLLM(bad_then_respond)
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    # Bad iter recorded, second iter succeeded.
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 2
    assert any("invalid_args" in (h.get("error") or "") for h in delta["tool_call_history"])
