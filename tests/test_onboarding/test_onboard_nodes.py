"""SPEC-ONBOARD-CARDS-001 Phase 3 — onboard_{mood,color,fit} node tests.

Covers REQ-ONBOARD-CARDS-002 toggle/done/skip semantics, bounds-blocking
toast, ✓ marker re-render (editMessageReplyMarkup), Pinterest-disabled
completion branch (REQ-ONBOARD-COMPLETION-001), and onboard_select emit
(REQ-ONBOARD-OBS-001).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.nodes.onboard_color import onboard_color
from app.graphs.nodes.onboard_fit import onboard_fit
from app.graphs.nodes.onboard_mood import onboard_mood
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import (
    InMemorySessionStore,
    SessionState,
    set_store,
)
from app.infrastructure.memory.taste_profile import (
    InMemoryTasteProfileStore,
    set_taste_store,
)


# ────────────────────────────────────────────────────────────────────────────
# FakeAdapter local extension (adds keyboard helpers used by onboarding)
# ────────────────────────────────────────────────────────────────────────────
class FakeAdapter:
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.keyboards: list[tuple[int, str, list]] = []
        self.edits: list[tuple[int, int, list]] = []
        self.callback_answers: list[tuple[str, str | None]] = []
        self.send_keyboard_returns: int | None = 2001
        self.edit_returns: bool = True

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))

    async def send_text_with_keyboard(self, chat_id, text, keyboard):
        self.keyboards.append((chat_id, text, [list(row) for row in keyboard]))
        return self.send_keyboard_returns

    async def edit_inline_keyboard(self, chat_id, message_id, keyboard):
        self.edits.append((chat_id, message_id, [list(row) for row in keyboard]))
        return self.edit_returns

    async def answer_callback_query(self, cbq_id, text=None):
        self.callback_answers.append((cbq_id, text))


def _make_state(chat_id: int = 1, *, callback_data: str = "", text: str = "") -> WorkingState:
    msg = ChannelMessage(
        chat_id=chat_id,
        from_user_id=10,
        text=text or None,
        callback_data=callback_data or None,
        callback_query_id="cbq-1" if callback_data else None,
        received_at=datetime.now(tz=UTC),
    )
    return WorkingState(message=msg, chat_id=chat_id, from_user_id=10)


@pytest.fixture
def adapter():
    a = FakeAdapter()
    tok = set_adapter(a)  # type: ignore[arg-type]
    yield a
    reset_adapter(tok)


@pytest.fixture
def session_store():
    store = InMemorySessionStore()
    set_store(store)
    yield store
    set_store(InMemorySessionStore())  # reset to fresh instance


@pytest.fixture
def taste_store():
    store = InMemoryTasteProfileStore()
    set_taste_store(store)
    yield store
    set_taste_store(InMemoryTasteProfileStore())


@pytest.fixture
def seeded_session(session_store):
    sess = session_store.get_or_create(1)
    sess.state = SessionState.IDLE
    sess.lang = "ko"
    sess.onboard_stage = "mood"
    sess.onboard_selections = {"mood": [], "color": [], "fit": []}
    sess.onboard_card_message_id = 2000
    return sess


# ────────────────────────────────────────────────────────────────────────────
# Mood — toggle / done / bounds
# ────────────────────────────────────────────────────────────────────────────
class TestMoodNode:
    @pytest.mark.asyncio
    async def test_toggle_adds_selection_and_edits_keyboard(self, adapter, seeded_session, session_store):
        state = _make_state(callback_data="onboard:mood:toggle:minimal")
        result = await onboard_mood(state)
        assert result["onboard_stage"] == "mood"
        assert seeded_session.onboard_selections["mood"] == ["minimal"]
        # Edit (not new send) — message_id was set.
        assert len(adapter.edits) == 1
        assert adapter.edits[0][1] == 2000  # message_id

    @pytest.mark.asyncio
    async def test_toggle_removes_existing_value(self, adapter, seeded_session, session_store):
        seeded_session.onboard_selections["mood"] = ["minimal", "street"]
        state = _make_state(callback_data="onboard:mood:toggle:minimal")
        await onboard_mood(state)
        assert seeded_session.onboard_selections["mood"] == ["street"]

    @pytest.mark.asyncio
    async def test_toggle_capped_at_max(self, adapter, seeded_session, session_store):
        seeded_session.onboard_selections["mood"] = ["minimal", "street", "y2k"]
        state = _make_state(callback_data="onboard:mood:toggle:vintage")
        await onboard_mood(state)
        # Stays at 3; toast served.
        assert seeded_session.onboard_selections["mood"] == ["minimal", "street", "y2k"]
        assert any("2~3" in (t or "") for _, t in adapter.callback_answers)

    @pytest.mark.asyncio
    async def test_done_below_min_blocked_with_toast(self, adapter, seeded_session, session_store):
        seeded_session.onboard_selections["mood"] = ["minimal"]  # below 2
        state = _make_state(callback_data="onboard:mood:done")
        result = await onboard_mood(state)
        assert result["onboard_stage"] == "mood"
        assert any("2~3" in (t or "") for _, t in adapter.callback_answers)

    @pytest.mark.asyncio
    async def test_done_advances_to_color_and_emits(self, adapter, seeded_session, session_store, monkeypatch):
        seeded_session.onboard_selections["mood"] = ["minimal", "street"]
        emitted: list[dict] = []
        monkeypatch.setattr(
            "app.graphs.nodes._onboard_stage.emit",
            lambda **kw: emitted.append(kw),
        )
        state = _make_state(callback_data="onboard:mood:done")
        result = await onboard_mood(state)
        assert result["onboard_stage"] == "color"
        assert seeded_session.onboard_stage == "color"
        assert emitted
        assert emitted[0]["event_type"] == "onboard_select"
        assert emitted[0]["payload"]["stage"] == "mood"
        assert emitted[0]["payload"]["selected_values"] == ["minimal", "street"]

    @pytest.mark.asyncio
    async def test_skip_advances_with_empty(self, adapter, seeded_session, session_store):
        state = _make_state(callback_data="onboard:mood:skip")
        result = await onboard_mood(state)
        assert result["onboard_stage"] == "color"
        assert seeded_session.onboard_selections["mood"] == []

    @pytest.mark.asyncio
    async def test_stale_callback_stays_on_stage(self, adapter, seeded_session, session_store):
        state = _make_state(callback_data="onboard:color:toggle:mono")  # wrong stage
        result = await onboard_mood(state)
        assert result["onboard_stage"] == "mood"

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_send(self, adapter, seeded_session, session_store):
        adapter.edit_returns = False
        state = _make_state(callback_data="onboard:mood:toggle:minimal")
        await onboard_mood(state)
        # send_text_with_keyboard called as fallback.
        assert adapter.keyboards


# ────────────────────────────────────────────────────────────────────────────
# Color — symmetric with mood; advances to fit
# ────────────────────────────────────────────────────────────────────────────
class TestColorNode:
    @pytest.mark.asyncio
    async def test_done_advances_to_fit(self, adapter, seeded_session, session_store):
        seeded_session.onboard_stage = "color"
        seeded_session.onboard_selections["color"] = ["mono", "earth"]
        state = _make_state(callback_data="onboard:color:done")
        result = await onboard_color(state)
        assert result["onboard_stage"] == "fit"

    @pytest.mark.asyncio
    async def test_toggle_color(self, adapter, seeded_session, session_store):
        seeded_session.onboard_stage = "color"
        state = _make_state(callback_data="onboard:color:toggle:pastel")
        await onboard_color(state)
        assert seeded_session.onboard_selections["color"] == ["pastel"]


# ────────────────────────────────────────────────────────────────────────────
# Fit — Pinterest enabled branch (advance) + Pinterest disabled completion branch
# ────────────────────────────────────────────────────────────────────────────
class TestFitNode:
    @pytest.mark.asyncio
    async def test_done_advances_to_pinterest_when_enabled(self, adapter, seeded_session, session_store, monkeypatch):
        monkeypatch.setattr("app.graphs.nodes.onboard_fit.settings.PINTEREST_BOOTSTRAP_ENABLED", True, raising=False)
        seeded_session.onboard_stage = "fit"
        seeded_session.onboard_selections["fit"] = ["oversize"]
        state = _make_state(callback_data="onboard:fit:done")
        result = await onboard_fit(state)
        assert result["onboard_stage"] == "pinterest"

    @pytest.mark.asyncio
    async def test_done_completes_when_pinterest_disabled(
        self, adapter, seeded_session, session_store, taste_store, monkeypatch
    ):
        monkeypatch.setattr(
            "app.graphs.nodes.onboard_fit.settings.PINTEREST_BOOTSTRAP_ENABLED",
            False,
            raising=False,
        )
        seeded_session.onboard_stage = "fit"
        seeded_session.onboard_selections["fit"] = ["oversize"]
        state = _make_state(callback_data="onboard:fit:done")
        result = await onboard_fit(state)
        assert result["onboard_stage"] == "done"
        assert seeded_session.onboarded_at is not None
        # Completion text dispatched.
        assert adapter.texts

    @pytest.mark.asyncio
    async def test_done_below_min_blocked_disabled_branch(self, adapter, seeded_session, session_store, monkeypatch):
        monkeypatch.setattr(
            "app.graphs.nodes.onboard_fit.settings.PINTEREST_BOOTSTRAP_ENABLED",
            False,
            raising=False,
        )
        seeded_session.onboard_stage = "fit"
        seeded_session.onboard_selections["fit"] = []  # below 1
        state = _make_state(callback_data="onboard:fit:done")
        result = await onboard_fit(state)
        assert result["onboard_stage"] == "fit"
        assert any("1~2" in (t or "") for _, t in adapter.callback_answers)

    @pytest.mark.asyncio
    async def test_skip_completes_when_disabled(self, adapter, seeded_session, session_store, taste_store, monkeypatch):
        monkeypatch.setattr(
            "app.graphs.nodes.onboard_fit.settings.PINTEREST_BOOTSTRAP_ENABLED",
            False,
            raising=False,
        )
        seeded_session.onboard_stage = "fit"
        state = _make_state(callback_data="onboard:fit:skip")
        result = await onboard_fit(state)
        assert result["onboard_stage"] == "done"
        assert seeded_session.onboarded_at is not None
