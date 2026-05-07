"""SPEC-VISION-UNIFY-001 — end-to-end test through GRAPH.ainvoke().

REQ-VISION-SEARCH-003 acceptance: searchQueryKo from a rich VisionResult
reaches the ChannelRecommendationRequest captured by StubPort.
"""

from __future__ import annotations

import pytest

from app.channels.recommendation import set_port
from app.channels.session import (
    InMemorySessionStore,
    set_store,
    shutdown_store,
)
from app.channels.taste_profile import (
    InMemoryTasteProfileStore,
    set_taste_store,
    shutdown_taste_store,
)
from app.channels.vision import (
    VisionItem,
    VisionResult,
    VisionStyle,
    VisionStyleNode,
)
from app.graphs.fashion_bot import GRAPH
from app.graphs.nodes import respond as respond_module
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import InputState
from tests.conftest_graph import FakeAdapter, StubLLM, StubPort, make_msg


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


@pytest.fixture(autouse=True)
def disable_router_llm(monkeypatch):
    monkeypatch.setattr("app.channels.router.settings.ROUTER_LLM_ENABLED", False)


@pytest.fixture(autouse=True)
def stub_respond_llm(monkeypatch):
    fake = StubLLM(content="Okay.")
    monkeypatch.setattr(respond_module, "_llm", fake)
    return fake


@pytest.mark.asyncio
async def test_rich_search_query_ko_reaches_port(store, taste_store, stub_port, adapter, monkeypatch):
    """REQ-VISION-SEARCH-003 — end-to-end: rich VisionResult → port carries
    item_search_query_ko + outfit context.

    Single-item path: vision returns one rich item, single-item flow goes
    straight to critique_apply → search_node.
    """
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    rich = VisionResult(
        isApparel=True,
        styleNode=VisionStyleNode(primary="C", secondary="D"),
        style=VisionStyle(detectedGender="male"),
        items=[
            VisionItem(
                id="top_1",
                category="Top",
                subcategory="t-shirt",
                name="Boxy Tee",
                fabric="jersey",
                color="black",
                colorFamily="BLACK",
                fit="boxy",
                searchQuery="boxy black jersey t-shirt men",
                searchQueryKo="박시 블랙 저지 티셔츠 남성",
            ),
        ],
    )

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_rich(_url):
        return rich

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_rich)

    msg = make_msg(urls=["https://www.pinterest.com/pin/123/"])
    await GRAPH.ainvoke(InputState(message=msg, chat_id=42))

    # Single-item rich → critique_apply has no delta (no callback / no text);
    # routing._route_after_critique sees critique_delta=None → respond.
    # So search_node may not run; we instead verify session persistence.
    sess = store.get_or_create(42)
    assert sess.vision_result is not None
    assert sess.vision_result.items[0].searchQueryKo == "박시 블랙 저지 티셔츠 남성"
    assert sess.vision_outfit_style_node_primary == "C"
    assert sess.vision_outfit_gender == "male"
    assert sess.vision_selected_item_index == 0
    # Legacy fields derived from the rich item.
    assert sess.vision_item == "Boxy Tee"
    assert "boxy" in sess.vision_keywords


@pytest.mark.asyncio
async def test_rich_search_query_ko_reaches_port_via_intent_text(store, taste_store, stub_port, adapter, monkeypatch):
    """Two-turn flow: turn 1 image upload → vision result persisted.
    Turn 2 user types intent → search_node fires → StubPort receives request
    with item_search_query_ko populated from the persisted VisionResult.
    """
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    rich = VisionResult(
        isApparel=True,
        styleNode=VisionStyleNode(primary="C", secondary="D"),
        style=VisionStyle(detectedGender="male"),
        items=[
            VisionItem(
                id="top_1",
                category="Top",
                subcategory="t-shirt",
                name="Boxy Tee",
                colorFamily="BLACK",
                fit="boxy",
                fabric="jersey",
                searchQuery="boxy black jersey t-shirt men",
                searchQueryKo="박시 블랙 저지 티셔츠 남성",
            ),
        ],
    )

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_rich(_url):
        return rich

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_rich)

    # Turn 1: upload image, vision result populates session.
    await GRAPH.ainvoke(InputState(message=make_msg(urls=["https://www.pinterest.com/pin/123/"]), chat_id=42))

    # Turn 2: user types intent → routing sees AWAITING_INTENT → critique_apply.
    sess = store.get_or_create(42)
    from app.channels.session import SessionState

    sess.state = SessionState.AWAITING_INTENT
    store.update(sess)

    await GRAPH.ainvoke(InputState(message=make_msg(text="something cheaper"), chat_id=42))

    # Verify search_node fired and the rich fields reached the port.
    assert stub_port.calls, "search must have run on turn 2"
    last_req = stub_port.calls[-1]
    assert last_req.item_search_query_ko == "박시 블랙 저지 티셔츠 남성"
    assert last_req.item_subcategory == "t-shirt"
    assert last_req.item_fit == "boxy"
    assert last_req.item_color_family == "BLACK"
    assert last_req.outfit_style_node_primary == "C"
    assert last_req.outfit_gender == "male"
