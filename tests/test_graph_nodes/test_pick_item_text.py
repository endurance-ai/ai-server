"""B6 — text-digit pick path in pick_item.

When the user types "2", "2번", "2,4번", "두번째" instead of tapping the
inline keyboard, pick_item must parse the index out of the text and pick
the item (single index) or first parsed index + ack the rest (multi).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.nodes.pick_item import _parse_indices_from_text, pick_item
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState, set_store


class _FakeStore:
    def __init__(self, sess: Session) -> None:
        self._sess = sess

    def get_or_create(self, chat_id: int) -> Session:
        return self._sess

    def update(self, sess: Session) -> None:
        self._sess = sess


@pytest.fixture(autouse=True)
def _reset_store():
    yield
    set_store(None)


@pytest.fixture
def _adapter():
    from app.graphs.nodes import _adapter_ctx

    a = MagicMock()
    a.send_text = AsyncMock()
    a.send_text_with_keyboard = AsyncMock()
    token = _adapter_ctx.set_adapter(a)
    yield a
    _adapter_ctx.reset_adapter(token)


def _items(n: int) -> list[dict]:
    return [{"label": f"item-{i}", "keywords": [f"kw{i}"], "subcategory": "top"} for i in range(n)]


def _state_text(text: str, items: list[dict]) -> WorkingState:
    msg = ChannelMessage(chat_id=42, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99, detected_items=items)


def _session(items: list[dict]) -> Session:
    s = Session(chat_id=42, state=SessionState.AWAITING_ITEM_PICK, from_user_id=99)
    s.detected_items = items
    s.lang = "ko"
    return s


# ── _parse_indices_from_text ────────────────────────────────────────────────


class TestParseIndices:
    def test_single_digit(self):
        assert _parse_indices_from_text("2", max_index=4) == [1]

    def test_single_with_suffix(self):
        assert _parse_indices_from_text("2번", max_index=4) == [1]

    def test_multi_digit_with_suffix(self):
        assert _parse_indices_from_text("2,4번", max_index=4) == [1, 3]

    def test_multi_with_korean_connector(self):
        assert _parse_indices_from_text("1번이랑 3번", max_index=4) == [0, 2]
        assert _parse_indices_from_text("1과 3", max_index=4) == [0, 2]

    def test_dedup_preserves_order(self):
        assert _parse_indices_from_text("2, 2, 4", max_index=4) == [1, 3]

    def test_out_of_range_dropped(self):
        # max_index=3 → only 1..3 valid
        assert _parse_indices_from_text("2, 5, 1", max_index=3) == [1, 0]

    def test_ko_ordinal_word(self):
        assert _parse_indices_from_text("두번째", max_index=4) == [1]

    def test_en_ordinal_word(self):
        assert _parse_indices_from_text("the first one", max_index=4) == [0]

    def test_digits_beat_ordinals(self):
        # Should pick 3 (digit) not "first".
        assert _parse_indices_from_text("first 3", max_index=4) == [2]

    def test_empty_text(self):
        assert _parse_indices_from_text("", max_index=4) == []
        assert _parse_indices_from_text("   ", max_index=4) == []

    def test_no_digit_no_ordinal(self):
        assert _parse_indices_from_text("뭐가 좋아 보여", max_index=4) == []


# ── pick_item dispatch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_pick_single_index_routes_to_selection(_adapter):
    items = _items(4)
    sess = _session(items)
    set_store(_FakeStore(sess))

    delta = await pick_item(_state_text("2번", items))
    assert delta["selected_item_index"] == 1
    assert sess.vision_item == "item-1"
    assert sess.state == SessionState.AWAITING_INTENT
    # No multi-select ack on a single index.
    _adapter.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_text_pick_multi_select_pre_search_ack_only(_adapter):
    """Updated flow: pre-search ack is just '2번 먼저 찾는 중 🐱' (status
    only, no question). The follow-up question ("4번도 이어서 보여줄까?")
    moves to the respond tool — after the cards land. pick_item's job is
    to process the first index and stash the rest in `pending_pick_indices`."""
    items = _items(4)
    sess = _session(items)
    set_store(_FakeStore(sess))

    delta = await pick_item(_state_text("2,4번", items))
    # First parsed index (1) is the active selection.
    assert delta["selected_item_index"] == 1
    assert sess.vision_item == "item-1"
    # Pre-search status ack went out (short, no question mark).
    _adapter.send_text.assert_awaited_once()
    sent_text = _adapter.send_text.await_args[0][1]
    assert "2번" in sent_text and "찾는 중" in sent_text
    assert "?" not in sent_text  # question moved to after-cards follow-up
    # Remaining indices stashed for respond to pick up after card delivery.
    assert sess.pending_pick_indices == [3]


@pytest.mark.asyncio
async def test_text_pick_single_does_not_stash_pending(_adapter):
    """Single-index reply leaves pending_pick_indices empty so respond
    doesn't ask a follow-up that doesn't apply."""
    items = _items(4)
    sess = _session(items)
    set_store(_FakeStore(sess))

    delta = await pick_item(_state_text("2번", items))
    assert delta["selected_item_index"] == 1
    assert sess.pending_pick_indices == []


@pytest.mark.asyncio
async def test_text_pick_no_match_falls_through_to_picker(_adapter):
    items = _items(4)
    sess = _session(items)
    set_store(_FakeStore(sess))

    delta = await pick_item(_state_text("음 모르겠어", items))
    # No selection — fallthrough re-sends the picker.
    assert "selected_item_index" not in delta
    _adapter.send_text_with_keyboard.assert_awaited_once()
