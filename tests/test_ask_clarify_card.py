"""SPEC-CLARIFY-CARDS-001 — ask_clarify 카드 vs 텍스트 분기 테스트.

REQ-CLARIFY-CARD-001/002/003, REQ-CLARIFY-COMPAT-001/002.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.channels.session import InMemorySessionStore, SessionState, set_store, shutdown_store
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.nodes.ask_clarify import ask_clarify
from app.graphs.state import WorkingState
from tests.conftest_graph import FakeAdapter, make_msg


@pytest.fixture
async def store():
    s = InMemorySessionStore()
    set_store(s)
    yield s
    await shutdown_store()


@pytest.fixture
def adapter():
    a = FakeAdapter()
    token = set_adapter(a)
    yield a
    reset_adapter(token)


def _vision(subcategory="", fit="", detected_gender=""):
    item = SimpleNamespace(subcategory=subcategory, fit=fit)
    style = SimpleNamespace(detectedGender=detected_gender, fit=fit, aesthetic="")
    mood = SimpleNamespace(occasion=[])
    return SimpleNamespace(items=[item], style=style, mood=mood)


@pytest.mark.asyncio
async def test_card_path_when_axis_picked(store, adapter):
    """REQ-CLARIFY-CARD-001 — pick_clarify_axis non-None → 인라인 키보드 발행."""
    msg = make_msg()
    state = WorkingState(message=msg, chat_id=42, vision_result=_vision(subcategory="", detected_gender=""))
    out = await ask_clarify(state)

    assert adapter.buttons, "card 경로는 send_text_with_buttons 발행"
    assert not adapter.texts, "card 경로는 send_text 발행 금지"
    chat_id, body, btns = adapter.buttons[0]
    assert chat_id == 42
    assert 0 < len(body) <= 60, f"본문 60자 이하: {body}"
    assert 3 <= len(btns) <= 5, f"버튼 개수 [3, 5]: {len(btns)}"
    # 마지막 = skip (REQ-CLARIFY-CARD-002).
    assert btns[-1][1].endswith(":skip")
    # 모든 callback_data 가 clarify: 로 시작.
    for _, cb in btns:
        assert cb.startswith("clarify:")

    sess = store.get_or_create(42)
    assert sess.state == SessionState.AWAITING_CLARIFY
    assert out.get("clarify_axis") is not None


@pytest.mark.asyncio
async def test_card_skipped_when_flag_off(store, adapter, monkeypatch):
    """REQ-CLARIFY-COMPAT-002 — CLARIFY_CARDS_ENABLED=false → 텍스트 폴백."""
    monkeypatch.setattr("app.graphs.nodes.ask_clarify.settings.CLARIFY_CARDS_ENABLED", False)

    import app.graphs.nodes.ask_clarify as ac

    class _Boom:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("stub")

    monkeypatch.setattr(ac, "_llm", _Boom())

    msg = make_msg()
    state = WorkingState(message=msg, chat_id=42, vision_result=_vision(subcategory="", detected_gender=""))
    await ask_clarify(state)

    assert adapter.texts, "flag off 시 자유 텍스트 폴백"
    assert not adapter.buttons


@pytest.mark.asyncio
async def test_card_falls_back_to_text_when_axis_none(store, adapter, monkeypatch):
    """REQ-CLARIFY-COMPAT-001 — pick_clarify_axis=None → 텍스트 폴백."""
    import app.graphs.nodes.ask_clarify as ac

    class _Boom:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("stub")

    monkeypatch.setattr(ac, "_llm", _Boom())

    msg = make_msg()
    # vision_result=None → pick_clarify_axis 가 None 반환.
    state = WorkingState(message=msg, chat_id=42, vision_result=None)
    await ask_clarify(state)

    assert adapter.texts, "axis None 시 텍스트 폴백"
    assert not adapter.buttons


@pytest.mark.asyncio
async def test_card_max_buttons_respected(store, adapter, monkeypatch):
    """settings.CLARIFY_MAX_BUTTONS 가 상한으로 작동."""
    monkeypatch.setattr("app.graphs.nodes.ask_clarify.settings.CLARIFY_MAX_BUTTONS", 4)

    msg = make_msg()
    state = WorkingState(message=msg, chat_id=42, vision_result=_vision(subcategory="", detected_gender=""))
    await ask_clarify(state)

    assert adapter.buttons
    _, _, btns = adapter.buttons[0]
    assert len(btns) <= 4
