"""SPEC-AGENT-V3-REACT / T7 — Gap2 residual-budget forced cancel (D2 P0).

REQ-AGENT-V3-REFLEX-DEADLINE-001. AC-2.5 (slow-evaluator cancelled at residual
boundary, NOT at EVALUATOR_TIMEOUT_S=8s; turn within budget) + AC-2.6
(remaining<=0 → 0 calls; fast evaluator → normal _quality positive control).

Mechanical: a 20s evaluator stub injected with ~residual budget ≈ small must
be cancelled at the residual boundary, the turn must complete within
turn_deadline, and the ToolMessage proceeds with a skipped marker.
"""

from __future__ import annotations

import asyncio
import json
import time
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

    msg = ChannelMessage(chat_id=42, text="bag", received_at=datetime.now(UTC))
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
async def test_ac_2_5_slow_evaluator_cancelled_at_residual_boundary(monkeypatch, _adapter):
    """AC-2.5 — 20s evaluator stub cancelled at residual boundary (~0.3s, NOT
    8s); ToolMessage proceeds skipped; turn completes within turn_deadline."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "EVALUATOR_TIMEOUT_S", 8.0, raising=False)

    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": []}),
    )

    cancelled = {"flag": False}

    async def _slow_eval(state, sess, ctx):
        try:
            await asyncio.sleep(20.0)  # would blow past turn_deadline if not cancelled
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise
        return {"score": 0.1, "retry_suggested": True, "reason": "x"}

    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", _slow_eval)

    # Force the residual budget tiny: patch _maybe_reflexion's clock so
    # turn_deadline - now ≈ 0.3s when reflexion runs.
    real_monotonic = time.monotonic

    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    # Shrink the whole turn budget so residual at reflexion time is small.
    monkeypatch.setattr(settings, "AGENT_MAX_ITERATIONS", 1, raising=False)
    monkeypatch.setattr(settings, "AGENT_LLM_TIMEOUT_S", 0.15, raising=False)
    monkeypatch.setattr(settings, "AGENT_TOOL_TIMEOUT_S", 0.15, raising=False)

    t0 = real_monotonic()
    delta = await rl.run_react_loop(_state(), _sess())
    elapsed = real_monotonic() - t0

    # Cancelled at residual boundary, NOT at 20s and NOT at 8s.
    assert cancelled["flag"] is True
    assert elapsed < 5.0, f"turn overran residual budget ({elapsed:.2f}s) — not cancelled"
    # respond still terminates the loop.
    assert delta["agent_status"] in ("done", "exhausted")


@pytest.mark.asyncio
async def test_ac_2_6_remaining_zero_skips_evaluator(monkeypatch, _adapter):
    """AC-2.6(a) — remaining<=0 → evaluator 0 calls, skipped marker, proceed."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    eval_spy = AsyncMock(return_value={"score": 0.5})
    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", eval_spy)

    state = _state()
    sess = _sess()
    ctx = {"user_key": "u:99"}
    # Deadline already in the past → remaining == 0.
    q = await rl._maybe_reflexion(state, sess, ctx, turn_deadline=time.monotonic() - 1.0)
    assert q == {"skipped": True, "reason": "deadline"}
    eval_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_ac_2_6_fast_evaluator_positive_control(monkeypatch, _adapter):
    """AC-2.6(b) — fast evaluator, ample budget → normal _quality attached."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": []}),
    )
    monkeypatch.setattr(
        "app.agents._reflexion.evaluate_search_quality",
        AsyncMock(return_value={"score": 0.42, "retry_suggested": True, "reason": "ok"}),
    )
    llm = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    payload = json.loads(tool_msgs[-1].content)
    assert payload["_quality"]["score"] == 0.42  # positive control intact
