"""앱이 SSE 도중 연결을 끊어도 봇 답이 chat_messages 에 남는다 (2026-09-26).

9월 실유저 로그에서 검색(ai.searches)은 있는데 assistant 메시지가 없는 턴이 5개
나왔다. 봇 답 저장이 SSE 제너레이터 끝(이벤트를 다 흘려보낸 뒤)에 있어서, 앱이
연결을 끊으면(앱 종료·새 메시지) 제너레이터가 닫혀 저장 코드가 안 돌았다.
검색 저장은 그래프 task 안이라 살아남았다.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from app.services import chat_service
from app.services.chat_service import AppCapStatus

_CAP = AppCapStatus(
    user_tier="free",
    cap_tier="free",
    daily_cap=100,
    cap_used=0,
    cap_remaining=100,
    cap_reset_at="2026-09-27T00:00:00+00:00",
    cap_reached=False,
)


def _wire(monkeypatch) -> tuple[list[tuple[Any, ...]], asyncio.Event]:
    saved: list[tuple[Any, ...]] = []
    graph_done = asyncio.Event()
    adapters: list[chat_service.StreamingAdapter] = []

    async def fake_append(pool, session_id, role, content, product_refs=None, search_id=None):
        saved.append((role, content, search_id))

    async def noop(*_a, **_k):
        return None

    async def fake_session(pool, user_id, session_id):
        return session_id or uuid4()

    async def fake_cap(pool, user_id):
        return _CAP

    async def fake_persist(*_a, **_k):
        return ("search-1", 12)

    def fake_set_adapter(adapter):
        adapters.append(adapter)
        return None

    class FakeGraph:
        async def ainvoke(self, _state, config=None):
            adapter = adapters[-1]
            await adapter.send_text(1, "베이지 카고팬츠 몇 개 골라봤어")
            # 사진 턴처럼 오래 걸리는 동안 앱이 연결을 끊는다.
            await asyncio.sleep(0.05)
            graph_done.set()
            return {}

    monkeypatch.setattr(chat_service, "append_message", fake_append)
    monkeypatch.setattr(chat_service, "get_or_create_session", fake_session)
    monkeypatch.setattr(chat_service, "get_app_cap_status", fake_cap)
    monkeypatch.setattr(chat_service, "_sync_gender_to_taste_profile", noop)
    monkeypatch.setattr(chat_service, "_prime_feature_scores", noop)
    monkeypatch.setattr(chat_service, "set_session_title", noop)
    monkeypatch.setattr(chat_service, "_persist_search", fake_persist)
    monkeypatch.setattr(chat_service, "set_adapter", fake_set_adapter)
    monkeypatch.setattr(chat_service, "reset_adapter", lambda _t: None)
    monkeypatch.setattr(chat_service, "_bind_chat_trace", lambda *a, **k: [])
    monkeypatch.setattr(chat_service, "_reset_app_turn", lambda *a, **k: "turn")
    monkeypatch.setattr(chat_service, "GRAPH", FakeGraph())
    return saved, graph_done


@pytest.mark.asyncio
async def test_assistant_reply_saved_when_client_disconnects_mid_stream(monkeypatch):
    saved, graph_done = _wire(monkeypatch)
    gen = chat_service.invoke_streaming(uuid4(), "이 사진이랑 비슷한 거 찾아줘", pool=None, session_id=uuid4())

    assert (await gen.__anext__())[0] == "session"
    assert (await gen.__anext__())[0] == "text_delta"
    await gen.aclose()  # 앱이 연결을 끊음 → StreamingResponse 가 제너레이터를 닫는다

    await asyncio.wait_for(graph_done.wait(), timeout=1)
    await asyncio.sleep(0.05)  # 그래프 task 의 마무리(검색·답 저장)

    assert ("user", "이 사진이랑 비슷한 거 찾아줘", None) in saved
    assert ("assistant", "베이지 카고팬츠 몇 개 골라봤어", "search-1") in saved


@pytest.mark.asyncio
async def test_full_stream_saves_reply_exactly_once(monkeypatch):
    saved, _ = _wire(monkeypatch)
    events = [e async for e in chat_service.invoke_streaming(uuid4(), "자라 아우터", pool=None, session_id=uuid4())]

    assert [e[0] for e in events][-2:] == ["search", "done"]
    assistant_rows = [r for r in saved if r[0] == "assistant"]
    assert assistant_rows == [("assistant", "베이지 카고팬츠 몇 개 골라봤어", "search-1")]


@pytest.mark.asyncio
async def test_callback_reply_saved_when_client_disconnects(monkeypatch):
    saved, graph_done = _wire(monkeypatch)
    gen = chat_service.invoke_streaming_callback(uuid4(), uuid4(), "clarify:category_pick:하의", pool=None)

    assert (await gen.__anext__())[0] == "session"
    assert (await gen.__anext__())[0] == "text_delta"
    await gen.aclose()

    await asyncio.wait_for(graph_done.wait(), timeout=1)
    await asyncio.sleep(0.05)

    assert ("assistant", "베이지 카고팬츠 몇 개 골라봤어", "search-1") in saved
