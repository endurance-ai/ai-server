"""SPEC-AGENT-V3-REACT / T6 — Gap2 in-loop wiring + bound + flag-off.

REQ-AGENT-V3-REFLEX-BOUND-001 / -REFLEX-FLAG-001 / -REFLEX-DELTA-001.
AC-2.1 (autonomous refine), AC-2.3 (cap + infinite-loop no-conflict),
AC-2.4 (flag OFF byte-identical + 0 evaluator calls).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import react_loop as rl
from app.channels.schemas import ChannelMessage
from app.channels.session import Session, SessionState
from app.core.config import settings
from app.graphs.state import WorkingState


class _FakeAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.usage_metadata = {"total_tokens": 10}


class _FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.seen: list = []

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return self._responses.pop(0) if self._responses else _FakeAIMessage([])


def _state() -> WorkingState:
    from datetime import UTC, datetime

    msg = ChannelMessage(chat_id=42, text="cheap bag", received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _sess() -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE)
    s.lang = "en"
    return s


@pytest.fixture
def _adapter(monkeypatch):
    a = MagicMock()
    a.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: a)
    return a


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(rl, "_LLM_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))


@pytest.mark.asyncio
async def test_ac_2_1_quality_merged_then_autonomous_refine(monkeypatch, _adapter):
    """AC-2.1 — low score merged into ToolMessage; LLM autonomously refines."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)

    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents.tools.refine_search.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 8, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents._reflexion.evaluate_search_quality",
        AsyncMock(return_value={"score": 0.3, "retry_suggested": True, "reason": "weak"}),
    )

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "cheap bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "refine_search", "args": {"action": "broaden"}, "id": "2"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "here"}, "id": "3"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "done"
    # 2nd ainvoke saw the ToolMessage with _quality merged.
    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    payload = json.loads(tool_msgs[-1].content)
    assert payload["_quality"]["score"] == 0.3
    assert payload["_quality"]["retry_suggested"] is True


@pytest.mark.asyncio
async def test_ac_2_1_high_score_llm_may_respond(monkeypatch, _adapter):
    """AC-2.1 — high score still merged, LLM free to respond (no forced refine)."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 9, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents._reflexion.evaluate_search_quality",
        AsyncMock(return_value={"score": 0.9, "retry_suggested": False, "reason": "good"}),
    )
    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "great"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 2  # no forced extra refine


@pytest.mark.asyncio
async def test_ac_2_3_evaluator_call_capped(monkeypatch, _adapter):
    """AC-2.3 — 6 search calls but evaluator capped at SELF_CRITIQUE_MAX_ITERATIONS."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SELF_CRITIQUE_MAX_ITERATIONS", 2, raising=False)
    monkeypatch.setattr(settings, "AGENT_MAX_ITERATIONS", 8, raising=False)

    # Distinct args each call so the infinite-loop guard does NOT fire here.
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 3, "top_candidates": []}),
    )
    eval_spy = AsyncMock(return_value={"score": 0.5, "retry_suggested": True, "reason": "x"})
    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", eval_spy)

    llm = _FakeLLM(
        [_FakeAIMessage([{"name": "search_products", "args": {"text_query": f"q{i}"}, "id": str(i)}]) for i in range(6)]
        + [_FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "9"}])]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    assert eval_spy.await_count == 2  # capped at SELF_CRITIQUE_MAX_ITERATIONS


@pytest.mark.asyncio
async def test_ac_2_3_infinite_loop_guard_and_history_untouched(monkeypatch, _adapter):
    """AC-2.3 — identical args 3x → infinite-loop guard fires; Reflexion does
    not pollute tool_call_history (history identical pre/post evaluation)."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SELF_CRITIQUE_MAX_ITERATIONS", 5, raising=False)

    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 3, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents._reflexion.evaluate_search_quality",
        AsyncMock(return_value={"score": 0.5, "retry_suggested": True, "reason": "x"}),
    )
    same = {"name": "search_products", "args": {"text_query": "same"}, "id": "z"}
    llm = _FakeLLM([_FakeAIMessage([same]) for _ in range(6)])
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "exhausted"  # infinite-loop guard fired
    # History only carries genuine tool dispatches — no evaluator entries.
    assert all(h["tool_name"] == "search_products" for h in delta["tool_call_history"])


@pytest.mark.asyncio
async def test_ac_2_4_flag_off_byte_identical_no_evaluator(monkeypatch, _adapter):
    """AC-2.4 — flag OFF: ToolMessage == V2 json.dumps(...)[:2000], no _quality,
    evaluator path 0 calls."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", False, raising=False)

    result = {"ok": True, "candidates_count": 5, "top_candidates": [{"brand": "ami"}]}
    monkeypatch.setattr("app.agents.tools.search_products.dispatch", AsyncMock(return_value=result))
    eval_spy = AsyncMock(return_value={"score": 0.1})
    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", eval_spy)

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    eval_spy.assert_not_awaited()
    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    assert tool_msgs[-1].content == json.dumps(result, default=str)[:2000]
    assert "_quality" not in tool_msgs[-1].content
