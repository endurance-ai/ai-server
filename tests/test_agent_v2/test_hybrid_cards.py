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


# UX flag override (2026-06-04, SPEC-CARD-DELIVERY-001):
# settings.INDIVIDUAL_CARD_DELIVERY defaults to True (per-card delivery in
# production), but this module's tests verify the LEGACY album path
# (sendMediaGroup + atomic-fail → per-card fallback). Force the flag OFF for
# every test in this file so album-mode assertions continue to hold. The
# per-card primary path is exercised separately in new dedicated tests.
@pytest.fixture(autouse=True)
def _force_album_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.config.settings.INDIVIDUAL_CARD_DELIVERY", False)


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
def _reset_pager(monkeypatch):
    """SPEC-CHAT-STATE-REDIS-001 — bind chat_state._pool to a fresh fakeredis
    per test so cursor + impression-dedupe state is isolated. Replaces the
    pre-SPEC `reset_card_batch_cursor_for_tests` module-global wipe."""
    import fakeredis.aioredis

    from app.infrastructure.cache import chat_state

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_state, "_pool", client)
    yield
    monkeypatch.setattr(chat_state, "_pool", None)


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
        # UX 260611 — the legacy `🔄 다르게 찾기` button was removed; the
        # "다른 스타일로" affordance moved into the closing prompt
        # ("What do you think of this style? Want me to try a different vibe?").
        # Summary keyboard is now footer-only with ONLY the `➕ More` pager
        # button (when a next batch exists).
        assert len(keyboard) == 1
        assert len(keyboard[0]) == 1
        assert keyboard[0][0][1] == "cards:more"
        assert "More" in keyboard[0][0][0]
        # Closing prompt copy is now in the summary text body.
        assert "What do you think of this style?" in summary
        # NO per-card streaming.
        adapter.send_card.assert_not_awaited()
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_respond_hybrid_summary_ko(monkeypatch):
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    # 8 results → first batch is 5, a next batch exists → 더보기 shown.
    set_store(_FakeStore(_session_with_results(8, lang="ko")))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        res = await respond_tool.dispatch({"text": "찾았어요!"}, ctx)

        assert res["cards_sent"] == 5
        (_, summary, keyboard), kwargs = adapter.send_text_with_keyboard.await_args
        assert "추려봤어" in summary
        assert summary.splitlines()[2].startswith("1.")  # numbered "1." format
        assert kwargs.get("parse_mode") == "HTML"
        # UX 260611 — footer now has ONLY `➕ 더보기` (when a next batch exists);
        # `🔄 다르게 찾기` removed. The closing prompt invites a natural-language
        # follow-up which the agent routes via refine_search / search_products.
        assert "더보기" in keyboard[-1][0][0]
        assert "이 스타일 어때?" in summary
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_respond_hybrid_no_more_button_when_single_batch(monkeypatch):
    """≤5 results → no next batch → `➕ 더보기` suppressed → empty keyboard.

    UX 260611 — `🔄 다르게 찾기` was removed, so a single-batch result has no
    keyboard at all. The closing prompt in the summary text invites the
    natural-language refinement instead.
    """
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(5, lang="ko")))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        res = await respond_tool.dispatch({"text": "찾았어요!"}, {"chat_id": 42, CARDS_READY_KEY: True})

        assert res["cards_sent"] == 5
        (_, summary, keyboard), _ = adapter.send_text_with_keyboard.await_args
        # No keyboard rows at all when single batch.
        assert keyboard == []
        # Closing prompt still invites refinement via natural language.
        assert "이 스타일 어때?" in summary
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
    """card:like / cards:more / cap:membership_interest are terminal post-ingest;
    cards:refine → agent.

    260610 — the source check was loosened to substring matches because the
    routing condition is now a multi-line `if (... or ... or ...):` block.
    Asserting individual token substrings keeps the contract without coupling
    to the exact formatting.
    """
    import inspect

    from app.graphs import fashion_bot

    src = inspect.getsource(fashion_bot.build_graph)
    assert 'cb.startswith("card:like:")' in src
    assert 'cb == "cards:more"' in src
    assert 'cb == "cap:membership_interest"' in src
    assert 'return "__end__"' in src


# ── SPEC-IMPLICIT-FB-001 / REQ-FB-IMPRESSION-001 ────────────────────────────
# Impression logging on the LIVE hybrid delivery path (the only path on the
# permanent ReAct topology — send_results is NOT registered in the graph).


