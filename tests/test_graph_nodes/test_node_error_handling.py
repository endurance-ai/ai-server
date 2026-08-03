"""SPEC-AGENT-001 / REQ-AGENT-007 — node exceptions never propagate.

Each node MUST log to log_events and return a (possibly empty) state delta on
error rather than letting the exception escape into the graph runtime. This
ensures the webhook handler always receives a graph result and HTTP 200 is
preserved (SPEC-MSG-001 REQ-MSG-002).
"""

from __future__ import annotations

import pytest

from app.channels.recommendation import set_port
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import (
    InMemorySessionStore,
    set_store,
    shutdown_store,
)
from app.infrastructure.memory.taste_profile import (
    InMemoryTasteProfileStore,
    set_taste_store,
    shutdown_taste_store,
)
from tests.conftest_graph import FakeAdapter, StubPort, make_msg


@pytest.fixture
async def store():
    s = InMemorySessionStore()
    set_store(s)
    yield s
    await shutdown_store()


@pytest.fixture
async def taste_store():
    s = InMemoryTasteProfileStore()
    set_taste_store(s)
    yield s
    await shutdown_taste_store()


@pytest.fixture
def stub_port():
    p = StubPort()
    set_port(p)
    yield p


@pytest.fixture
def adapter():
    a = FakeAdapter()
    token = set_adapter(a)
    yield a
    reset_adapter(token)


def _state(message=None, **kw) -> WorkingState:
    msg = message if message is not None else make_msg()
    return WorkingState(message=msg, chat_id=msg.chat_id, **kw)


# SPEC-AGENT-V2-CLEANUP-001 — `test_ingest_router_exception_does_not_propagate`
# was removed: `route_text` is no longer invoked by ingest (the V1 LLM router
# path was deleted; ingest returns early). The V2 ingest never-raises contract
# is covered by tests/test_agent_v2/test_ingest_route_text_gate.py.


# ── resolve_image ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_image_resolver_exception_yields_none(monkeypatch):
    async def _boom(_url):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.graphs.nodes.resolve_image.link_resolver.resolve", _boom)
    from app.graphs.nodes.resolve_image import resolve_image

    delta = await resolve_image(_state(make_msg(urls=["https://www.pinterest.com/pin/1/"])))
    assert delta["image_url"] is None
    assert any("resolve_image_error" in s for s in delta["log_events"])


# ── vision_node ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_node_extract_exception_yields_no_items(monkeypatch):
    async def _boom(_url):
        raise RuntimeError("vision down")

    monkeypatch.setattr("app.graphs.nodes.vision.vision_module.extract", _boom)
    from app.graphs.nodes.vision import vision_node

    state = _state(make_msg(), image_url="https://i.pinimg.com/x.jpg")
    delta = await vision_node(state)
    # No items written; only a log breadcrumb.
    assert "detected_items" not in delta or delta["detected_items"] == []
    assert any("vision_node_error" in s for s in delta["log_events"])


# SPEC-AGENT-V2-CLEANUP-001 — `test_search_node_port_exception_*` was removed:
# the V1 `search_node` was deleted with the V1 topology. Search now runs via
# the agent's `search_products` tool; its fail-open behavior is covered by
# tests/test_agent_v2/test_search_text_only.py.


# ── pick_item carousel send failure ────────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_item_send_failure_does_not_propagate(store, taste_store, adapter, monkeypatch):
    """If the adapter raises, pick_item logs and returns a delta — no propagation."""

    async def _boom(*a, **k):
        raise RuntimeError("telegram 5xx")

    monkeypatch.setattr(adapter, "send_text_with_buttons", _boom)
    monkeypatch.setattr(adapter, "send_text", _boom)

    state = _state(
        make_msg(),
        detected_items=[
            {"label": "white tee", "description": "round neck slim", "keywords": ["tee"]},
            {"label": "blue jeans", "description": "slim fit", "keywords": ["jeans"]},
        ],
    )
    from app.graphs.nodes.pick_item import pick_item

    delta = await pick_item(state)
    assert any("pick_item_error" in s for s in delta["log_events"])
