"""SPEC-AGENT-V2-CLEANUP-001 — ingest never invokes the legacy route_text.

The ReAct agent is the only topology. `ingest` returns early after the
implicit-feedback + clarify-inline steps and never calls `route_text`; the
returned state carries no `decision` (log_events + turn_no only).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import SessionState, get_store


def _make_state(text: str = "casual blazer recommendation") -> WorkingState:
    msg = ChannelMessage(chat_id=7, from_user_id=99, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=7, from_user_id=99)


@pytest.fixture
def _idle_session():
    """Session in IDLE — the (removed) V1 needs_router gate would have fired."""
    sess = get_store().get_or_create(7)
    sess.state = SessionState.IDLE
    get_store().update(sess)
    return sess


@pytest.mark.asyncio
async def test_ingest_never_produces_a_decision(_idle_session):
    from app.graphs.nodes import ingest as ingest_mod

    # `route_text` is no longer imported by ingest at all.
    assert not hasattr(ingest_mod, "route_text")

    out = await ingest_mod.ingest(_make_state())

    # Same shape as the legacy `not needs_router` path: log_events + turn_no,
    # never a `decision`.
    assert "decision" not in out
    assert out["turn_no"] == 1
    assert isinstance(out["log_events"], list)
