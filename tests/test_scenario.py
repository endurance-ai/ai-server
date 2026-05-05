"""Scenario state machine tests — focuses on the silent-dead-end fixes
(A1 bytes block / A2 vision fallback) and the RESULTS_SENT refine flow (C1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.channels import scenario
from app.channels.recommendation import (
    ChannelRecommendationRequest,
    ChannelRecommendationResult,
    set_port,
)
from app.channels.schemas import ChannelMessage
from app.channels.session import (
    InMemorySessionStore,
    SessionState,
    set_store,
    shutdown_store,
)

# ── Test doubles ────────────────────────────────────────────────────────────


class FakeAdapter:
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.cards: list = []
        self.actions: list[tuple[int, str]] = []
        self.buttons: list[tuple[int, str, list]] = []
        self.callback_answers: list[tuple[str, str | None]] = []
        self.bytes_payload: bytes | None = None
        self.send_card_returns = True

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))

    async def send_card(self, chat_id, card):
        self.cards.append((chat_id, card))
        return self.send_card_returns

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))

    async def send_text_with_buttons(self, chat_id, text, buttons):
        self.buttons.append((chat_id, text, list(buttons)))

    async def answer_callback_query(self, cbq_id, text=None):
        self.callback_answers.append((cbq_id, text))

    async def download_attachment(self, file_id):
        return self.bytes_payload or b"\x00\x01"

    async def parse_inbound(self, payload):  # unused
        raise NotImplementedError


@dataclass
class FakeCandidate:
    image_url: str | None = "https://img.example.com/a.jpg"
    product_url: str | None = "https://shop.example.com/p/1"
    brand: str = "BrandX"
    name: str = "Slim Tee"
    platform: str = "BrandX"
    subcategory: str = "tops"
    price: int = 49000


class StubPort:
    def __init__(self, candidates: list[FakeCandidate] | None = None, delay: float = 0.05) -> None:
        self.candidates = candidates if candidates is not None else [FakeCandidate()]
        self.calls: list[ChannelRecommendationRequest] = []
        self.delay = delay  # gives the typing heartbeat a chance to tick at least once

    async def recommend(self, req: ChannelRecommendationRequest) -> ChannelRecommendationResult:
        import asyncio as _asyncio

        self.calls.append(req)
        if self.delay > 0:
            await _asyncio.sleep(self.delay)
        return ChannelRecommendationResult(candidates=list(self.candidates))


def _msg(chat_id=42, text=None, photo_file_id=None, urls=None, callback_data=None) -> ChannelMessage:
    return ChannelMessage(
        chat_id=chat_id,
        text=text,
        photo_file_id=photo_file_id,
        urls=urls or [],
        callback_data=callback_data,
        callback_query_id=None,
        received_at=datetime.now(tz=UTC),
    )


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def store():
    s = InMemorySessionStore()
    set_store(s)
    yield s
    await shutdown_store()


@pytest.fixture
def stub_port():
    p = StubPort()
    set_port(p)
    yield p


@pytest.fixture(autouse=True)
def fast_heartbeat(monkeypatch):
    monkeypatch.setattr(scenario, "TYPING_HEARTBEAT_INTERVAL", 0.01)


# ── A1: bytes path is blocked with a friendly message ──────────────────────


@pytest.mark.asyncio
async def test_direct_photo_upload_is_blocked_before_vision(store, stub_port):
    adapter = FakeAdapter()
    msg = _msg(photo_file_id="f1")
    with patch.object(scenario.vision, "extract", new=AsyncMock()) as vision_mock:
        await scenario.handle(adapter, msg)
    vision_mock.assert_not_called()  # vision tokens must not be burned
    assert any("direct photo uploads" in t.lower() for _, t in adapter.texts)
    assert store.get_or_create(42).state == SessionState.IDLE
    assert stub_port.calls == []


# ── A2: vision fallback short-circuits instead of dragging into intent ─────


@pytest.mark.asyncio
async def test_vision_fallback_triggers_zero_result(store, stub_port):
    adapter = FakeAdapter()
    msg = _msg(urls=["https://www.pinterest.com/pin/123/"])
    fallback_vision = {"items": [{"label": "item", "description": "", "color": "", "keywords": []}]}
    with (
        patch.object(
            scenario, "_resolve_image_for_message", new=AsyncMock(return_value="https://i.pinimg.com/originals/x.jpg")
        ),
        patch.object(scenario.vision, "extract", new=AsyncMock(return_value=fallback_vision)),
    ):
        await scenario.handle(adapter, msg)
    assert any("couldn't find a match" in t for _, t in adapter.texts)
    sess = store.get_or_create(42)
    assert sess.state == SessionState.IDLE
    assert stub_port.calls == []  # never reached search


# ── A3: invalid pick re-sends the picker buttons ────────────────────────────


@pytest.mark.asyncio
async def test_invalid_pick_text_resends_picker(store, stub_port):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_ITEM_PICK
    sess.detected_items = [
        {"label": "white tee", "description": "round neck", "color": "white", "keywords": ["tee"]},
        {"label": "blue jeans", "description": "slim fit", "color": "blue", "keywords": ["jeans"]},
    ]
    store.update(sess)
    await scenario.handle(adapter, _msg(text="huh??"))
    assert adapter.buttons, "picker should have been re-sent with inline buttons"
    chat_id, body, btns = adapter.buttons[-1]
    assert chat_id == 42
    assert len(btns) == 2


# ── B1: search emits start message + heartbeat actions ─────────────────────


@pytest.mark.asyncio
async def test_intent_reply_runs_search_with_start_msg_and_heartbeat(store, stub_port):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.vision_keywords = ["tee", "white"]
    store.update(sess)

    await scenario.handle(adapter, _msg(text="something cheaper"))

    # SEARCH_START_TMPL was sent
    assert any("hunting for white tee" in t for _, t in adapter.texts)
    # CLOSER (not refine variant) sent
    assert any("see more like it" in t for _, t in adapter.texts)
    # heartbeat fired at least once (typing action)
    assert any(action == "typing" for _, action in adapter.actions)
    assert sess.state == SessionState.RESULTS_SENT
    assert stub_port.calls and stub_port.calls[0].intent == "something cheaper"


# ── B2: 0-card fallback sends a text list of links ─────────────────────────


@pytest.mark.asyncio
async def test_zero_card_render_falls_back_to_text_list(store, stub_port):
    adapter = FakeAdapter()
    adapter.send_card_returns = False  # every sendPhoto fails
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    store.update(sess)

    stub_port.candidates = [FakeCandidate(name="Tee A"), FakeCandidate(name="Tee B")]

    await scenario.handle(adapter, _msg(text="cheaper"))

    fallback_msgs = [t for _, t in adapter.texts if "here are the links" in t]
    assert fallback_msgs, "should fall back to text-card list when sendPhoto fails"
    assert "shop.example.com/p/1" in fallback_msgs[-1]
    assert sess.state == SessionState.RESULTS_SENT  # not IDLE — we delivered SOMETHING


# ── C1: RESULTS_SENT + text → refine search reusing prior context ──────────


@pytest.mark.asyncio
async def test_results_sent_text_triggers_refine_search(store, stub_port):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.vision_keywords = ["tee", "white"]
    store.update(sess)

    await scenario.handle(adapter, _msg(text="in black"))

    assert stub_port.calls, "refine should have invoked the recommendation port"
    req = stub_port.calls[0]
    assert req.image_url == "https://i.pinimg.com/originals/x.jpg"
    assert req.intent == "in black"
    assert "tee" in req.keywords
    assert any("refining the picks" in t for _, t in adapter.texts)
    assert any("Tweak it more" in t for _, t in adapter.texts)
    assert sess.state == SessionState.RESULTS_SENT


@pytest.mark.asyncio
async def test_refine_without_prior_context_falls_back_to_nudge(store, stub_port):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.state = SessionState.RESULTS_SENT
    # no image_url / vision_item — simulates TTL eviction or bytes-only flow
    store.update(sess)

    await scenario.handle(adapter, _msg(text="in black"))

    assert any("photo or a Pinterest link" in t for _, t in adapter.texts)
    assert stub_port.calls == []
