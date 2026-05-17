"""SPEC-AGENT-V3-REACT / T10 — performance guards.

REQ-AGENT-V3-PERF-001 / AC-P.2. Full 200-turn p95<8s load is an
operational/manual gate (V2 harness reuse, documented in plan.md runbook).
Here we mechanically assert the bounded unit invariants that GUARANTEE the
inherited budget is not exceeded:
  - build_memory_context assembly overhead < 50ms (AC-P.2)
  - slow-evaluator residual-budget cancel keeps a turn within turn_deadline
    (the p95-inflation guard; AC-2.5/AC-P.2 consistency)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import _memory_context
from app.agents import react_loop as rl
from app.channels.schemas import ChannelMessage
from app.core.config import settings
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState

# Taste-store reset + settings snapshot/restore handled centrally by
# tests/test_agent_v3/conftest.py::_v3_isolation (autouse).


@pytest.mark.asyncio
async def test_ac_p_2_memory_assembly_under_50ms(monkeypatch):
    """AC-P.2 — build_memory_context assembly overhead < 50ms."""
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:99")
    prof.liked_brands = {f"b{i}": float(i) for i in range(50)}
    prof.liked_keywords = {f"k{i}": float(i) for i in range(50)}
    get_taste_store().update(prof)

    async def _grh(args, ctx):
        return {
            "ok": True,
            "events": [{"event_type": "user_text", "payload_summary": {"text": "hi"}} for _ in range(5)],
        }

    monkeypatch.setattr("app.agents.tools.get_recent_history.dispatch", _grh)

    ctx = {"user_key": "u:99"}
    # Warm + measure best-of-3 to exclude import jitter.
    await _memory_context.build_memory_context(None, None, ctx, max_tokens=1500)
    samples = []
    for _ in range(3):
        t0 = time.monotonic()
        await _memory_context.build_memory_context(None, None, ctx, max_tokens=1500)
        samples.append(time.monotonic() - t0)
    best = min(samples)
    assert best < 0.05, f"memory assembly too slow: {best * 1000:.1f}ms"


@pytest.mark.asyncio
async def test_ac_p_2_slow_evaluator_does_not_inflate_turn(monkeypatch):
    """AC-P.2 — a 20s evaluator stub is residual-cancelled so the turn does
    NOT inflate toward EVALUATOR_TIMEOUT_S; turn stays within a tight bound."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENT_MAX_ITERATIONS", 1, raising=False)
    monkeypatch.setattr(settings, "AGENT_LLM_TIMEOUT_S", 0.15, raising=False)
    monkeypatch.setattr(settings, "AGENT_TOOL_TIMEOUT_S", 0.15, raising=False)
    monkeypatch.setattr(rl, "_LLM_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))

    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": []}),
    )

    async def _slow(state, sess, ctx):
        await asyncio.sleep(20.0)
        return {"score": 0.1}

    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", _slow)

    class _LLM:
        def __init__(self):
            self._r = [
                _AI([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
                _AI([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
            ]

        async def ainvoke(self, m):
            return self._r.pop(0) if self._r else _AI([])

    a = MagicMock()
    a.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: a)
    monkeypatch.setattr(rl, "get_llm", lambda: _LLM())

    msg = ChannelMessage(
        chat_id=42, text="bag", received_at=__import__("datetime").datetime.now(__import__("datetime").UTC)
    )
    state = WorkingState(message=msg, chat_id=42, from_user_id=99)
    sess = Session(chat_id=42, state=SessionState.IDLE)
    sess.lang = "en"

    t0 = time.monotonic()
    await rl.run_react_loop(state, sess)
    elapsed = time.monotonic() - t0
    # Without the residual-cancel this would be ~20s (or 8s at EVALUATOR_TIMEOUT_S).
    assert elapsed < 5.0, f"slow evaluator inflated the turn to {elapsed:.1f}s"


class _AI:
    def __init__(self, tc):
        self.tool_calls = tc
        self.usage_metadata = {"total_tokens": 10}
