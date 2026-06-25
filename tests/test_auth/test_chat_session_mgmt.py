"""PATCH /chat/sessions/{id}, DELETE /chat/sessions/{id} integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient


async def _login(client: AsyncClient) -> str:
    from app.core.social_auth.google import GoogleClaims

    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=f"sub-{uuid4()}", email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})
    return f"Bearer {resp.json()['access_token']}"


async def _create_session(client: AsyncClient, auth: str) -> str:
    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        resp = await client.post("/v1/chat/sessions", json={"message": "hi"}, headers={"Authorization": auth})
    assert resp.status_code == 200
    sessions = await client.get("/v1/chat/sessions", headers={"Authorization": auth})
    return sessions.json()[0]["session_id"]


# ── PATCH /chat/sessions/{id} ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_session_succeeds(client: AsyncClient):
    auth = await _login(client)
    sid = await _create_session(client, auth)

    resp = await client.patch(f"/v1/chat/sessions/{sid}", json={"title": "새 제목"}, headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.json()["title"] == "새 제목"
    assert resp.json()["session_id"] == sid


@pytest.mark.asyncio
async def test_rename_session_persists(client: AsyncClient):
    auth = await _login(client)
    sid = await _create_session(client, auth)

    await client.patch(f"/v1/chat/sessions/{sid}", json={"title": "저장된 제목"}, headers={"Authorization": auth})
    sessions = await client.get("/v1/chat/sessions", headers={"Authorization": auth})
    titles = [s["title"] for s in sessions.json()]
    assert "저장된 제목" in titles


@pytest.mark.asyncio
async def test_rename_session_not_found(client: AsyncClient):
    auth = await _login(client)
    resp = await client.patch(f"/v1/chat/sessions/{uuid4()}", json={"title": "x"}, headers={"Authorization": auth})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_session_other_user_returns_404(client: AsyncClient):
    auth1 = await _login(client)
    auth2 = await _login(client)
    sid = await _create_session(client, auth1)

    resp = await client.patch(f"/v1/chat/sessions/{sid}", json={"title": "x"}, headers={"Authorization": auth2})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_session_requires_auth(client: AsyncClient):
    resp = await client.patch(f"/v1/chat/sessions/{uuid4()}", json={"title": "x"})
    assert resp.status_code in (401, 403)


# ── DELETE /chat/sessions/{id} ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_session_succeeds(client: AsyncClient):
    auth = await _login(client)
    sid = await _create_session(client, auth)

    resp = await client.request("DELETE", f"/v1/chat/sessions/{sid}", headers={"Authorization": auth})
    assert resp.status_code == 204

    sessions = await client.get("/v1/chat/sessions", headers={"Authorization": auth})
    assert all(s["session_id"] != sid for s in sessions.json())


@pytest.mark.asyncio
async def test_delete_session_cascades_messages(client: AsyncClient):
    auth = await _login(client)
    sid = await _create_session(client, auth)

    await client.request("DELETE", f"/v1/chat/sessions/{sid}", headers={"Authorization": auth})

    resp = await client.get(f"/v1/chat/sessions/{sid}/messages", headers={"Authorization": auth})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_not_found(client: AsyncClient):
    auth = await _login(client)
    resp = await client.request("DELETE", f"/v1/chat/sessions/{uuid4()}", headers={"Authorization": auth})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_other_user_returns_404(client: AsyncClient):
    auth1 = await _login(client)
    auth2 = await _login(client)
    sid = await _create_session(client, auth1)

    resp = await client.request("DELETE", f"/v1/chat/sessions/{sid}", headers={"Authorization": auth2})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_requires_auth(client: AsyncClient):
    resp = await client.request("DELETE", f"/v1/chat/sessions/{uuid4()}")
    assert resp.status_code in (401, 403)
