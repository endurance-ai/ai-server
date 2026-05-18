"""SPEC-AGENT-V2-REACT — first-touch service intro (onboarding-cards OFF).

Verifies:
- New user (onboarded_at None) + ONBOARDING_CARDS_ENABLED=false → routes to
  `intro`, ONE intro message sent (KO content), onboarded_at set, agent NOT
  invoked, graph terminates.
- Same user 2nd message (onboarded_at now set) → does NOT hit intro; routes
  to agent normally.
- Flag ON (ONBOARDING_CARDS_ENABLED=true) → intro node NOT used (old
  onboarding path unchanged — regression guard).
- KO vs EN intro selection by first-message language.
- First message is a Pinterest URL → still intro-only first (no resolve_image).
- `/start` as first message → still intro.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.state import InputState
from app.infrastructure.memory.session import InMemorySessionStore, set_store


class _FakeAdapter:
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))

    async def send_text_with_keyboard(self, chat_id, text, keyboard):
        self.texts.append((chat_id, text))
        return 1

    async def send_chat_action(self, chat_id, action):  # pragma: no cover - defensive
        return None


@pytest.fixture
def _v2_graph(monkeypatch):
    """Build a V2 graph with onboarding cards OFF and agent/intro adapter stubbed."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "nova-lite", raising=False)
    monkeypatch.setattr(cfg.settings, "ONBOARDING_CARDS_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg.settings, "DEMO_MODE", False, raising=False)

    visited: list[str] = []

    async def _agent_stub(state):
        visited.append("agent")
        return {"log_events": ["agent-stub"]}

    async def _resolve_stub(state):
        visited.append("resolve_image")
        return {"log_events": ["resolve-stub"], "image_url": None}

    import app.graphs.fashion_bot as fb
    import app.graphs.nodes.agent as agent_mod
    import app.graphs.nodes.intro as intro_mod

    importlib.reload(fb)
    monkeypatch.setattr(agent_mod, "agent", _agent_stub)
    monkeypatch.setattr(fb, "resolve_image", _resolve_stub)

    adapter = _FakeAdapter()
    monkeypatch.setattr(intro_mod, "get_adapter", lambda: adapter)

    graph = fb.build_graph()
    return graph, visited, adapter


def _msg(**kw) -> ChannelMessage:
    return ChannelMessage(chat_id=42, from_user_id=99, received_at=datetime.now(UTC), **kw)


def _fresh_store() -> InMemorySessionStore:
    s = InMemorySessionStore()
    set_store(s)
    return s


@pytest.mark.asyncio
async def test_new_user_first_message_routes_to_intro_ko(_v2_graph):
    graph, visited, adapter = _v2_graph
    store = _fresh_store()

    msg = _msg(text="안녕 미니멀한 코트 찾아줘")
    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    # Agent NOT invoked — intro only.
    assert visited == [], f"first message must NOT reach agent; ran {visited}"
    # Exactly one intro message sent, KO content.
    assert len(adapter.texts) == 1
    _, sent = adapter.texts[0]
    assert "kiko" in sent
    assert "핀터레스트" in sent
    assert "인스타그램" in sent
    assert "찾아드려요" in sent or "기다리고 있을게요" in sent
    # onboarded_at persisted so the 2nd webhook won't loop intro.
    assert store.get_or_create(42).onboarded_at is not None


@pytest.mark.asyncio
async def test_new_user_first_message_en(_v2_graph):
    graph, visited, adapter = _v2_graph
    _fresh_store()

    msg = _msg(text="find me a minimal black coat")
    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    assert visited == []
    assert len(adapter.texts) == 1
    _, sent = adapter.texts[0]
    assert "kiko" in sent
    assert "Pinterest" in sent
    assert "Instagram" in sent
    assert "first request" in sent


@pytest.mark.asyncio
async def test_second_message_skips_intro_routes_to_agent(_v2_graph):
    graph, visited, adapter = _v2_graph
    _fresh_store()

    first = _msg(text="안녕")
    await graph.ainvoke(InputState(message=first, chat_id=42, from_user_id=99))
    assert visited == [] and len(adapter.texts) == 1

    second = _msg(text="블랙 코트 찾아줘")
    await graph.ainvoke(InputState(message=second, chat_id=42, from_user_id=99))

    # No second intro message; routed to agent.
    assert visited == ["agent"], f"2nd message must reach agent; ran {visited}"
    assert len(adapter.texts) == 1, "intro must be sent only once"


@pytest.mark.asyncio
async def test_first_message_pinterest_url_still_intro_only(_v2_graph):
    graph, visited, adapter = _v2_graph
    store = _fresh_store()

    msg = _msg(text="https://pin.it/abc123", urls=["https://pin.it/abc123"])
    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    # Not resolve_image — intro wins on the very first turn.
    assert visited == [], f"first link must NOT reach resolve_image; ran {visited}"
    assert len(adapter.texts) == 1
    assert store.get_or_create(42).onboarded_at is not None


@pytest.mark.asyncio
async def test_first_message_slash_start_still_intro(_v2_graph):
    graph, visited, adapter = _v2_graph
    _fresh_store()

    msg = _msg(text="/start")
    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    assert visited == []
    assert len(adapter.texts) == 1


@pytest.mark.asyncio
async def test_contentless_update_does_not_trigger_intro(_v2_graph):
    graph, visited, adapter = _v2_graph
    store = _fresh_store()

    msg = _msg(text="   ", callback_data=None, urls=[], photo_file_id=None)
    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    # Contentless → silent END, NOT intro (no spam, onboarded_at untouched).
    assert visited == []
    assert adapter.texts == []
    assert store.get_or_create(42).onboarded_at is None


@pytest.mark.asyncio
async def test_flag_on_does_not_use_intro_node(monkeypatch):
    """Regression guard: ONBOARDING_CARDS_ENABLED=true → old onboarding path,
    new user routes to onboard_intro (cards), never the new `intro` node."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "nova-lite", raising=False)
    monkeypatch.setattr(cfg.settings, "ONBOARDING_CARDS_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg.settings, "DEMO_MODE", False, raising=False)

    visited: list[str] = []

    async def _onboard_intro_stub(state):
        visited.append("onboard_intro")
        return {"log_events": ["onboard-intro-stub"], "onboard_stage": "mood"}

    async def _intro_stub(state):  # pragma: no cover - must never run
        visited.append("intro")
        return {"log_events": ["intro-stub"]}

    import app.graphs.fashion_bot as fb
    import app.graphs.nodes.intro as intro_mod

    importlib.reload(fb)
    monkeypatch.setattr(fb, "onboard_intro", _onboard_intro_stub)
    monkeypatch.setattr(intro_mod, "intro", _intro_stub)

    _fresh_store()
    graph = fb.build_graph()

    msg = _msg(text="안녕")
    await graph.ainvoke(InputState(message=msg, chat_id=42, from_user_id=99))

    assert "onboard_intro" in visited
    assert "intro" not in visited, f"flag ON must use card flow, not intro; ran {visited}"
