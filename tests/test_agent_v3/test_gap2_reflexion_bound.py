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
from app.core.config import settings
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState


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
    """AC-2.1 — low score merged into ToolMessage; LLM autonomously refines.

    Scope-narrow update (2026-05-20): Reflexion now only fires on empty results
    (candidates_count == 0). search → 0 results triggers eval → low score → LLM
    autonomously refines → refine returns 0 again → eval fires again.
    """

    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 0, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents.tools.refine_search.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 0, "top_candidates": []}),
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
    monkeypatch.setattr(settings, "SELF_CRITIQUE_MAX_ITERATIONS", 2, raising=False)
    monkeypatch.setattr(settings, "AGENT_MAX_ITERATIONS", 8, raising=False)

    # Distinct args each call so the infinite-loop guard does NOT fire here.
    # candidates_count=0 to trigger Reflexion under new scope (empty-results only).
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 0, "top_candidates": []}),
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
async def test_reflexion_fires_on_empty_results_quality_merged(monkeypatch, _adapter):
    """SCOPE-NARROWED (2026-05-20) — Reflexion fires ONLY on empty results
    (candidates_count == 0). Empty search result gets the `_quality` marker
    merged and the evaluator path IS invoked. Non-empty searches skip
    reflexion entirely (covered by test_reflexion_skipped_on_nonempty_results).
    """
    result = {"ok": True, "candidates_count": 0, "top_candidates": []}
    monkeypatch.setattr("app.agents.tools.search_products.dispatch", AsyncMock(return_value=result))
    eval_spy = AsyncMock(return_value={"score": 0.1, "retry_suggested": False})
    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", eval_spy)

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    eval_spy.assert_awaited()
    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    assert "_quality" in tool_msgs[-1].content


@pytest.mark.asyncio
async def test_reflexion_skipped_on_nonempty_results(monkeypatch, _adapter):
    """SCOPE-NARROWED (2026-05-20) — Reflexion does NOT fire when search returns
    >0 candidates. evaluator stub MUST NOT be awaited; _quality MUST NOT appear
    in the ToolMessage (LLM proceeds straight to respond).
    """
    result = {"ok": True, "candidates_count": 15, "top_candidates": [{"brand": "ami"}]}
    monkeypatch.setattr("app.agents.tools.search_products.dispatch", AsyncMock(return_value=result))
    eval_spy = AsyncMock(return_value={"score": 0.1, "retry_suggested": True})
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
    assert "_quality" not in tool_msgs[-1].content


@pytest.mark.asyncio
async def test_b7_directive_injected_when_retry_suggested(monkeypatch, _adapter):
    """B7 — empty search + retry_suggested → ToolMessage MUST carry an explicit
    `_directive` forbidding silence (LLM was observed ignoring `_quality` alone).
    """
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 0, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents._reflexion.evaluate_search_quality",
        AsyncMock(return_value={"score": 0.0, "retry_suggested": True, "reason": "empty"}),
    )

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "x"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "sorry"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    content = tool_msgs[-1].content
    assert "_directive" in content
    assert "silence is forbidden" in content


@pytest.mark.asyncio
async def test_b7_no_directive_when_retry_not_suggested(monkeypatch, _adapter):
    """Negative guard — when reflexion says no retry, do NOT inject the
    directive (avoid pushing the LLM toward an unnecessary refine)."""
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 0, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents._reflexion.evaluate_search_quality",
        AsyncMock(return_value={"score": 0.9, "retry_suggested": False, "reason": "fine"}),
    )

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "x"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    assert "_directive" not in tool_msgs[-1].content


@pytest.mark.asyncio
async def test_b9_second_identical_search_blocked_without_dispatch(monkeypatch, _adapter):
    """B9 — 2nd identical search call must NOT dispatch the pipeline. The LLM
    receives a `duplicate_call` synthetic result with a `_directive` nudging
    it to vary args or respond."""
    dispatch_spy = AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": [{"brand": "x"}]})
    monkeypatch.setattr("app.agents.tools.search_products.dispatch", dispatch_spy)

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "grey jeans"}, "id": "1"}]),
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "grey jeans"}, "id": "2"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "3"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    assert dispatch_spy.await_count == 1  # only the 1st identical call dispatched

    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in llm.seen[-1] if isinstance(m, ToolMessage)]
    dup_msgs = [m for m in tool_msgs if "duplicate_call" in m.content]
    assert dup_msgs, "expected a duplicate_call synthetic ToolMessage"
    assert "_directive" in dup_msgs[-1].content


@pytest.mark.asyncio
async def test_b9_three_identical_still_exhausts(monkeypatch, _adapter):
    """The 2-strike soft guard must NOT defang the existing 3-strike exhaust
    safety net. Three identical calls still trip the infinite-loop guard."""
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 3, "top_candidates": []}),
    )

    same = {"name": "search_products", "args": {"text_query": "same"}, "id": "z"}
    llm = _FakeLLM([_FakeAIMessage([same]) for _ in range(6)])
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "exhausted"


@pytest.mark.asyncio
async def test_b9_different_args_not_blocked(monkeypatch, _adapter):
    """The 2-strike soft guard only fires on IDENTICAL args. Different args
    must still dispatch normally."""
    dispatch_spy = AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": [{"brand": "x"}]})
    monkeypatch.setattr("app.agents.tools.search_products.dispatch", dispatch_spy)

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "q1"}, "id": "1"}]),
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "q2"}, "id": "2"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "3"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    assert dispatch_spy.await_count == 2  # both dispatched, no soft block
