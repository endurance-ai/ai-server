"""Chat API integration tests — LangGraph is mocked, DB is real (testcontainers)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.core.social_auth.google import GoogleClaims


async def _login(client: AsyncClient) -> str:
    """Helper: log in as a Google user, return Authorization header value."""
    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})
    return f"Bearer {resp.json()['access_token']}"


def _parse_sse(text: str) -> dict[str, object]:
    """Parse SSE stream into {event_type: last_payload}.

    text_delta events are accumulated into events["_text"] (str).
    All other event types store the last seen payload dict.
    """
    events: dict[str, object] = {}
    accumulated_text = ""
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type: str | None = None
        data: dict | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event_type and data is not None:
            if event_type == "text_delta":
                accumulated_text += data.get("delta", "")
            else:
                events[event_type] = data
    events["_text"] = accumulated_text
    return events


# ── POST /v1/chat/sessions ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_returns_reply(client: AsyncClient):
    auth = await _login(client)
    turn_contexts: list[dict] = []

    with patch("app.services.chat_service.GRAPH") as mock_graph:

        async def _fake_invoke(state, **_):
            from app.graphs.nodes._adapter_ctx import get_adapter
            from app.observability.turn_cost import get_turn_totals

            turn_contexts.append(get_turn_totals())
            adapter = get_adapter()
            if adapter:
                await adapter.send_text(state.chat_id, "Here are some picks!")

        mock_graph.ainvoke = AsyncMock(side_effect=_fake_invoke)

        resp = await client.post(
            "/v1/chat/sessions",
            json={"message": "캐주얼 코디 추천해줘"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert "session" in events
    UUID(events["session"]["session_id"])  # valid UUID
    assert events["session"]["user_tier"] == "free"
    assert isinstance(events["session"]["daily_cap"], int)
    assert isinstance(events["session"]["cap_used"], int)
    assert "cap_remaining" in events["session"]
    assert "cap_reset_at" in events["session"]
    assert "cap_reset_at_kst" in events["session"]
    assert "cap_reset_display" in events["session"]
    assert turn_contexts
    assert turn_contexts[0]["turn_id"] == f"{turn_contexts[0]['thread_id']}:0"
    assert turn_contexts[0]["chat_id"] is not None
    assert events["_text"] == "Here are some picks!"
    assert "done" in events


@pytest.mark.asyncio
async def test_create_session_cap_reached_emits_banner_event_without_graph_or_messages(
    client: AsyncClient,
    monkeypatch,
):
    from app.services.chat_service import AppCapStatus

    auth = await _login(client)

    async def _cap_reached(*_args, **_kwargs):
        return AppCapStatus(
            user_tier="free",
            cap_tier="free",
            daily_cap=500_000,
            cap_used=500_000,
            cap_remaining=0,
            cap_reset_at="2026-06-28T15:00:00+00:00",
            cap_reached=True,
        )

    monkeypatch.setattr("app.services.chat_service.get_app_cap_status", _cap_reached)

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock()
        resp = await client.post(
            "/v1/chat/sessions",
            json={"message": "더 추천해줘"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events["session"]["user_tier"] == "free"
    assert events["session"]["cap_remaining"] == 0
    assert events["session"]["cap_reset_at"] == "2026-06-28T15:00:00+00:00"
    assert events["session"]["cap_reset_at_kst"] == "2026-06-29T00:00:00+09:00"
    assert events["session"]["cap_reset_display"] == "2026-06-29 00:00 KST"
    assert events["cap_reached"] == {
        "code": "daily_token_cap_reached",
        "user_tier": "free",
        "used": 500_000,
        "cap": 500_000,
        "remaining": 0,
        "reset_at": "2026-06-28T15:00:00+00:00",
        "reset_at_kst": "2026-06-29T00:00:00+09:00",
        "reset_display": "2026-06-29 00:00 KST",
        "cta": "upgrade",
    }
    assert "done" in events
    mock_graph.ainvoke.assert_not_called()

    session_id = events["session"]["session_id"]
    history = await client.get(f"/v1/chat/sessions/{session_id}/messages", headers={"Authorization": auth})
    assert history.status_code == 200
    assert history.json()["messages"] == []


@pytest.mark.asyncio
async def test_create_session_empty_message_rejected(client: AsyncClient):
    auth = await _login(client)
    resp = await client.post("/v1/chat/sessions", json={"message": "  "}, headers={"Authorization": auth})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/chat/sessions", json={"message": "hi"})
    assert resp.status_code in (401, 403)  # HTTPBearer returns 403 on missing header


# ── GET /v1/chat/sessions ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_empty_for_new_user(client: AsyncClient):
    auth = await _login(client)
    resp = await client.get("/v1/chat/sessions", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_sessions_returns_created_sessions(client: AsyncClient):
    auth = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        # consume SSE streams fully so sessions are persisted before listing
        r1 = await client.post("/v1/chat/sessions", json={"message": "msg 1"}, headers={"Authorization": auth})
        r2 = await client.post("/v1/chat/sessions", json={"message": "msg 2"}, headers={"Authorization": auth})
    _ = r1.text, r2.text  # ensure streams are fully read

    resp = await client.get("/v1/chat/sessions", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ── GET /v1/chat/sessions/{id}/messages ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_messages_for_own_session(client: AsyncClient):
    auth = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        create_resp = await client.post("/v1/chat/sessions", json={"message": "안녕"}, headers={"Authorization": auth})

    events = _parse_sse(create_resp.text)
    session_id = events["session"]["session_id"]
    resp = await client.get(f"/v1/chat/sessions/{session_id}/messages", headers={"Authorization": auth})

    assert resp.status_code == 200
    messages = resp.json()["messages"]
    # At minimum: user message persisted
    assert any(m["role"] == "user" for m in messages)


@pytest.mark.asyncio
async def test_get_messages_other_user_session_returns_404(client: AsyncClient):
    auth1 = await _login(client)
    auth2 = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        create_resp = await client.post("/v1/chat/sessions", json={"message": "hi"}, headers={"Authorization": auth1})

    events = _parse_sse(create_resp.text)
    session_id = events["session"]["session_id"]
    resp = await client.get(f"/v1/chat/sessions/{session_id}/messages", headers={"Authorization": auth2})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_product_id_flows_to_sse_and_history(client: AsyncClient):
    """ProductRef.product_id must surface in the SSE `product` event and persist
    into message-history product_refs so the client can deep-link to the PDP."""
    auth = await _login(client)

    async def _send_card(state, **_):
        from app.channels.schemas import BotCard
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
        await adapter.send_text(state.chat_id, "여기 추천이에요!")
        await adapter.send_card(
            state.chat_id,
            BotCard(image_url="https://example.com/p.jpg", caption="오버핏 니트", product_id=12345),
        )

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_send_card)
        create_resp = await client.post(
            "/v1/chat/sessions", json={"message": "니트 추천"}, headers={"Authorization": auth}
        )

    events = _parse_sse(create_resp.text)
    assert events["product"]["product_id"] == 12345
    assert events["product"]["image_url"] == "https://example.com/p.jpg"

    session_id = events["session"]["session_id"]
    resp = await client.get(f"/v1/chat/sessions/{session_id}/messages", headers={"Authorization": auth})
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["product_refs"][0]["product_id"] == 12345


# ── POST /v1/chat/sessions/{id}/messages ────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_message_returns_reply(client: AsyncClient):
    auth = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        create_resp = await client.post(
            "/v1/chat/sessions", json={"message": "첫 번째 메시지"}, headers={"Authorization": auth}
        )
        session_id = _parse_sse(create_resp.text)["session"]["session_id"]

        resp = await client.post(
            f"/v1/chat/sessions/{session_id}/messages",
            json={"message": "두 번째 메시지"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert events["session"]["session_id"] == session_id
    assert "done" in events


@pytest.mark.asyncio
async def test_append_message_to_nonexistent_session_returns_404(client: AsyncClient):
    auth = await _login(client)
    fake_id = "00000000-0000-0000-0000-000000000001"

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        resp = await client.post(
            f"/v1/chat/sessions/{fake_id}/messages",
            json={"message": "hi"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 404


# ── searches 영속화 (결과셋) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_persisted_and_search_event_emitted(client: AsyncClient, pool):
    """검색 결과셋이 ai.searches 에 저장되고 SSE 'search' 이벤트로 search_id 가 나온다."""
    from types import SimpleNamespace

    auth = await _login(client)

    async def _fake_invoke(state, **_):
        # 검색 툴이 하듯 세션에 결과셋을 채운다 (Candidate-like: id/image_url)
        from app.infrastructure.memory.session import get_store

        sess = get_store().get_or_create(state.chat_id)
        sess.last_results = [
            SimpleNamespace(id=101, image_url="https://img.test/1.jpg"),
            SimpleNamespace(id=102, image_url="https://img.test/2.jpg"),
        ]

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_fake_invoke)
        resp = await client.post(
            "/v1/chat/sessions",
            json={"message": "코트 추천해줘"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert "search" in events
    search_id = events["search"]["search_id"]
    UUID(search_id)  # valid UUID

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT product_ids, total, is_listed FROM ai.searches WHERE search_id = %s",
            (search_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[1] == 2  # total
    assert row[2] is False  # is_listed default
    assert 101 in row[0]


@pytest.mark.asyncio
async def test_no_search_event_when_no_results(client: AsyncClient):
    """결과가 없으면 search 이벤트도 ai.searches 행도 없다."""
    auth = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        resp = await client.post(
            "/v1/chat/sessions",
            json={"message": "hi"},
            headers={"Authorization": auth},
        )

    events = _parse_sse(resp.text)
    assert "search" not in events
    assert "done" in events