@pytest.mark.asyncio
async def test_hybrid_delivery_logs_impressions_for_delivered_candidates(monkeypatch):
    """When send_hybrid_batch delivers a batch, log_impressions is called once
    with EXACTLY the delivered candidates + the session's from_user_id."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(8)))  # from_user_id=99
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)
        log_imp = AsyncMock(return_value=5)
        # Patch at the SOURCE module: _log_delivered_impressions lazily imports
        # `log_impressions` from app.channels.implicit_feedback at call time
        # (the lazy import avoids a circular import — do NOT hoist it), so the
        # source-module attr is what the call resolves.
        monkeypatch.setattr("app.channels.implicit_feedback.log_impressions", log_imp)

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        res = await respond_tool.dispatch({"text": "found"}, ctx)

        assert res["cards_sent"] == 5
        log_imp.assert_awaited_once()
        (chat_id_arg, from_user_arg, products_arg), _ = log_imp.await_args
        assert chat_id_arg == 42
        assert from_user_arg == 99  # sourced from sess.from_user_id like send_results
        # EXACTLY the 5 delivered candidates (album cap), in order.
        assert [c.id for c in products_arg] == ["p0", "p1", "p2", "p3", "p4"]
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_cards_more_logs_only_new_batch_no_double_log(monkeypatch):
    """`cards:more` impression-logs the NEXT batch's candidates; the same
    product_id is never logged twice across batches (no ON CONFLICT in 0002)."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY
    from app.graphs.nodes import ingest as ingest_mod

    set_store(_FakeStore(_session_with_results(12)))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)
        logged_pids: list[list[str]] = []

        async def _capture(chat_id, from_user_id, products):
            logged_pids.append([c.id for c in products])
            return len(products)

        monkeypatch.setattr("app.channels.implicit_feedback.log_impressions", AsyncMock(side_effect=_capture))
        monkeypatch.setattr("app.channels.implicit_feedback.detect_and_apply_re_query", AsyncMock(return_value=False))
        monkeypatch.setattr("app.channels.implicit_feedback.attribute_expired_impressions", AsyncMock(return_value=0))

        # Turn 1: post-search reply → batch [p0..p4].
        await respond_tool.dispatch({"text": "here"}, {"chat_id": 42, CARDS_READY_KEY: True})
        # Turn 2: cards:more → next batch [p5..p9].
        await ingest_mod.ingest(_cb_state("cards:more"))

        assert logged_pids[0] == ["p0", "p1", "p2", "p3", "p4"]
        assert logged_pids[1] == ["p5", "p6", "p7", "p8", "p9"]
        # No product_id appears in more than one logged batch.
        flat = [pid for batch in logged_pids for pid in batch]
        assert len(flat) == len(set(flat))
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_new_search_suppresses_previously_shown_products(monkeypatch):
    """260611 UX dedup — a product shown in search #1 MUST be suppressed when it
    appears again in a SUBSEQUENT search (whether fresh or refine), to prevent
    repeated MUSED/Mardi/ZARA fatigue across `crit:more` / new search turns.

    Inverts the prior `test_new_search_relogs_product_shown_in_previous_search`
    contract: cross-turn dedup now wins over Langfuse re-attribution, because
    the re-attribution edge case (a click on a re-recommended product) is
    suppressed upstream — re-recommendations no longer happen.

    The dedupe set is cleared on `/reset` and naturally by its 7-day Redis TTL.
    """
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    try:
        logged_pids: list[list[str]] = []

        async def _capture(chat_id, from_user_id, products):
            logged_pids.append([c.id for c in products])
            return len(products)

        monkeypatch.setattr("app.channels.implicit_feedback.log_impressions", AsyncMock(side_effect=_capture))
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

        # Search #1 → [p0, p1, p2] all delivered + impression-logged.
        set_store(_FakeStore(_session_with_results(3)))
        await respond_tool.dispatch({"text": "first"}, {"chat_id": 42, CARDS_READY_KEY: True})
        assert logged_pids == [["p0", "p1", "p2"]]

        # Search #2 (NEW search, fresh offset==0) with the SAME pids → must
        # deliver 0 (all filtered as previously-shown) → log_impressions NOT
        # called again because there's nothing to deliver.
        from app.infrastructure.cache import chat_state as _cs

        await _cs.set_cursor(42, 0)
        set_store(_FakeStore(_session_with_results(3)))
        res = await respond_tool.dispatch({"text": "second"}, {"chat_id": 42, CARDS_READY_KEY: True})
        assert res["cards_sent"] == 0
        # No second logging — products were filtered upstream.
        assert logged_pids == [["p0", "p1", "p2"]]
    finally:
        set_store(None)


@pytest.mark.asyncio
async def test_impression_logging_failure_does_not_break_delivery(monkeypatch):
    """A raising log_impressions MUST NOT undo or fail the card delivery."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(6)))
    try:
        adapter = _adapter(group_ok=True)
        monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)
        monkeypatch.setattr(
            "app.channels.implicit_feedback.log_impressions",
            AsyncMock(side_effect=RuntimeError("db down")),
        )

        ctx = {"chat_id": 42, CARDS_READY_KEY: True}
        res = await respond_tool.dispatch({"text": "found"}, ctx)

        # Delivery still fully succeeded despite the impression-logging blowup.
        assert res["ok"] is True
        assert res["cards_sent"] == 5
        adapter.send_media_group.assert_awaited_once()
        adapter.send_text_with_keyboard.assert_awaited_once()
    finally:
        set_store(None)
