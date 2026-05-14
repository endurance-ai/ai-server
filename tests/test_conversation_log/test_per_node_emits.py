"""SPEC-CONVERSATION-LOG-001 Phase 3 — per-node emit verification (no Docker).

각 노드가 success terminus 에서 올바른 `event_type` + payload 키들을 emit 하는지
검증한다. testcontainers 없이 emit() 자체를 patch 한다.

LOG-T11 ~ LOG-T22 매핑은 plan §4.15 / tasks.md §Phase 3 표 참조.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.channels.recommendation import ChannelRecommendationResult
from app.channels.schemas import ChannelMessage
from app.channels.session import InMemorySessionStore, SessionState, set_store
from app.channels.taste_profile import InMemoryTasteProfileStore, set_taste_store
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import InputState, WorkingState
from tests.conftest_graph import FakeAdapter, FakeCandidate


# Override the package-level autouse truncate fixture so these tests do NOT
# require Docker. They patch the emit() helper directly.
@pytest.fixture(autouse=True)
def _truncate_log_table():
    yield


def _msg(**kw) -> ChannelMessage:
    return ChannelMessage(
        chat_id=kw.get("chat_id", 42),
        from_user_id=kw.get("from_user_id", 7),
        text=kw.get("text"),
        photo_file_id=kw.get("photo_file_id"),
        urls=kw.get("urls") or [],
        callback_data=kw.get("callback_data"),
        callback_query_id="cbq-test" if kw.get("callback_data") else None,
        received_at=datetime.now(tz=UTC),
    )


def _state(message: ChannelMessage, **kw) -> WorkingState:
    base = InputState(
        message=message,
        chat_id=message.chat_id,
        from_user_id=message.from_user_id,
        thread_id=kw.get("thread_id", uuid4()),
        turn_no=kw.get("turn_no", 0),
    )
    extra = {k: v for k, v in kw.items() if k not in {"thread_id", "turn_no"}}
    return WorkingState(**base.model_dump(), **extra)


@pytest.fixture(autouse=True)
def _isolate():
    set_store(InMemorySessionStore())
    set_taste_store(InMemoryTasteProfileStore())
    yield
    set_store(InMemorySessionStore())
    set_taste_store(InMemoryTasteProfileStore())


@pytest.fixture
def adapter_ctx():
    adapter = FakeAdapter()
    token = set_adapter(adapter)
    try:
        yield adapter
    finally:
        reset_adapter(token)


# ─────────────────────────────────────────────────────────────────────────
# LOG-T11 — ingest → intent_routed (turn_no=1)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t11_ingest_emits_intent_routed():
    from app.graphs.nodes.ingest import ingest

    s = _state(_msg(photo_file_id="ph-1"))  # bypass router path
    with patch("app.graphs.nodes.ingest.emit") as m:
        result = await ingest(s)
    assert any(call.kwargs.get("event_type") == "intent_routed" for call in m.call_args_list)
    assert result.get("turn_no") == 1


# ─────────────────────────────────────────────────────────────────────────
# LOG-T12 — resolve_image → link_resolved (turn_no=2)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t12_resolve_image_no_urls_still_emits_nothing():
    """no URL case returns early WITHOUT emit (link_resolved not raised)."""
    from app.graphs.nodes.resolve_image import resolve_image

    s = _state(_msg())
    with patch("app.graphs.nodes.resolve_image.emit") as m:
        result = await resolve_image(s)
    # No urls/photo → no emit (per-plan: only emit when we tried to resolve).
    assert m.call_count == 0
    assert result.get("turn_no") == 2


@pytest.mark.asyncio
async def test_log_t12_resolve_image_with_url_emits(monkeypatch):
    from app.graphs.nodes import resolve_image as ri

    async def _fake_resolve(_url):
        return ["https://i.pinimg.com/originals/foo.jpg"]

    monkeypatch.setattr(ri.link_resolver, "resolve", _fake_resolve)
    s = _state(_msg(urls=["https://pin.it/abc"]))
    with patch("app.graphs.nodes.resolve_image.emit") as m:
        await ri.resolve_image(s)
    events = [call.kwargs["event_type"] for call in m.call_args_list]
    # Pinterest host → pinterest_ingest branch.
    assert "pinterest_ingest" in events


# ─────────────────────────────────────────────────────────────────────────
# LOG-T13 — vision → vision_done (turn_no=3)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t13_vision_no_image_emits_with_error():
    from app.graphs.nodes.vision import vision_node

    s = _state(_msg())  # no image_url
    with patch("app.graphs.nodes.vision.emit") as m:
        await vision_node(s)
    calls = [c for c in m.call_args_list if c.kwargs.get("event_type") == "vision_done"]
    assert len(calls) == 1
    assert calls[0].kwargs["turn_no"] == 3
    assert calls[0].kwargs["payload"]["error"] == "no_image_url"


# ─────────────────────────────────────────────────────────────────────────
# LOG-T14 — pick_item → pick_item_done (turn_no=4)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t14_pick_item_carousel_emits(adapter_ctx):
    from app.graphs.nodes.pick_item import pick_item

    items = [{"label": "shirt", "subcategory": "tops"}, {"label": "pants", "subcategory": "bottoms"}]
    s = _state(_msg(), detected_items=items)
    with patch("app.graphs.nodes.pick_item.emit") as m:
        result = await pick_item(s)
    calls = [c for c in m.call_args_list if c.kwargs.get("event_type") == "pick_item_done"]
    assert len(calls) == 1
    assert calls[0].kwargs["payload"]["picked_index"] == -1
    assert calls[0].kwargs["payload"]["auto_picked"] is False
    assert result.get("turn_no") == 4


# ─────────────────────────────────────────────────────────────────────────
# LOG-T16 — search → search_done parallel array invariant (turn_no=6)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t16_search_parallel_array_invariant(monkeypatch):
    from app.channels.recommendation import set_port
    from app.channels.session import get_store
    from app.graphs.nodes.search import search_node

    sess = get_store().get_or_create(42)
    sess.image_url = "https://example.com/img.jpg"
    sess.vision_item = "tee"
    sess.vision_keywords = ["white", "casual"]
    get_store().update(sess)

    candidates = [FakeCandidate(id="p-1"), FakeCandidate(id="p-2"), FakeCandidate(id="p-3")]

    class _Port:
        async def recommend(self, _req):
            return ChannelRecommendationResult(candidates=candidates, counts={"dense": 30, "sparse": 20})

    set_port(_Port())
    monkeypatch.setattr("app.graphs.nodes.search.settings.DEMO_MODE", False)

    s = _state(_msg())
    with patch("app.graphs.nodes.search.emit") as m:
        await search_node(s)

    sd = [c for c in m.call_args_list if c.kwargs.get("event_type") == "search_done"]
    assert len(sd) == 1
    payload = sd[0].kwargs["payload"]
    assert len(payload["top_k_product_ids"]) == len(payload["rrf_scores"])
    assert payload["top_k_product_ids"] == ["p-1", "p-2", "p-3"]
    assert sd[0].kwargs["turn_no"] == 6


@pytest.mark.asyncio
async def test_log_t16_search_empty_emits_empty_arrays(monkeypatch):
    from app.channels.recommendation import set_port
    from app.channels.session import get_store
    from app.graphs.nodes.search import search_node

    sess = get_store().get_or_create(42)
    sess.image_url = "https://example.com/img.jpg"
    get_store().update(sess)

    class _Port:
        async def recommend(self, _req):
            return ChannelRecommendationResult(candidates=[], counts={})

    set_port(_Port())
    monkeypatch.setattr("app.graphs.nodes.search.settings.DEMO_MODE", False)

    s = _state(_msg())
    with patch("app.graphs.nodes.search.emit") as m:
        await search_node(s)

    sd = [c for c in m.call_args_list if c.kwargs.get("event_type") == "search_done"]
    assert len(sd) == 1
    payload = sd[0].kwargs["payload"]
    assert payload["top_k_product_ids"] == []
    assert payload["rrf_scores"] == []


# ─────────────────────────────────────────────────────────────────────────
# LOG-T17 — send_results → diversify_done + card_sent per card
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t17_send_results_emits_diversify_and_card_sent(adapter_ctx, monkeypatch):
    from app.graphs.nodes.send_results import send_results

    monkeypatch.setattr("app.core.config.settings.DEMO_MODE", False)

    cands = [FakeCandidate(id=f"p-{i}") for i in range(3)]
    s = _state(_msg(), candidates=cands)

    # Adapter.send_card returns sequential message_ids (1001, 1002, 1003).
    adapter_ctx.send_card_returns = True

    with patch("app.graphs.nodes.send_results.emit") as m:
        await send_results(s)

    events = [c.kwargs.get("event_type") for c in m.call_args_list]
    # exactly one diversify_done + 3 card_sent rows.
    assert events.count("diversify_done") == 1
    assert events.count("card_sent") == 3

    # card_sent rows carry distinct source_message_id values (LOG-T09 dep).
    card_sent_calls = [c for c in m.call_args_list if c.kwargs.get("event_type") == "card_sent"]
    msg_ids = [c.kwargs["payload"]["source_message_id"] for c in card_sent_calls]
    assert len(set(msg_ids)) == 3


# ─────────────────────────────────────────────────────────────────────────
# LOG-T17 — implicit_feedback coexistence: card_sent (3) + card_impression (3)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t17_implicit_fb_coexistence(adapter_ctx, monkeypatch):
    """REQ-LOG-IMPLICIT-FB-COEXIST-001 — `card_sent` events DO NOT affect the
    `ai.card_impression` write. Three cards → three card_sent emits AND three
    log_impressions calls.
    """
    from app.graphs.nodes.send_results import send_results

    monkeypatch.setattr("app.core.config.settings.DEMO_MODE", False)

    impression_calls: list = []

    async def _fake_log_impressions(chat_id, from_user_id, products):
        impression_calls.append((chat_id, len(products)))
        return len(products)

    monkeypatch.setattr(
        "app.channels.implicit_feedback.log_impressions",
        _fake_log_impressions,
    )

    cands = [FakeCandidate(id=f"p-{i}") for i in range(3)]
    s = _state(_msg(), candidates=cands)
    adapter_ctx.send_card_returns = True

    with patch("app.graphs.nodes.send_results.emit") as m:
        await send_results(s)

    card_sent_n = sum(1 for c in m.call_args_list if c.kwargs.get("event_type") == "card_sent")
    assert card_sent_n == 3
    # `card_impression` write (via log_impressions) ran exactly once with 3 products.
    assert impression_calls == [(42, 3)]


# ─────────────────────────────────────────────────────────────────────────
# LOG-T19 — respond → bot_text per chunk (turn_no=10)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t19_respond_per_chunk_emit(adapter_ctx, monkeypatch):
    from app.graphs.nodes import respond as respond_mod

    class _StubLLM:
        async def ainvoke(self, _msgs):
            class _R:
                content = "First sentence. Second sentence!"

            return _R()

    monkeypatch.setattr(respond_mod, "_llm", _StubLLM())
    monkeypatch.setattr(respond_mod.settings, "RESPONSE_SPLIT_ENABLED", True)
    monkeypatch.setattr(respond_mod.settings, "RESPONSE_SPLIT_MIN_CHARS", 1)
    monkeypatch.setattr(respond_mod.settings, "RESPONSE_SPLIT_DELAY_MS", 0)

    s = _state(_msg(text="hello"))
    with patch.object(respond_mod, "emit") as m:
        await respond_mod.respond(s)

    bot_text_calls = [c for c in m.call_args_list if c.kwargs.get("event_type") == "bot_text"]
    assert len(bot_text_calls) >= 1
    # Each call carries chunk_index / total_chunks / flow.
    for c in bot_text_calls:
        payload = c.kwargs["payload"]
        assert "chunk_index" in payload
        assert "total_chunks" in payload
        assert "flow" in payload
        assert c.kwargs["turn_no"] == 10


# ─────────────────────────────────────────────────────────────────────────
# LOG-T20 — taste_update → taste_update(source="free_text")
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t20_taste_update_free_text_emit():
    from app.channels.router import (
        RoutedDecision,
        RoutedIntent,
    )
    from app.channels.router import (
        TasteUpdate as RouterTasteUpdate,
    )
    from app.graphs.nodes.taste_update import taste_update

    decision = RoutedDecision(
        intent=RoutedIntent.TASTE_UPDATE,
        taste_update=RouterTasteUpdate(liked_brands=["acme"], liked_keywords=["minimal"]),
    )
    s = _state(_msg(text="i like minimalist"), decision=decision)

    with patch("app.graphs.nodes.taste_update.emit") as m:
        await taste_update(s)

    calls = [c for c in m.call_args_list if c.kwargs.get("event_type") == "taste_update"]
    assert len(calls) == 1
    assert calls[0].kwargs["payload"]["source"] == "free_text"


# ─────────────────────────────────────────────────────────────────────────
# LOG-T21 — critique_apply dual emit (card_clicked + taste_update)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t21_critique_apply_click_dual_emit(adapter_ctx):
    from app.channels.session import get_store
    from app.graphs.nodes.critique_apply import critique_apply

    sess = get_store().get_or_create(42)
    cand = FakeCandidate(id="p-99", brand="acme")
    sess.last_results = [cand]
    sess.state = SessionState.RESULTS_SENT
    get_store().update(sess)

    s = _state(_msg(callback_data="crit:click:p-99"))
    with patch("app.graphs.nodes.critique_apply.emit") as m:
        await critique_apply(s)

    events = [c.kwargs.get("event_type") for c in m.call_args_list]
    assert "card_clicked" in events
    # Dual: also a taste_update with source="click".
    taste_calls = [c for c in m.call_args_list if c.kwargs.get("event_type") == "taste_update"]
    assert any(c.kwargs["payload"].get("source") == "click" for c in taste_calls)


@pytest.mark.asyncio
async def test_log_t21_critique_apply_more_emits_taste_update_critique(adapter_ctx):
    from app.channels.session import get_store
    from app.graphs.nodes.critique_apply import critique_apply

    sess = get_store().get_or_create(42)
    cand = FakeCandidate(id="p-77", brand="bcorp")
    sess.last_results = [cand]
    sess.state = SessionState.RESULTS_SENT
    get_store().update(sess)

    s = _state(_msg(callback_data="crit:more:0"))
    with patch("app.graphs.nodes.critique_apply.emit") as m:
        await critique_apply(s)

    taste_calls = [c for c in m.call_args_list if c.kwargs.get("event_type") == "taste_update"]
    # Exactly one taste_update with source="critique" (no card_clicked for more/less/cheap).
    assert len(taste_calls) == 1
    assert taste_calls[0].kwargs["payload"]["source"] == "critique"
    assert all(c.kwargs.get("event_type") != "card_clicked" for c in m.call_args_list)
