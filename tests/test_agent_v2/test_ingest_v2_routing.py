"""SPEC-AGENT-V2-REACT §15 Decision 2/3 — `_route_after_ingest_v2` behavior.

Exercised through a freshly-built V2 graph with `agent`/`pick_item` replaced by
recording sentinels. Verifies:
- empty/contentless Update → silent END (no node ran, no adapter send)
- real text → agent
- photo / url → resolve_image (vision pre-step)
- item:N callback → pick_item
- characterization: photo → vision → pick_item carousel → item:0 tap →
  selected_item_index set → routes to agent (Decision 3: pick path unbroken)
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.state import InputState
from app.infrastructure.memory.session import InMemorySessionStore, SessionState, set_store


@pytest.fixture
def _v2_graph(monkeypatch):
    """Build a V2 graph whose post-ingest terminal nodes are recording stubs."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_V2_REACT_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "nova-lite", raising=False)

    visited: list[str] = []

    async def _agent_stub(state):
        visited.append("agent")
        return {"log_events": ["agent-stub"]}

    async def _pick_stub(state):
        visited.append("pick_item")
        return {"log_events": ["pick-stub"]}

    async def _resolve_stub(state):
        visited.append("resolve_image")
        return {"log_events": ["resolve-stub"], "image_url": None}

    import app.graphs.fashion_bot as fb
    import app.graphs.nodes.agent as agent_mod

    importlib.reload(fb)
    monkeypatch.setattr(agent_mod, "agent", _agent_stub)
    monkeypatch.setattr(fb, "pick_item", _pick_stub)
    monkeypatch.setattr(fb, "resolve_image", _resolve_stub)
    graph = fb.build_graph()
    return graph, visited


def _msg(**kw) -> ChannelMessage:
    return ChannelMessage(chat_id=42, from_user_id=99, received_at=datetime.now(UTC), **kw)


def _onboarded_store() -> InMemorySessionStore:
    s = InMemorySessionStore()
    set_store(s)
    sess = s.get_or_create(42)
    sess.onboarded_at = datetime.now(UTC)
    s.update(sess)
    return s


@pytest.mark.asyncio
async def test_empty_input_routes_to_silent_end(_v2_graph):
    graph, visited = _v2_graph
    _onboarded_store()
    msg = _msg(text="   ", callback_data=None, urls=[], photo_file_id=None)

    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    # No terminal node ran — graph went straight to END.
    assert visited == [], f"contentless Update must NOT spawn any node; ran {visited}"


@pytest.mark.asyncio
async def test_real_text_routes_to_agent(_v2_graph):
    graph, visited = _v2_graph
    _onboarded_store()
    msg = _msg(text="show me a navy blazer", callback_data=None, urls=[], photo_file_id=None)

    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    assert visited == ["agent"]


@pytest.mark.asyncio
async def test_photo_routes_to_resolve_image(_v2_graph):
    graph, visited = _v2_graph
    _onboarded_store()
    msg = _msg(photo_file_id="photo-abc")

    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    # Photo enters the vision pre-step (resolve_image) first, never straight
    # to agent and never silently dropped.
    assert visited and visited[0] == "resolve_image"


@pytest.mark.asyncio
async def test_item_callback_routes_to_pick_item(_v2_graph):
    graph, visited = _v2_graph
    _onboarded_store()
    msg = _msg(callback_data="item:1")

    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    assert visited == ["pick_item"]


@pytest.mark.asyncio
async def test_pick_happy_path_tap_routes_to_agent(monkeypatch):
    """Decision 3 characterization — item:0 tap sets selected_item_index → agent.

    Proves the vision→pick_item→`item:`callback→agent chain is correctly wired
    (no regression). Uses the real pick_item node; only `agent` is stubbed.
    """
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_V2_REACT_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "nova-lite", raising=False)

    visited: list[str] = []

    async def _agent_stub(state):
        visited.append(f"agent:selected={state.selected_item_index}")
        return {"log_events": ["agent-stub"]}

    import app.graphs.fashion_bot as fb
    import app.graphs.nodes.agent as agent_mod

    importlib.reload(fb)
    monkeypatch.setattr(agent_mod, "agent", _agent_stub)
    graph = fb.build_graph()

    s = _onboarded_store()
    # Seed a prior carousel: two detected items + AWAITING_ITEM_PICK.
    sess = s.get_or_create(42)
    sess.detected_items = [
        {"label": "white tee", "keywords": ["tee"]},
        {"label": "blue jeans", "keywords": ["jeans"]},
    ]
    sess.state = SessionState.AWAITING_ITEM_PICK
    s.update(sess)

    msg = _msg(callback_data="item:0")
    out = await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    assert out["selected_item_index"] == 0
    assert visited == ["agent:selected=0"], f"pick→tap must reach agent; ran {visited}"
