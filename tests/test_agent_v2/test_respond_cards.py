"""SPEC-AGENT-V2-REACT — respond cards-spam fix.

Verifies the LLM never hand-serializes cards; the respond tool sources real
product cards internally from the turn's last search (`sess.last_results`),
the send_text empty guard, RespondArgs no longer accepts `cards`, and respond
is idempotent under react_loop's transient retry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.session import Session, SessionState, set_store


class _FakeStore:
    """Minimal in-memory store returning a single fixed session."""

    def __init__(self, sess: Session) -> None:
        self._sess = sess

    def get_or_create(self, chat_id: int) -> Session:
        return self._sess

    def update(self, sess: Session) -> None:  # noqa: D401 — protocol parity
        self._sess = sess


def _candidate(i: int):
    from app.models.response import Candidate

    return Candidate(
        id=f"p{i}",
        brand=f"Brand{i}",
        name=f"Slim Tee {i}",
        price=39000 + i,
        image_url=f"https://img.example.com/{i}.jpg",
        product_url=f"https://shop.example.com/{i}",
        platform="kiko",
        subcategory="t-shirt",
        score=0.9 - i * 0.01,
    )


def _session_with_results(n: int) -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE)
    s.last_results = [_candidate(i) for i in range(n)]
    return s


# --- RespondArgs schema: `cards` removed ---


def test_respond_args_has_no_cards_field():
    from app.agents.tool_registry import RespondArgs, validate_args

    assert "cards" not in RespondArgs.__annotations__
    # LLM passing a `cards` arg is now an unknown-key validation failure.
    ok, err = validate_args("respond", {"text": "hi", "cards": [{"x": 1}]})
    assert ok is False
    assert "unknown_keys" in (err or "")
    # text-only respond still valid.
    ok, _ = validate_args("respond", {"text": "hello"})
    assert ok is True


# --- search persists FULL candidates to sess.last_results ---


@pytest.mark.asyncio
async def test_search_persists_last_results(monkeypatch):
    import app.agents.tools.search_products as sp

    sess = Session(chat_id=7)
    store = _FakeStore(sess)
    set_store(store)
    try:

        async def fake_search_step(state):
            state.raw_candidates = [
                {
                    "id": "p1",
                    "name": "Slim Black Tee",
                    "brand": "Acme",
                    "price": 29000,
                    "image_url": "https://img/x.jpg",
                    "product_url": "https://shop/x",
                }
            ]
            return state

        async def fake_diversify_step(state):
            state.final_candidates = list(state.raw_candidates)
            return state

        monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
        monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

        res = await sp.dispatch({"text_query": "slim black tee"}, {"chat_id": 7, "image_url": ""})
        assert res["ok"] is True
        # Full candidate promoted to a Candidate model (attribute access works
        # for the V1 card renderer) and stashed in the EXISTING last_results.
        assert len(sess.last_results) == 1
        c = sess.last_results[0]
        assert getattr(c, "id") == "p1"
        assert getattr(c, "image_url") == "https://img/x.jpg"
        assert "p1" in sess.shown_product_ids
    finally:
        set_store(None)


# --- respond auto-sources real cards from last search ---


@pytest.mark.asyncio
async def test_respond_auto_sources_cards(monkeypatch):
    from app.agents.tools import respond as respond_tool

    sess = _session_with_results(3)
    set_store(_FakeStore(sess))
    try:
        adapter = MagicMock()
        adapter.send_text = AsyncMock()
        adapter.send_card = AsyncMock(return_value=1001)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42}
        res = await respond_tool.dispatch({"text": "Here are some matches!"}, ctx)

        assert res["ok"] is True
        assert res["text_sent"] is True
        assert res["cards_sent"] == 3
        # Exactly ONE text message (no 1-char spam).
        adapter.send_text.assert_awaited_once()
        assert adapter.send_card.await_count == 3
        # Every send_text payload is a real non-trivial string.
        (_, sent_text), _ = adapter.send_text.await_args
        assert len(sent_text) > 5
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_respond_no_prior_search_text_only(monkeypatch):
    from app.agents.tools import respond as respond_tool

    sess = Session(chat_id=42)  # last_results empty
    set_store(_FakeStore(sess))
    try:
        adapter = MagicMock()
        adapter.send_text = AsyncMock()
        adapter.send_card = AsyncMock()
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        res = await respond_tool.dispatch({"text": "hi there!"}, {"chat_id": 42})

        assert res["ok"] is True
        assert res["cards_sent"] == 0
        adapter.send_text.assert_awaited_once()
        adapter.send_card.assert_not_awaited()
    finally:
        set_store(None)


# --- send_text empty/whitespace guard ---


@pytest.mark.asyncio
async def test_send_text_empty_guard():
    from app.channels.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(bot_token="x:y")
    posted: list = []

    async def _fake_post(method, payload, **kw):
        posted.append((method, payload))
        return {"ok": True}

    adapter._post = _fake_post  # type: ignore[assignment]

    await adapter.send_text(123, "")
    await adapter.send_text(123, "   ")
    await adapter.send_text(123, "\n\t ")
    assert posted == []  # NO Telegram call for empty/whitespace

    await adapter.send_text(123, "real")
    assert len(posted) == 1


# --- respond idempotency under react_loop transient retry ---


@pytest.mark.asyncio
async def test_respond_idempotent_on_retry(monkeypatch):
    from app.agents.tools import respond as respond_tool

    sess = _session_with_results(2)
    set_store(_FakeStore(sess))
    try:
        adapter = MagicMock()
        adapter.send_text = AsyncMock()
        adapter.send_card = AsyncMock(return_value=1)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42}  # same dict reused across retries (react_loop contract)
        first = await respond_tool.dispatch({"text": "hello"}, ctx)
        second = await respond_tool.dispatch({"text": "hello"}, ctx)  # simulated retry

        assert first["ok"] is True and first["text_sent"] is True
        assert second["ok"] is True and second["text_sent"] is False
        # Text + cards sent exactly once total — NOT double-sent.
        adapter.send_text.assert_awaited_once()
        assert adapter.send_card.await_count == 2
    finally:
        set_store(None)
