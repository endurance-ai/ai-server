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

from app.channels.schemas import ChannelMessage
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import InputState, WorkingState
from app.infrastructure.memory.session import InMemorySessionStore, set_store
from app.infrastructure.memory.taste_profile import InMemoryTasteProfileStore, set_taste_store
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
# LOG-T0 — ingest → user_text / user_photo / user_callback (intake, turn_no=0)
# The inbound user event: catalog #1–#3 were documented but never emitted,
# leaving turns with no visible trigger. `ingest` now records it at the
# universal entry node.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_t0_ingest_emits_user_text():
    from app.graphs.nodes.ingest import ingest

    s = _state(_msg(text="블랙 오버사이즈 블레이저"))
    with patch("app.graphs.nodes.ingest.emit") as m:
        await ingest(s)
    ut = [c for c in m.call_args_list if c.kwargs.get("event_type") == "user_text"]
    assert len(ut) == 1
    assert ut[0].kwargs["turn_no"] == 0
    assert ut[0].kwargs["payload"]["text"] == "블랙 오버사이즈 블레이저"
    assert ut[0].kwargs["payload"]["lang_detected"] == "ko"


@pytest.mark.asyncio
async def test_log_t0_ingest_emits_user_callback_not_text():
    from app.graphs.nodes.ingest import ingest

    s = _state(_msg(callback_data="clarify:fit:oversized"))
    with patch("app.graphs.nodes.ingest.emit") as m:
        await ingest(s)
    events = [c.kwargs.get("event_type") for c in m.call_args_list]
    assert "user_callback" in events
    assert "user_text" not in events  # callback shape is not text


@pytest.mark.asyncio
async def test_log_t0_ingest_emits_user_photo_for_url():
    from app.graphs.nodes.ingest import ingest

    s = _state(_msg(urls=["https://example.com/a.jpg"]))
    with patch("app.graphs.nodes.ingest.emit") as m:
        await ingest(s)
    up = [c for c in m.call_args_list if c.kwargs.get("event_type") == "user_photo"]
    assert len(up) == 1
    assert up[0].kwargs["payload"]["image_url"] == "https://example.com/a.jpg"


@pytest.mark.asyncio
async def test_log_t0_ingest_contentless_emits_no_user_event():
    from app.graphs.nodes.ingest import ingest

    s = _state(_msg())  # no text / photo / url / callback
    with patch("app.graphs.nodes.ingest.emit") as m:
        await ingest(s)
    events = [c.kwargs.get("event_type") for c in m.call_args_list]
    assert "user_text" not in events
    assert "user_photo" not in events
    assert "user_callback" not in events


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


# SPEC-AGENT-V2-CLEANUP-001 — the LOG-T16 search_node emit tests were removed
# (the V1 `search_node` was deleted). Search now runs via the agent's
# `search_products` tool which emits `tool_call` events from the ReAct loop;
# that coverage lives in tests/test_agent_v2/ + tests/test_conversation_log
# tool_call coverage.


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


# SPEC-AGENT-V2-CLEANUP-001 — the LOG-T19 (respond), LOG-T20 (taste_update)
# and LOG-T21 (critique_apply) per-node emit tests were removed: those V1
# nodes were deleted with the V1 topology. The agent path emits bot_text /
# taste_update / tool_call via the ReAct loop (tests/test_agent_v2/).
