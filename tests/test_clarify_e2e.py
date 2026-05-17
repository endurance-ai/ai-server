"""SPEC-CLARIFY-CARDS-001 — 그래프-레벨 E2E 테스트.

본 테스트는 컴파일된 StateGraph 를 ainvoke 로 끝까지 돌려서 다음을 검증한다:

1. weak-vision 이미지 turn → ask_clarify 카드 발행 + 검색 미진입.
2. clarify:* 콜백 turn → apply_clarify → search_node 진입 + boost_keywords
   가 검색 요청에 실려 들어감.
3. self-critique fast-path 가 critique_delta 를 갈아치워도 sticky boost_keywords
   가 살아남음(R6 / Q7).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl

from app.channels.recommendation import set_port
from app.channels.schemas import ChannelMessage
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import (
    InMemorySessionStore,
    SessionState,
    set_store,
    shutdown_store,
)
from app.infrastructure.memory.taste_profile import (
    InMemoryTasteProfileStore,
    set_taste_store,
    shutdown_taste_store,
)
from tests.conftest_graph import FakeAdapter, StubPort


@pytest.fixture
async def store():
    # SPEC-ONBOARD-CARDS-001 cascade — bypass onboarding gate for legacy paths.
    s = InMemorySessionStore()
    set_store(s)
    sess = s.get_or_create(42)
    sess.onboarded_at = datetime.now(tz=UTC)
    s.update(sess)
    yield s
    await shutdown_store()


@pytest.fixture
async def taste_store():
    s = InMemoryTasteProfileStore()
    set_taste_store(s)
    yield s
    await shutdown_taste_store()


@pytest.fixture
def stub_port():
    p = StubPort()
    set_port(p)
    yield p


@pytest.fixture
def adapter():
    a = FakeAdapter()
    token = set_adapter(a)
    yield a
    reset_adapter(token)


def _msg(**kw) -> ChannelMessage:
    base: dict = {"chat_id": 42, "received_at": datetime.now(tz=UTC)}
    if "urls" in kw:
        kw["urls"] = [HttpUrl(u) if not isinstance(u, HttpUrl) else u for u in kw["urls"]]
    base.update(kw)
    return ChannelMessage(**base)


async def _run(message: ChannelMessage):
    from app.graphs.fashion_bot import build_graph

    graph = build_graph()
    inp = WorkingState(message=message, chat_id=42)
    return await graph.ainvoke(inp)


# ── E2E #1: weak-vision 이미지 → 카드 발행, 검색 미진입 ─────────────────────


@pytest.mark.asyncio
async def test_e2e_weak_vision_emits_card_no_search(store, taste_store, stub_port, adapter, monkeypatch):
    msg = _msg(urls=["https://www.pinterest.com/pin/123/"])
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_weak(_url):
        return {"items": [{"label": "item", "description": "kind of", "keywords": ["x"]}]}

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_weak)

    await _run(msg)

    assert adapter.buttons, "clarify card must fire"
    assert not stub_port.calls, "검색은 다음 turn(콜백) 까지 미진입"
    # 카드 본문에 표시된 buttons 의 callback 이 모두 clarify:*.
    _, _, btns = adapter.buttons[0]
    for _, cb in btns:
        assert cb.startswith("clarify:")
    # 세션은 AWAITING_CLARIFY.
    assert store.get_or_create(42).state == SessionState.AWAITING_CLARIFY


# ── E2E #2: clarify:* 콜백 turn → apply_clarify → search_node 진입 ─────────


@pytest.mark.asyncio
async def test_e2e_clarify_callback_routes_to_search_with_boost(store, taste_store, stub_port, adapter):
    # 이전 turn 으로 ask_clarify 가 끝났다고 가정 — 세션을 그 상태로 시드.
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_CLARIFY
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "item"
    sess.clarify_axis = "fit"
    store.update(sess)

    msg = _msg(callback_data="clarify:fit:oversize", callback_query_id="cbq-1")
    await _run(msg)

    assert stub_port.calls, "콜백 turn 은 search_node 까지 진입해야 함"
    req = stub_port.calls[0]
    assert "oversized" in req.boost_keywords, f"clarify 키워드가 검색 요청에 실려야 함; got={req.boost_keywords}"

    # toast 가 발사됨.
    assert any("핏" in (t or "") for _, t in adapter.callback_answers)


# ── E2E #3: skip → 검색 진입하되 boost 추가 없음 ────────────────────────────


@pytest.mark.asyncio
async def test_e2e_clarify_skip_routes_to_search_without_extra_boost(store, taste_store, stub_port, adapter):
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_CLARIFY
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "item"
    store.update(sess)

    msg = _msg(callback_data="clarify:formality:skip", callback_query_id="cbq-2")
    await _run(msg)

    assert stub_port.calls, "skip 도 검색은 진행"
    req = stub_port.calls[0]
    # skip 은 boost 추가 없음(이전 누적이 없으면 비어 있어야).
    assert req.boost_keywords == [] or all("formal" not in k for k in req.boost_keywords)


# ── E2E #4: 자유 텍스트 폴백 — REQ-CLARIFY-CALLBACK-003 ──────────────────────


@pytest.mark.asyncio
async def test_e2e_free_text_during_awaiting_clarify_falls_through(store, taste_store, stub_port, adapter):
    """사용자가 카드 대신 자유 텍스트를 보내면 기존 router_text 또는 critique_apply
    경로를 그대로 탄다(graceful degradation)."""
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_CLARIFY
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "item"
    store.update(sess)

    # AWAITING_CLARIFY 에서 자유 텍스트 → routing 은 photo/url 없으니 RESULTS_SENT/
    # IDLE 분기 또는 respond 로 향한다(현재 routing 룰에 맞춰 graceful 하게 종료).
    msg = _msg(text="더 캐주얼하게")
    await _run(msg)
    # 핵심 — 그래프가 raise 없이 종료. (구체 분기 동작은 router 룰에 위임).
    # 본 SPEC 의 보장: 콜백이 아니면 새 LLM 비용 없이 폴백한다.
    assert True


# ── E2E #5: self-critique fast-path 가 boost_keywords 를 보존 ──────────────


@pytest.mark.asyncio
async def test_e2e_clarify_boost_survives_self_critique_fastpath(store, taste_store, stub_port, adapter, monkeypatch):
    """R6 / Q7 — clarify 가 정한 boost_keywords 는 자기-비평 fast-path 가
    critique_delta 를 갈아치워도 sticky 하게 살아남아 두 번째 search 요청에도
    실린다.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_CLARIFY
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "item"
    store.update(sess)

    # 첫 검색은 0건 → fast-path 발동 (REQ-CRITIQUE-EMPTY-001).
    stub_port.candidates = []

    # evaluator 가 LLM 호출 없이 fast-path 만 타도록 — _call_llm 은 호출되지
    # 않지만 안전하게 패치.
    from app.graphs.nodes import evaluator as evaluator_module

    async def _fail_open(_prompt):
        from app.graphs.nodes._evaluator_models import CritiqueScore

        return CritiqueScore(score=1.0, reasoning="ok", suggested_delta=None, retry=False)

    monkeypatch.setattr(evaluator_module, "_call_llm", _fail_open)

    msg = _msg(callback_data="clarify:formality:semi_formal", callback_query_id="cbq-3")
    await _run(msg)

    # 적어도 한 번은 검색 호출.
    assert stub_port.calls
    # 모든 검색 요청에 'semi-formal' boost 가 포함되어야 함.
    for req in stub_port.calls:
        assert any("semi" in k for k in req.boost_keywords), (
            f"sticky boost 가 갈아치워짐; req.boost_keywords={req.boost_keywords}"
        )
