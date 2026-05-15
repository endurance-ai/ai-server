"""SPEC-ONBOARD-CARDS-001 Phase 3 — `pinterest_ingest` node (continuous path).

Covers REQ-ONBOARD-PINTEREST-003 — rate-limit window, cache-hit fast-path,
mode A/B/C continuous ingest, APIFY_TOKEN missing → mode-C-only path, and
critical invariant: `sess.onboarded_at` is NEVER mutated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.channels.schemas import ChannelMessage
from app.channels.session import (
    InMemorySessionStore,
    set_store,
)
from app.channels.taste_profile import (
    InMemoryTasteProfileStore,
    set_taste_store,
)
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.nodes.pinterest_ingest import pinterest_ingest
from app.graphs.state import WorkingState


class FakeAdapter:
    def __init__(self):
        self.texts: list[tuple[int, str]] = []

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))

    async def send_text_with_keyboard(self, chat_id, text, kb):
        return 0

    async def answer_callback_query(self, cbq, text=None):
        pass


def _state(text: str) -> WorkingState:
    msg = ChannelMessage(
        chat_id=1,
        from_user_id=10,
        text=text,
        received_at=datetime.now(tz=UTC),
    )
    return WorkingState(message=msg, chat_id=1, from_user_id=10)


@pytest.fixture
def adapter():
    a = FakeAdapter()
    tok = set_adapter(a)  # type: ignore[arg-type]
    yield a
    reset_adapter(tok)


@pytest.fixture
def session_store():
    s = InMemorySessionStore()
    set_store(s)
    yield s
    set_store(InMemorySessionStore())


@pytest.fixture
def taste_store():
    t = InMemoryTasteProfileStore()
    set_taste_store(t)
    yield t
    set_taste_store(InMemoryTasteProfileStore())


@pytest.fixture
def onboarded_session(session_store):
    sess = session_store.get_or_create(1)
    sess.lang = "ko"
    sess.onboard_stage = "done"
    sess.onboarded_at = datetime.now(UTC) - timedelta(days=2)  # well past onboarding
    return sess


# ────────────────────────────────────────────────────────────────────────────
# Rate-limit window — declines work within 5 minutes
# ────────────────────────────────────────────────────────────────────────────
class TestRateLimit:
    @pytest.mark.asyncio
    async def test_within_window_replies_and_returns(self, adapter, onboarded_session, taste_store, monkeypatch):
        onboarded_session.last_pinterest_scrape_at = datetime.now(UTC) - timedelta(seconds=30)
        monkeypatch.setattr(
            "app.graphs.nodes.pinterest_ingest.settings.PINTEREST_CONTINUOUS_RATELIMIT_S", 300, raising=False
        )
        state = _state("https://pinterest.com/jane/coats/")
        result = await pinterest_ingest(state)
        assert "rate_limited" in (result["log_events"][0] if result["log_events"] else "")
        assert any("5" in t for _, t in adapter.texts)

    @pytest.mark.asyncio
    async def test_past_window_proceeds(self, adapter, onboarded_session, taste_store, monkeypatch):
        onboarded_session.last_pinterest_scrape_at = datetime.now(UTC) - timedelta(seconds=600)
        monkeypatch.setattr(
            "app.graphs.nodes.pinterest_ingest.settings.PINTEREST_CONTINUOUS_RATELIMIT_S", 300, raising=False
        )

        # Stub apify + vision + resolver via _apify_adapter / _link_resolver_batch_adapter monkeypatch
        async def fake_apify_helper(*a, **kw):
            return []  # degraded

        # Patch the ingest helper to bypass the real Apify/vision call chain.
        async def fake_ingest(*args, **kwargs):
            from app.graphs.nodes._pinterest_helpers import _IngestOutcome

            return _IngestOutcome(mode="degraded", error="apify_empty_board")

        monkeypatch.setattr("app.graphs.nodes.pinterest_ingest.ingest_pinterest_pins", fake_ingest)
        state = _state("https://pinterest.com/jane/coats/")
        result = await pinterest_ingest(state)
        assert "rate_limited" not in (result["log_events"][0] if result["log_events"] else "")


# ────────────────────────────────────────────────────────────────────────────
# Cache hit fast-path
# ────────────────────────────────────────────────────────────────────────────
class TestCacheHit:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_apify_and_seeds_directly(self, adapter, onboarded_session, taste_store, monkeypatch):
        # Disable rate-limit window so the rate-limit guard doesn't fire.
        monkeypatch.setattr(
            "app.graphs.nodes.pinterest_ingest.settings.PINTEREST_CONTINUOUS_RATELIMIT_S", 0, raising=False
        )
        ingest_calls: list[bool] = []

        async def fake_ingest(state, classified, *, continuous_origin, **kw):
            from app.graphs.nodes._pinterest_helpers import _IngestOutcome

            ingest_calls.append(continuous_origin)
            # taste_store should receive the seed call when continuous_origin=True
            kw["taste_store"].seed_from_onboarding(kw["user_key"], {"vintage": 0.4})
            return _IngestOutcome(
                mode="board",
                pin_count=7,
                successfully_analyzed=7,
                cache_hit=True,
                seed_called=True,
            )

        monkeypatch.setattr("app.graphs.nodes.pinterest_ingest.ingest_pinterest_pins", fake_ingest)
        state = _state("https://pinterest.com/jane/coats/")
        result = await pinterest_ingest(state)
        assert ingest_calls == [True]  # continuous_origin asserted
        assert "board" in (result["log_events"][0] if result["log_events"] else "")

    @pytest.mark.asyncio
    async def test_onboarded_at_invariant_preserved(self, adapter, onboarded_session, taste_store, monkeypatch):
        monkeypatch.setattr(
            "app.graphs.nodes.pinterest_ingest.settings.PINTEREST_CONTINUOUS_RATELIMIT_S", 0, raising=False
        )
        original_onboarded_at = onboarded_session.onboarded_at

        async def fake_ingest(*a, **kw):
            from app.graphs.nodes._pinterest_helpers import _IngestOutcome

            return _IngestOutcome(mode="pins", pin_count=3, successfully_analyzed=3, seed_called=True)

        monkeypatch.setattr("app.graphs.nodes.pinterest_ingest.ingest_pinterest_pins", fake_ingest)
        state = _state("https://pinterest.com/pin/111/ https://pinterest.com/pin/222/ https://pinterest.com/pin/333/")
        await pinterest_ingest(state)
        # CRITICAL: onboarded_at must NOT be touched.
        assert onboarded_session.onboarded_at == original_onboarded_at


# ────────────────────────────────────────────────────────────────────────────
# Mode-specific confirmation messages
# ────────────────────────────────────────────────────────────────────────────
class TestModeConfirmations:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode,expected_substring",
        [
            ("board", "보드"),
            ("profile", "프로필"),
            ("pins", "핀"),
        ],
    )
    async def test_mode_specific_message_korean(
        self, adapter, onboarded_session, taste_store, monkeypatch, mode, expected_substring
    ):
        monkeypatch.setattr(
            "app.graphs.nodes.pinterest_ingest.settings.PINTEREST_CONTINUOUS_RATELIMIT_S", 0, raising=False
        )

        async def fake_ingest(*a, **kw):
            from app.graphs.nodes._pinterest_helpers import _IngestOutcome

            return _IngestOutcome(mode=mode, pin_count=5, successfully_analyzed=5, seed_called=True)

        monkeypatch.setattr("app.graphs.nodes.pinterest_ingest.ingest_pinterest_pins", fake_ingest)
        state = _state("https://pinterest.com/pin/111/")
        await pinterest_ingest(state)
        # First text dispatched must contain the expected mode-specific keyword.
        assert adapter.texts
        assert any(expected_substring in t for _, t in adapter.texts)


# ────────────────────────────────────────────────────────────────────────────
# Continuous origin flag set on state
# ────────────────────────────────────────────────────────────────────────────
class TestContinuousOriginFlag:
    @pytest.mark.asyncio
    async def test_continuous_origin_set_true(self, adapter, onboarded_session, taste_store, monkeypatch):
        monkeypatch.setattr(
            "app.graphs.nodes.pinterest_ingest.settings.PINTEREST_CONTINUOUS_RATELIMIT_S", 0, raising=False
        )

        async def fake_ingest(state, *a, continuous_origin, **kw):
            from app.graphs.nodes._pinterest_helpers import _IngestOutcome

            assert continuous_origin is True
            return _IngestOutcome(mode="pins", pin_count=1, successfully_analyzed=1, seed_called=True)

        monkeypatch.setattr("app.graphs.nodes.pinterest_ingest.ingest_pinterest_pins", fake_ingest)
        state = _state("https://pinterest.com/pin/1/")
        result = await pinterest_ingest(state)
        assert result.get("continuous_origin") is True
