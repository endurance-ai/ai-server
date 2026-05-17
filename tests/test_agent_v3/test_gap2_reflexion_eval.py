"""SPEC-AGENT-V3-REACT / T5 — Gap2 Reflexion eval wrapper (unit).

REQ-AGENT-V3-REFLEX-EVAL-001 / -REFLEX-DELTA-001 / REQ-AGENT-V3-COMPAT-WRAP-001.
AC-2.2 (wrap evidence). The in-loop wiring is T6; this is the wrapper unit.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents import _reflexion
from app.channels.session import Session, SessionState
from app.graphs.nodes._evaluator_models import CritiqueScore
from app.graphs.state import WorkingState


def _state() -> WorkingState:
    from datetime import UTC, datetime

    from app.channels.schemas import ChannelMessage

    msg = ChannelMessage(chat_id=42, text="cheap bag", received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _sess(results) -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE)
    s.last_results = results
    s.user_intent = "wants a cheap bag"
    return s


@pytest.mark.asyncio
async def test_ac_2_2_wraps_call_llm_exactly_once(monkeypatch):
    """AC-2.2 — 5 candidates → evaluator._call_llm called exactly once,
    its CritiqueScore is the propagated source of truth."""
    spy = AsyncMock(return_value=CritiqueScore(score=0.3, reasoning="off-target", retry=True))
    monkeypatch.setattr("app.graphs.nodes.evaluator._call_llm", spy)

    cands = [SimpleNamespace(brand="b", name=f"n{i}", subcategory="bag", price=10) for i in range(5)]
    out = await _reflexion.evaluate_search_quality(_state(), _sess(cands), {})

    spy.assert_awaited_once()
    assert out["score"] == 0.3
    assert out["retry_suggested"] is True
    assert "off-target" in out["reason"]


@pytest.mark.asyncio
async def test_empty_results_uses_fastpath_no_llm(monkeypatch):
    """REQ-AGENT-V3-REFLEX-EVAL-001 — 0 candidates → fast-path, NO LLM call."""
    spy = AsyncMock(return_value=CritiqueScore(score=0.9, retry=False))
    monkeypatch.setattr("app.graphs.nodes.evaluator._call_llm", spy)

    out = await _reflexion.evaluate_search_quality(_state(), _sess([]), {})
    spy.assert_not_awaited()
    assert out["score"] == 0.0
    assert out["retry_suggested"] is True
    assert "broaden" in out["reason"]


@pytest.mark.asyncio
async def test_fail_open_propagated_verbatim(monkeypatch):
    """AC-2.2 — evaluator timeout fail-open (score=1.0, retry=False) passes
    through unchanged (proves no reimplementation)."""
    from app.graphs.nodes import evaluator as ev

    async def _timeout_call(_prompt):
        return ev._fail_open_score("timeout")

    monkeypatch.setattr(ev, "_call_llm", _timeout_call)
    cands = [SimpleNamespace(brand="b", name="n", subcategory="bag", price=1)]
    out = await _reflexion.evaluate_search_quality(_state(), _sess(cands), {})
    assert out["score"] == 1.0
    assert out["retry_suggested"] is False


@pytest.mark.asyncio
async def test_high_score_no_retry(monkeypatch):
    """REQ-AGENT-V3-REFLEX-DELTA-001 — high score → retry_suggested False."""
    monkeypatch.setattr(
        "app.graphs.nodes.evaluator._call_llm",
        AsyncMock(return_value=CritiqueScore(score=0.95, retry=False)),
    )
    cands = [SimpleNamespace(brand="b", name="n", subcategory="bag", price=1)]
    out = await _reflexion.evaluate_search_quality(_state(), _sess(cands), {})
    assert out["score"] == 0.95
    assert out["retry_suggested"] is False


# ── BLOCKING-2 — _reflexion helper-import fail-open coverage ───────────────


@pytest.mark.asyncio
async def test_reflexion_helper_import_failure_fail_open(monkeypatch):
    """_reflexion L58-60 — evaluator helper import failing → fail-open
    (score=1.0, retry=False, reason=evaluator_unavailable), never raises."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *a, **kw):
        if name == "app.graphs.nodes.evaluator":
            raise ImportError("evaluator module gone")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    cands = [SimpleNamespace(brand="b", name="n", subcategory="bag", price=1)]
    out = await _reflexion.evaluate_search_quality(_state(), _sess(cands), {})
    assert out == {"score": 1.0, "retry_suggested": False, "reason": "evaluator_unavailable"}
