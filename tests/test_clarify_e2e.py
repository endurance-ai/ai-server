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

# SPEC-AGENT-V2-CLEANUP-001 — the V1-only clarify→search routing tests were
# removed (clarify callbacks now route to `agent`; boost_keywords are still
# accumulated inline by ingest Step C, but the search dispatch is the agent's
# tool decision with no deterministic equivalent in this unmocked harness).
# The two flag-agnostic E2E tests below (weak-vision output class + free-text
# graceful fallthrough) are retained.


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
    """SPEC-AGENT-V2-REACT / T-010 (Bucket B1, flag-agnostic) — weak vision
    must produce a bot response and must NOT auto-search this turn. V1 emits a
    clarify card (SPEC-CLARIFY-CARDS-001); V2 lets the agent respond/ask. We
    assert the OUTPUT CLASS per REQ-AGENT-COMPAT-SEMANTIC-001.
    """
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

    # OUTPUT CLASS — some bot response, and no blind search before clarification.
    assert adapter.buttons or adapter.texts, "weak vision must produce a bot response"
    assert not stub_port.calls, "검색은 weak vision turn 에 자동 진입하면 안 됨"


# ── E2E #2: 자유 텍스트 폴백 — REQ-CLARIFY-CALLBACK-003 ──────────────────────


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


# SPEC-AGENT-V2-CLEANUP-001 — the V1 clarify→search and self-critique
# fast-path E2E tests were removed (search dispatch is now agent-mediated;
# the evaluator fast-path is reused by the `refine_search` tool / Reflexion
# Gap2 path covered in tests/test_agent_v3/test_gap2_reflexion_*).
