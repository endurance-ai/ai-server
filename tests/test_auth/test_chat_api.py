"""Chat API integration tests — LangGraph is mocked, DB is real (testcontainers)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.channels.schemas import BotReply
from app.core.social_auth.google import GoogleClaims


async def _login(client: AsyncClient) -> str:
    """Helper: log in as a Google user, return Authorization header value."""
    with patch("app.api.auth.verify_google_token", return_value=GoogleClaims(
        sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None
    )):
        resp = await client.post("/auth/social", json={"provider": "google", "id_token": "t"})
    return f"Bearer {resp.json()['access_token']}"


def _mock_reply(text: str = "Here are some picks!", cards: int = 2) -> BotReply:
    from app.channels.schemas import BotCard
    from pydantic import HttpUrl

    return BotReply(
        text=text,
        cards=[
            BotCard(
                image_url=HttpUrl("https://img.kiko.fashion/p1.jpg"),
                caption=f"Product {i}",
            )
            for i in range(cards)
        ],
    )


# ── POST /chat/sessions ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_returns_reply(client: AsyncClient):
    auth = await _login(client)

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(return_value=None)
        # CaptureAdapter collects what the "graph" sends — patch send_text via graph side-effect
        async def _fake_invoke(state, **_):
            from app.graphs.nodes._adapter_ctx import get_adapter
            adapter = get_adapter()
            if adapter:
                await adapter.send_text(state.chat_id, "Here are some picks!")
        mock_graph.ainvoke.side_effect = _fake_invoke

        resp = await client.post(
            "/chat/sessions",
            json={"message": "캐주얼 코디 추천해줘"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "session_id" in body
    assert body["reply_text"] == "Here are some picks!"
    # Validate session_id is a valid UUID
    UUID(body["session_id"])


@pytest.mark.asyncio
async def test_create_session_empty_message_rejected(client: AsyncClient):
    auth = await _login(client)
    resp = await client.post("/chat/sessions", json={"message": "  "}, headers={"Authorization": auth})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_requires_auth(client: AsyncClient):
    resp = await client.post("/chat/sessions", json={"message": "hi"})
    assert resp.status_code in (401, 403)  # HTTPBearer returns 403 on missing header


# ── GET /chat/sessions ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_empty_for_new_user(client: AsyncClient):
    auth = await _login(client)
    resp = await client.get("/chat/sessions", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_sessions_returns_created_sessions(client: AsyncClient):
    auth = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        await client.post("/chat/sessions", json={"message": "msg 1"}, headers={"Authorization": auth})
        await client.post("/chat/sessions", json={"message": "msg 2"}, headers={"Authorization": auth})

    resp = await client.get("/chat/sessions", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ── GET /chat/sessions/{id}/messages ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_messages_for_own_session(client: AsyncClient):
    auth = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        create_resp = await client.post(
            "/chat/sessions", json={"message": "안녕"}, headers={"Authorization": auth}
        )

    session_id = create_resp.json()["session_id"]
    resp = await client.get(f"/chat/sessions/{session_id}/messages", headers={"Authorization": auth})

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
        create_resp = await client.post(
            "/chat/sessions", json={"message": "hi"}, headers={"Authorization": auth1}
        )

    session_id = create_resp.json()["session_id"]
    resp = await client.get(f"/chat/sessions/{session_id}/messages", headers={"Authorization": auth2})
    assert resp.status_code == 404


# ── POST /chat/sessions/{id}/messages ────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_message_returns_reply(client: AsyncClient):
    auth = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        create_resp = await client.post(
            "/chat/sessions", json={"message": "첫 번째 메시지"}, headers={"Authorization": auth}
        )
        session_id = create_resp.json()["session_id"]

        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"message": "두 번째 메시지"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert "reply_text" in body
    assert "products" in body


@pytest.mark.asyncio
async def test_append_message_to_nonexistent_session_returns_404(client: AsyncClient):
    auth = await _login(client)
    fake_id = "00000000-0000-0000-0000-000000000001"

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        resp = await client.post(
            f"/chat/sessions/{fake_id}/messages",
            json={"message": "hi"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 404
