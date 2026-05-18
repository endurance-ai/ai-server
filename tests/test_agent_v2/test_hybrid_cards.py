"""Hybrid result-card delivery UX.

Replaces the per-card streaming carousel with ONE album (sendMediaGroup) +
ONE summary text message (numbered HTML links + inline keyboard). Covers:

- `send_media_group` adapter method (success + atomic-fail bounds).
- `respond` hybrid output: album + summary, KO & EN parity, idempotent
  re-entry, broken-image pre-filter, atomic-fail → per-card fallback.
- ingest handling of `card:like:` / `cards:more` / `cards:refine`
  (not treated as a fresh query, taste/clicked emitted, more → next batch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState, set_store


class _FakeStore:
    def __init__(self, sess: Session) -> None:
        self._sess = sess

    def get_or_create(self, chat_id: int) -> Session:
        return self._sess

    def update(self, sess: Session) -> None:
        self._sess = sess


def _candidate(i: int, *, image: str | None = None):
    from app.models.response import Candidate

    return Candidate(
        id=f"p{i}",
        brand=f"Brand{i}",
        name=f"Slim Tee {i}",
        price=39000 + i,
        image_url=f"https://img.example.com/{i}.jpg" if image is None else image,
        product_url=f"https://shop.example.com/{i}",
        platform="kiko",
        subcategory="t-shirt",
        score=0.9 - i * 0.01,
    )


def _session_with_results(n: int, *, lang: str = "en") -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE, from_user_id=99)
    s.last_results = [_candidate(i) for i in range(n)]
    s.lang = lang
    return s


@pytest.fixture(autouse=True)
def _reset_pager():
    from app.agents.tools.respond import reset_card_batch_cursor_for_tests

    reset_card_batch_cursor_for_tests()
    yield
    reset_card_batch_cursor_for_tests()


def _adapter(group_ok: bool = True) -> MagicMock:
    a = MagicMock()
    a.send_text = AsyncMock()
    a.send_text_with_keyboard = AsyncMock(return_value=1001)
    a.send_card = AsyncMock(return_value=2002)
    a.send_media_group = AsyncMock(return_value=group_ok)
    return a


# ── send_media_group adapter ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_media_group_success():
    from app.channels.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(bot_token="x:y")
    posted: list = []

    async def _fake_post(method, payload, **kw):
        posted.append((method, payload))
        return {"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]}

    adapter._post = _fake_post  # type: ignore[assignment]

    media = [
        {"image_url": "https://img/1.jpg", "caption": None},
        {"image_url": "https://img/2.jpg", "caption": None},
    ]
    ok = await adapter.send_media_group(7, media)
    assert ok is True
    assert posted[0][0] == "sendMediaGroup"
    assert len(posted[0][1]["media"]) == 2
    assert posted[0][1]["media"][0]["type"] == "photo"


@pytest.mark.asyncio
async def test_send_media_group_atomic_fail_returns_false():
    from app.channels.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(bot_token="x:y")

    async def _fake_post(method, payload, **kw):
        # Telegram rejects the whole group when one URL is bad → _post None.
        return None

    adapter._post = _fake_post  # type: ignore[assignment]

    ok = await adapter.send_media_group(7, [{"image_url": "https://bad"}, {"image_url": "https://x"}])
    assert ok is False


@pytest.mark.asyncio
async def test_send_media_group_bounds_guard():
    from app.channels.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(bot_token="x:y")
    called = {"n": 0}

    async def _fake_post(method, payload, **kw):
        called["n"] += 1
        return {"ok": True}

    adapter._post = _fake_post  # type: ignore[assignment]

    # < 2 and > 10 are rejected client-side (Telegram requires 2..10).
    assert await adapter.send_media_group(7, [{"image_url": "https://x"}]) is False
    assert await adapter.send_media_group(7, [{"image_url": f"https://x/{i}"} for i in range(11)]) is False
    assert called["n"] == 0  # no Telegram call for out-of-range


@pytest.mark.asyncio
async def test_abc_default_send_media_group_returns_false():
    from app.channels.adapter import MessengerAdapter

    # The ABC default degrades to False so callers fall back to per-card.
    assert await MessengerAdapter.send_media_group(object(), 7, []) is False  # type: ignore[arg-type]


# ── respond hybrid output ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_respond_hybrid_album_plus_summary_en(monkeypatch):
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(8, lang="en")))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        res = await respond_tool.dispatch({"text": "Found some matches!"}, ctx)

        assert res["ok"] is True
        assert res["text_sent"] is True
        assert res["cards_sent"] == 5  # album capped at 5
        # ONE album, ONE summary keyboard message (+ the reply text).
        adapter.send_media_group.assert_awaited_once()
        (_, media), _ = adapter.send_media_group.await_args
        assert len(media) == 5
        adapter.send_text_with_keyboard.assert_awaited_once()
        (_, summary, keyboard), _ = adapter.send_text_with_keyboard.await_args
        assert "Here are 5 picks" in summary
        assert '<a href="https://shop.example.com/0">' in summary  # HTML buy link
        # Keyboard: 5 like buttons + footer row.
        assert len(keyboard[0]) == 5
        assert keyboard[0][0][1] == "card:like:p0"
        assert keyboard[-1][0][1] == "cards:more"
        assert keyboard[-1][1][1] == "cards:refine"
        assert "More" in keyboard[-1][0][0]
        # NO per-card streaming.
        adapter.send_card.assert_not_awaited()
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_respond_hybrid_summary_ko(monkeypatch):
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(5, lang="ko")))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        res = await respond_tool.dispatch({"text": "찾았어요!"}, ctx)

        assert res["cards_sent"] == 5
        (_, summary, keyboard), _ = adapter.send_text_with_keyboard.await_args
        assert "추려봤어요" in summary
        assert "더보기" in keyboard[-1][0][0]
        assert "다르게 찾기" in keyboard[-1][1][0]
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_respond_hybrid_idempotent_reentry(monkeypatch):
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(5)))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        first = await respond_tool.dispatch({"text": "hello"}, ctx)
        second = await respond_tool.dispatch({"text": "hello"}, ctx)  # simulated retry

        assert first["ok"] and first["cards_sent"] == 5
        assert second["ok"] and second["text_sent"] is False
        # Album + summary sent EXACTLY once total, not double-sent.
        adapter.send_media_group.assert_awaited_once()
        adapter.send_text_with_keyboard.assert_awaited_once()
        adapter.send_text.assert_awaited_once()
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_respond_hybrid_broken_image_prefiltered(monkeypatch):
    """Candidates without a plausible http(s) image URL are excluded from the
    album (sendMediaGroup is atomic — one bad URL fails the whole group)."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    sess = Session(chat_id=42, from_user_id=99)
    sess.last_results = [
        _candidate(0),
        _candidate(1, image="not-a-url"),  # filtered out
        _candidate(2, image=""),  # filtered out
        _candidate(3),
        _candidate(4),
    ]
    set_store(_FakeStore(sess))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        res = await respond_tool.dispatch({"text": "ok"}, ctx)

        assert res["cards_sent"] == 3  # only the 3 valid-image candidates
        (_, media), _ = adapter.send_media_group.await_args
        assert len(media) == 3
        assert all(m["image_url"].startswith("https://") for m in media)
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_respond_hybrid_atomic_fail_falls_back_to_per_card(monkeypatch):
    """sendMediaGroup atomic-fail → per-card send_card loop so a search never
    yields zero cards."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(5)))
    try:
        adapter = _adapter(group_ok=False)  # media group fails
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        res = await respond_tool.dispatch({"text": "fallback please"}, ctx)

        assert res["ok"] is True
        assert res["cards_sent"] == 5  # delivered via per-card fallback
        adapter.send_media_group.assert_awaited_once()
        assert adapter.send_card.await_count == 5  # fallback loop
        adapter.send_text_with_keyboard.assert_not_awaited()  # no summary on fallback
    finally:
        set_store(None)


# ── ingest callback handling ────────────────────────────────────────────────


def _cb_state(cb: str) -> WorkingState:
    msg = ChannelMessage(chat_id=42, from_user_id=99, callback_data=cb, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


@pytest.mark.asyncio
async def test_card_like_not_treated_as_fresh_query_and_reuses_click_plumbing(monkeypatch):
    from app.graphs.nodes import ingest as ingest_mod

    set_store(_FakeStore(_session_with_results(5)))
    try:
        rc = AsyncMock(return_value=1)
        monkeypatch.setattr("app.channels.implicit_feedback.record_click", rc)
        emitted: list = []
        monkeypatch.setattr(ingest_mod, "emit", lambda **kw: emitted.append(kw))

        # Re-query soft-penalty must NOT fire on a card:like tap.
        rq = AsyncMock(return_value=False)
        monkeypatch.setattr("app.channels.implicit_feedback.detect_and_apply_re_query", rq)
        monkeypatch.setattr("app.channels.implicit_feedback.attribute_expired_impressions", AsyncMock(return_value=0))

        out = await ingest_mod.ingest(_cb_state("card:like:p2"))

        rc.assert_awaited_once()
        args = rc.await_args.args
        assert args[2] == "p2"  # product_id resolved from last_results
        # re_query called with inbound_is_fresh_query=False (callback excluded).
        assert rq.await_args.args[1] is False
        # card_clicked conversation-log event emitted.
        assert any(e.get("event_type") == "card_clicked" and e["payload"]["product_id"] == "p2" for e in emitted)
        assert out["turn_no"] == 1
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_cards_more_sends_next_batch(monkeypatch):
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY
    from app.graphs.nodes import ingest as ingest_mod

    set_store(_FakeStore(_session_with_results(12)))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        # Turn 1: post-search reply → batch [0:5], cursor advances to 5.
        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        await respond_tool.dispatch({"text": "here"}, ctx)
        (_, media1), _ = adapter.send_media_group.await_args
        assert media1[0]["image_url"].endswith("/0.jpg")

        monkeypatch.setattr("app.channels.implicit_feedback.detect_and_apply_re_query", AsyncMock(return_value=False))
        monkeypatch.setattr("app.channels.implicit_feedback.attribute_expired_impressions", AsyncMock(return_value=0))

        # Turn 2 (fresh webhook): cards:more → next batch [5:10].
        await ingest_mod.ingest(_cb_state("cards:more"))
        (_, media2), _ = adapter.send_media_group.await_args
        assert len(media2) == 5
        assert media2[0]["image_url"].endswith("/5.jpg")
        assert adapter.send_media_group.await_count == 2
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_cards_refine_excluded_from_fresh_query(monkeypatch):
    """cards:refine carries no taste side effect in ingest and is excluded
    from the fresh-query predicate (flows to the agent which already exposes
    refine_search / suggest_next_step — verified via routing)."""
    from app.graphs.nodes import ingest as ingest_mod

    set_store(_FakeStore(_session_with_results(5)))
    try:
        rc = AsyncMock(return_value=1)
        monkeypatch.setattr("app.channels.implicit_feedback.record_click", rc)
        rq = AsyncMock(return_value=False)
        monkeypatch.setattr("app.channels.implicit_feedback.detect_and_apply_re_query", rq)
        monkeypatch.setattr("app.channels.implicit_feedback.attribute_expired_impressions", AsyncMock(return_value=0))

        out = await ingest_mod.ingest(_cb_state("cards:refine"))

        rc.assert_not_awaited()  # no taste mutation in ingest for refine
        assert rq.await_args.args[1] is False  # excluded from fresh-query
        assert out["turn_no"] == 1
    finally:
        set_store(None)


def test_routing_card_callbacks_terminal_vs_agent():
    """card:like / cards:more are terminal post-ingest; cards:refine → agent."""
    import inspect

    from app.graphs import fashion_bot

    src = inspect.getsource(fashion_bot.build_graph)
    assert 'cb.startswith("card:like:") or cb == "cards:more"' in src
    assert 'return "__end__"' in src
