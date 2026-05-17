"""SPEC-ONBOARD-CARDS-001 Phase 3 — Pinterest URL validation + 3-strike skip.

Covers REQ-ONBOARD-PINTEREST-002 (scheme rejection, attack URLs → NONE) and
REQ-ONBOARD-PINTEREST-001 strike-tracking inside onboard_pinterest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.channels.pinterest_url import (
    PinInputNone,
    classify_pinterest_input,
)
from app.channels.schemas import ChannelMessage
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.nodes.onboard_pinterest import onboard_pinterest
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import (
    InMemorySessionStore,
    set_store,
)
from app.infrastructure.memory.taste_profile import (
    InMemoryTasteProfileStore,
    set_taste_store,
)


class FakeAdapter:
    def __init__(self):
        self.texts: list[tuple[int, str]] = []
        self.keyboards: list = []
        self.callback_answers: list = []

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))

    async def send_text_with_keyboard(self, chat_id, text, kb):
        self.keyboards.append((chat_id, text, kb))
        return 3000

    async def edit_inline_keyboard(self, chat_id, mid, kb):
        return True

    async def answer_callback_query(self, cbq_id, text=None):
        self.callback_answers.append((cbq_id, text))


def _make_state(text: str = "", callback_data: str = "") -> WorkingState:
    msg = ChannelMessage(
        chat_id=1,
        from_user_id=10,
        text=text or None,
        callback_data=callback_data or None,
        callback_query_id="cb-1" if callback_data else None,
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


# ────────────────────────────────────────────────────────────────────────────
# Scheme rejection — classifier-level safety
# ────────────────────────────────────────────────────────────────────────────
class TestSchemeRejection:
    # SPEC-ONBOARD-CARDS-001 ONB-T21 — broaden attack-URL matrix to 20+ cases.
    # All entries MUST classify to PinInputNone; tightening the classifier
    # to accept any of these is a security regression and must fail this test.
    @pytest.mark.parametrize(
        "bad",
        [
            # Dangerous schemes
            "javascript:alert(1)",
            "data:text/html,<script>",
            "data:image/svg+xml;base64,PHN2Zz4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "ftp://pinterest.com/jane/",
            "gopher://pinterest.com/",
            # Host-confusion / suffix appending
            "http://pinterest.com.evil.com/jane/",
            "http://evil.com/pinterest.com/",
            "http://pinterest.com@evil.com/",
            "http://pinterest.com#@evil.com/",
            # Subdomain look-alike NOT in allowlist
            "http://api.pinterest.com.evil.com/pin/1/",
            # Internal / SSRF targets
            "http://127.0.0.1/pin/1/",
            "http://localhost/pin/1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/pin/1/",
            "http://0.0.0.0/pin/1/",
            # IDN homograph mimicking pinterest.com (Cyrillic а / е)
            "http://pinterеst.com/jane/",
            "http://piṅterest.com/jane/",
            # Empty / malformed
            "",
            "   ",
            "://pinterest.com/",
            "pinterest.com",  # no scheme
        ],
    )
    def test_attack_urls_classified_none(self, bad):
        result = classify_pinterest_input(bad)
        assert isinstance(result, PinInputNone), f"Attack URL leaked: {bad!r}"


# ────────────────────────────────────────────────────────────────────────────
# 3-strike auto-skip inside onboard_pinterest
# ────────────────────────────────────────────────────────────────────────────
class TestThreeStrikeAutoSkip:
    @pytest.mark.asyncio
    async def test_first_invalid_url_increments_strike(self, adapter, session_store, taste_store):
        sess = session_store.get_or_create(1)
        sess.onboard_stage = "pinterest"
        sess.onboard_selections = {"mood": [], "color": [], "fit": []}
        state = _make_state(text="not a pinterest url at all")
        result = await onboard_pinterest(state)
        assert result["onboard_stage"] == "pinterest"
        # Strike counter increments inside onboard_selections.
        assert sess.onboard_selections.get("pinterest_strikes") == 1
        # Invalid-url text dispatched.
        assert adapter.texts

    @pytest.mark.asyncio
    async def test_third_strike_auto_skips(self, adapter, session_store, taste_store):
        sess = session_store.get_or_create(1)
        sess.onboard_stage = "pinterest"
        sess.onboard_selections = {
            "mood": ["minimal", "street"],
            "color": ["mono", "earth"],
            "fit": ["oversize"],
            "pinterest_strikes": 2,
        }
        state = _make_state(text="still no url")
        result = await onboard_pinterest(state)
        assert result["onboard_stage"] == "done"
        assert sess.onboarded_at is not None

    @pytest.mark.asyncio
    async def test_valid_pin_url_runs_ingest(self, adapter, session_store, taste_store, monkeypatch):
        sess = session_store.get_or_create(1)
        sess.onboard_stage = "pinterest"
        sess.onboard_selections = {"mood": ["minimal", "street"], "color": ["mono", "earth"], "fit": ["oversize"]}

        async def fake_ingest(state, classified, *, continuous_origin, **kw):
            from app.graphs.nodes._pinterest_helpers import _IngestOutcome

            assert continuous_origin is False  # onboarding path
            return _IngestOutcome(mode="pins", pin_count=2, successfully_analyzed=2, seed_called=False)

        monkeypatch.setattr("app.graphs.nodes.onboard_pinterest.ingest_pinterest_pins", fake_ingest)
        state = _make_state(text="https://pinterest.com/pin/111/ https://pinterest.com/pin/222/")
        result = await onboard_pinterest(state)
        assert result["onboard_stage"] == "done"
        assert sess.onboarded_at is not None

    @pytest.mark.asyncio
    async def test_no_apify_token_with_board_url_stays_on_stage(self, adapter, session_store, taste_store, monkeypatch):
        monkeypatch.setattr("app.graphs.nodes.onboard_pinterest.settings.APIFY_TOKEN", "", raising=False)
        sess = session_store.get_or_create(1)
        sess.onboard_stage = "pinterest"
        sess.onboard_selections = {"mood": [], "color": [], "fit": []}
        state = _make_state(text="https://pinterest.com/jane/coats/")
        result = await onboard_pinterest(state)
        assert result["onboard_stage"] == "pinterest"  # stays; degraded
        assert adapter.texts

    @pytest.mark.asyncio
    async def test_entry_renders_card_when_no_input(self, adapter, session_store, taste_store):
        sess = session_store.get_or_create(1)
        sess.onboard_stage = "fit"  # just-advanced from fit
        sess.onboard_selections = {"mood": [], "color": [], "fit": []}
        state = _make_state()  # no text, no callback
        result = await onboard_pinterest(state)
        assert result["onboard_stage"] == "pinterest"
        assert adapter.keyboards  # card rendered

    @pytest.mark.asyncio
    async def test_url_mode_callback_arms_stage(self, adapter, session_store, taste_store):
        sess = session_store.get_or_create(1)
        sess.onboard_stage = "pinterest"
        sess.onboard_selections = {"mood": [], "color": [], "fit": []}
        state = _make_state(callback_data="onboard:pinterest:url_mode")
        result = await onboard_pinterest(state)
        assert result["onboard_stage"] == "pinterest"

    @pytest.mark.asyncio
    async def test_skip_callback_finalizes(self, adapter, session_store, taste_store):
        sess = session_store.get_or_create(1)
        sess.onboard_stage = "pinterest"
        sess.onboard_selections = {"mood": ["minimal", "street"], "color": ["mono", "earth"], "fit": ["oversize"]}
        state = _make_state(callback_data="onboard:pinterest:skip")
        result = await onboard_pinterest(state)
        assert result["onboard_stage"] == "done"
        assert sess.onboarded_at is not None
