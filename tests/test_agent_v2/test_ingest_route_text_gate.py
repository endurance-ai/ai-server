"""SPEC-AGENT-V2-REACT / T-007 §15 Decision 1 — ingest V2 skips route_text.

V2 active (flag=true AND AGENT_LLM_MODEL set) → `route_text` MUST NOT be called.
V1 (flag=false) → `route_text` IS called + `decision` returned (byte-identical).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import SessionState, get_store


def _make_state(text: str = "casual blazer recommendation") -> WorkingState:
    msg = ChannelMessage(chat_id=7, from_user_id=99, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=7, from_user_id=99)


@pytest.fixture
def _idle_session():
    """Ensure the session is in IDLE so the V1 needs_router gate would fire."""
    sess = get_store().get_or_create(7)
    sess.state = SessionState.IDLE
    get_store().update(sess)
    return sess


@pytest.mark.asyncio
async def test_v2_skips_route_text(monkeypatch, _idle_session):
    from app.graphs.nodes import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.settings, "AGENT_V2_REACT_ENABLED", True, raising=False)
    monkeypatch.setattr(ingest_mod.settings, "AGENT_LLM_MODEL", "nova-lite", raising=False)
    route_text_spy = AsyncMock()
    monkeypatch.setattr(ingest_mod, "route_text", route_text_spy)

    out = await ingest_mod.ingest(_make_state())

    route_text_spy.assert_not_called()
    # Same shape as the `not needs_router` path: log_events + turn_no, no decision.
    assert "decision" not in out
    assert out["turn_no"] == 1
    assert isinstance(out["log_events"], list)


@pytest.mark.asyncio
async def test_v1_still_calls_route_text(monkeypatch, _idle_session):
    from app.channels.router import RoutedDecision, RoutedIntent
    from app.graphs.nodes import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.settings, "AGENT_V2_REACT_ENABLED", False, raising=False)
    monkeypatch.setattr(ingest_mod.settings, "AGENT_LLM_MODEL", "", raising=False)
    decision = RoutedDecision(intent=RoutedIntent.NEW_SEARCH_REQUEST)
    route_text_spy = AsyncMock(return_value=decision)
    monkeypatch.setattr(ingest_mod, "route_text", route_text_spy)

    out = await ingest_mod.ingest(_make_state())

    route_text_spy.assert_called_once()
    assert out["decision"] is decision
    assert out["turn_no"] == 1


@pytest.mark.asyncio
async def test_v2_fail_closed_when_model_unset_calls_route_text(monkeypatch, _idle_session):
    """flag=true but AGENT_LLM_MODEL empty → fail-closed → V1 route_text path."""
    from app.channels.router import RoutedDecision, RoutedIntent
    from app.graphs.nodes import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.settings, "AGENT_V2_REACT_ENABLED", True, raising=False)
    monkeypatch.setattr(ingest_mod.settings, "AGENT_LLM_MODEL", "", raising=False)
    route_text_spy = AsyncMock(return_value=RoutedDecision(intent=RoutedIntent.OFF_TOPIC))
    monkeypatch.setattr(ingest_mod, "route_text", route_text_spy)

    await ingest_mod.ingest(_make_state())

    route_text_spy.assert_called_once()
