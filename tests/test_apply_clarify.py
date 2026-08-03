"""SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-CALLBACK-002 — apply_clarify 노드 단위 테스트."""

from __future__ import annotations

import pytest

from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.nodes.apply_clarify import apply_clarify
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import InMemorySessionStore, SessionState, set_store, shutdown_store
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


def _state(callback_data: str, *, vision_result=None) -> WorkingState:
    msg = make_msg(callback_data=callback_data)
    return WorkingState(message=msg, chat_id=42, vision_result=vision_result)


@pytest.mark.asyncio
async def test_valid_callback_sets_critique_delta_and_session_boost(store, adapter):
    out = await apply_clarify(_state("clarify:fit:oversize"))

    assert "critique_delta" in out
    cd = out["critique_delta"]
    assert "oversized" in cd.boost_keywords

    sess = store.get_or_create(42)
    assert sess.state == SessionState.SEARCHING
    assert "oversized" in sess.boost_keywords  # sticky 누적 (R6)
    assert sess.clarify_axis == "fit"
    assert sess.clarify_value == "oversize"


@pytest.mark.asyncio
async def test_skip_callback_no_boost_no_delta(store, adapter):
    out = await apply_clarify(_state("clarify:formality:skip"))
    assert out.get("critique_delta") is None  # skip → 검색은 그대로
    sess = store.get_or_create(42)
    assert sess.state == SessionState.SEARCHING
    assert sess.boost_keywords == []  # skip 은 누적 없음
    assert sess.clarify_value == "skip"


@pytest.mark.asyncio
async def test_stale_callback_toast_then_no_state_change(store, adapter):
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_CLARIFY  # 카드 발행 후 stale 상태
    store.update(sess)

    out = await apply_clarify(_state("clarify:bogus_axis:foo"))
    # toast 발사됨.
    assert any(text and "Out of date" in text for _, text in adapter.callback_answers)
    # critique_delta 미설정 — 검색 진입하지 않음(라우팅에서 skip 가능).
    assert out.get("critique_delta") is None
    # 세션 상태는 그대로.
    assert store.get_or_create(42).state == SessionState.AWAITING_CLARIFY


@pytest.mark.asyncio
async def test_sticky_boost_keywords_accumulate(store, adapter):
    """동일 세션에서 두 번의 clarify 결과는 sticky 하게 누적."""
    sess = store.get_or_create(42)
    sess.boost_keywords = ["semi-formal"]  # 이전 clarify 결과
    store.update(sess)

    await apply_clarify(_state("clarify:fit:oversize"))
    sess = store.get_or_create(42)
    assert "semi-formal" in sess.boost_keywords
    assert "oversized" in sess.boost_keywords


@pytest.mark.asyncio
async def test_subcategory_override_applied_to_session_legacy_field(store, adapter):
    out = await apply_clarify(_state("clarify:category_pick:top"))
    sess = store.get_or_create(42)
    assert sess.vision_item == "top"
    assert out["critique_delta"] is not None


@pytest.mark.asyncio
async def test_toast_sent_for_known_axis(store, adapter):
    await apply_clarify(_state("clarify:formality:semi_formal"))
    # toast 본문은 한국어. "세미포멀" 라벨이 들어가야 함.
    assert adapter.callback_answers
    _, text = adapter.callback_answers[0]
    assert text is not None
    assert "세미포멀" in text
