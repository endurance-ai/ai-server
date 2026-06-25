"""GET/PATCH/DELETE /v1/me integration tests."""

from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.social_auth.google import GoogleClaims


async def _login(client: AsyncClient, sub: str | None = None) -> tuple[str, str]:
    """Login via Google mock. Returns (auth_header, user_id)."""
    sub = sub or f"sub-{uuid4()}"
    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=sub, email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/auth/social", json={"provider": "google", "id_token": "t"})
    data = resp.json()
    return f"Bearer {data['access_token']}", data["user_id"]


# ── GET /v1/me ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_me_returns_profile(client: AsyncClient):
    auth, user_id = await _login(client)
    resp = await client.get("/v1/me", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == user_id
    assert data["provider"] == "google"
    assert data["email"] == "u@test.com"
    assert data["display_name"] == "User"
    assert data["tier"] == "free"
    assert data["gender"] is None


@pytest.mark.asyncio
async def test_get_me_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/me")
    assert resp.status_code in (401, 403)


# ── PATCH /v1/me ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_me_updates_display_name(client: AsyncClient):
    auth, user_id = await _login(client)
    resp = await client.patch(
        "/v1/me", json={"display_name": "홍길동"}, headers={"Authorization": auth}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == user_id
    assert data["display_name"] == "홍길동"

    # verify persisted
    me = await client.get("/v1/me", headers={"Authorization": auth})
    assert me.json()["display_name"] == "홍길동"


@pytest.mark.asyncio
async def test_patch_me_updates_gender(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.patch(
        "/v1/me", json={"gender": "female"}, headers={"Authorization": auth}
    )
    assert resp.status_code == 200
    assert resp.json()["gender"] == "female"


@pytest.mark.asyncio
async def test_patch_me_updates_both_fields(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.patch(
        "/v1/me",
        json={"display_name": "김영희", "gender": "other"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "김영희"
    assert data["gender"] == "other"


@pytest.mark.asyncio
async def test_patch_me_empty_body_rejected(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.patch("/v1/me", json={}, headers={"Authorization": auth})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_me_invalid_gender_rejected(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.patch(
        "/v1/me", json={"gender": "unknown"}, headers={"Authorization": auth}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_me_requires_auth(client: AsyncClient):
    resp = await client.patch("/v1/me", json={"display_name": "x"})
    assert resp.status_code in (401, 403)


# ── DELETE /v1/me ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_me_succeeds_with_confirm(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.request(
        "DELETE", "/v1/me", json={"confirm": True}, headers={"Authorization": auth}
    )
    assert resp.status_code == 204

    # token is still technically valid but the user row is gone
    me = await client.get("/v1/me", headers={"Authorization": auth})
    assert me.status_code == 404


@pytest.mark.asyncio
async def test_delete_me_without_confirm_rejected(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.request(
        "DELETE", "/v1/me", json={"confirm": False}, headers={"Authorization": auth}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_me_cascades_chat_sessions(client: AsyncClient):
    """Deleting user removes their chat sessions (ON DELETE CASCADE)."""
    from unittest.mock import AsyncMock

    auth, user_id = await _login(client)

    async def _noop(state, **_):
        pass

    with patch("app.services.chat_service.GRAPH") as mock_graph:
        mock_graph.ainvoke = AsyncMock(side_effect=_noop)
        create = await client.post(
            "/chat/sessions", json={"message": "hi"}, headers={"Authorization": auth}
        )
    assert create.status_code == 200

    # delete user
    await client.request("DELETE", "/v1/me", json={"confirm": True}, headers={"Authorization": auth})

    # new user logging in with different sub shouldn't see deleted user's sessions
    auth2, _ = await _login(client, sub=f"sub-new-{uuid4()}")
    sessions = await client.get("/chat/sessions", headers={"Authorization": auth2})
    assert sessions.json() == []


@pytest.mark.asyncio
async def test_delete_me_requires_auth(client: AsyncClient):
    resp = await client.request("DELETE", "/v1/me", json={"confirm": True})
    assert resp.status_code in (401, 403)
