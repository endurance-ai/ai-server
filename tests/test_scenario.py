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
    id: str = "fake-1"
    image_url: str | None = "https://img.example.com/a.jpg"
    product_url: str | None = "https://shop.example.com/p/1"
    brand: str = "BrandX"
    name: str = "Slim Tee"
    platform: str = "BrandX"
    subcategory: str = "tops"
    price: int | None = 49000


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
    # Auto-attach a callback_query_id when a callback_data is provided —
    # mirrors real Telegram payloads (callback updates always carry an id).
    cbq_id = "cbq-test-1" if callback_data else None
    return ChannelMessage(
        chat_id=chat_id,
        text=text,
        photo_file_id=photo_file_id,
        urls=urls or [],
        callback_data=callback_data,
        callback_query_id=cbq_id,
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


@pytest.fixture(autouse=True)
def disable_router_llm(monkeypatch):
    """Default tests run without LiteLLM proxy — keep the router deterministic.
    Individual tests can re-enable + patch LLMProvider.chat for richer cases."""
    monkeypatch.setattr("app.channels.router.settings.ROUTER_LLM_ENABLED", False)


@pytest.fixture
async def taste_store():
    from app.channels.taste_profile import InMemoryTasteProfileStore, set_taste_store, shutdown_taste_store

    s = InMemoryTasteProfileStore()
    set_taste_store(s)
    yield s
    await shutdown_taste_store()


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
    # New copy from C1 redesign: pre-search summary instead of static "Reading
    # you" line. Either format is acceptable as long as a refine indicator
    # was sent before the cards.
    assert any(("Refining:" in t) or ("refining the picks" in t) for _, t in adapter.texts)
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


# ── Critique tap (♥ More / ✕ Less / 💰 Cheaper) ──────────────────────────────


@pytest.mark.asyncio
async def test_critique_tap_more_reinforces_taste_and_reruns(store, stub_port, taste_store):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.vision_keywords = ["tee", "white"]
    sess.last_results = [FakeCandidate(brand="ami", name="basic tee")]
    store.update(sess)

    await scenario.handle(adapter, _msg(callback_data="crit:more:0"))

    # taste profile updated for liked brand
    profile = taste_store.get_or_create("u:7")
    assert "ami" in profile.liked_brands

    # search re-ran with the anchor's brand boosted
    assert stub_port.calls
    req = stub_port.calls[-1]
    assert "ami" in req.boost_brands

    # callback toast was sent
    assert adapter.callback_answers
    _, toast = adapter.callback_answers[-1]
    assert "more" in (toast or "").lower()


@pytest.mark.asyncio
async def test_critique_tap_less_excludes_brand_and_excludes_shown(store, stub_port, taste_store):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.last_results = [FakeCandidate(id="p1", brand="Zara", name="cheap tee")]
    sess.shown_product_ids = ["p1"]
    store.update(sess)

    await scenario.handle(adapter, _msg(callback_data="crit:less:0"))

    profile = taste_store.get_or_create("u:7")
    assert "zara" in profile.disliked_brands

    assert stub_port.calls
    req = stub_port.calls[-1]
    assert "zara" in req.exclude_brands
    assert "p1" in req.exclude_product_ids


@pytest.mark.asyncio
async def test_critique_tap_cheap_sets_max_price(store, stub_port, taste_store):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.last_results = [FakeCandidate(brand="ami", name="tee", price=100000)]
    store.update(sess)

    await scenario.handle(adapter, _msg(callback_data="crit:cheap:0"))

    assert stub_port.calls
    req = stub_port.calls[-1]
    assert req.max_price == 70000  # default ratio 0.7


@pytest.mark.asyncio
async def test_critique_tap_invalid_idx_sends_toast_and_skips_search(store, stub_port, taste_store):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.last_results = []  # stale / empty
    store.update(sess)

    msg = ChannelMessage(
        chat_id=42,
        callback_data="crit:more:9",
        callback_query_id="cbq-stale",
        received_at=datetime.now(tz=UTC),
    )
    await scenario.handle(adapter, msg)

    assert stub_port.calls == []
    assert adapter.callback_answers and "out of date" in adapter.callback_answers[-1][1].lower()


# ── Pre-search summary surfaces tweak ──────────────────────────────────────


@pytest.mark.asyncio
async def test_critique_tap_emits_presearch_summary(store, stub_port, taste_store):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.last_results = [FakeCandidate(brand="Zara", name="tee", price=50000)]
    store.update(sess)

    await scenario.handle(adapter, _msg(callback_data="crit:less:0"))

    presearch = [t for _, t in adapter.texts if "Refining:" in t]
    assert presearch
    assert "Zara" in presearch[0] or "zara" in presearch[0].lower()


# ── Card carries critique buttons ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_sent_cards_carry_critique_buttons(store, stub_port):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    store.update(sess)

    await scenario.handle(adapter, _msg(text="for casual"))

    assert adapter.cards, "expected at least one card sent"
    _, card = adapter.cards[0]
    assert card.critique_buttons, "card should carry critique buttons (more/less/cheap)"
    labels = [lbl for lbl, _ in card.critique_buttons]
    assert any("More" in lb for lb in labels)
    assert any("Less" in lb for lb in labels)
    assert any("Cheaper" in lb for lb in labels)


# ── Session caches last_results + accumulates shown_product_ids ────────────


@pytest.mark.asyncio
async def test_session_caches_results_and_accumulates_shown_ids(store, stub_port):
    adapter = FakeAdapter()
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    store.update(sess)

    stub_port.candidates = [FakeCandidate(id="p1"), FakeCandidate(id="p2")]
    await scenario.handle(adapter, _msg(text="something cheaper"))

    sess_after = store.get_or_create(42)
    assert sess_after.state == SessionState.RESULTS_SENT
    assert len(sess_after.last_results) == 2
    assert "p1" in sess_after.shown_product_ids
    assert "p2" in sess_after.shown_product_ids
