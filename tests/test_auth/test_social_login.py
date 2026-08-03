"""Auth API integration tests — provider calls are mocked, DB is real (testcontainers)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core.social_auth.apple import AppleClaims
from app.core.social_auth.google import GoogleClaims

# ── helpers ───────────────────────────────────────────────────────────────────


def _google_claims(sub: str = "google-sub-123") -> GoogleClaims:
    return GoogleClaims(sub=sub, email="user@example.com", name="Test User", picture=None)


def _apple_claims(sub: str = "apple-sub-456") -> AppleClaims:
    return AppleClaims(sub=sub, email="user@apple.com")


# ── /auth/social ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_google_login_returns_tokens(client: AsyncClient):
    with patch("app.api.auth.verify_google_token", return_value=_google_claims()):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "fake"})

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert "user_id" in body
    assert body["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_apple_login_returns_tokens(client: AsyncClient):
    with patch("app.api.auth.verify_apple_token", return_value=_apple_claims()):
        resp = await client.post("/v1/auth/social", json={"provider": "apple", "id_token": "fake"})

    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_same_user_gets_same_user_id(client: AsyncClient):
    """Second login with same provider_id must return the same user_id (upsert)."""
    with patch("app.api.auth.verify_google_token", return_value=_google_claims("same-sub")):
        r1 = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "a"})
        r2 = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "b"})

    assert r1.json()["user_id"] == r2.json()["user_id"]


@pytest.mark.asyncio
async def test_new_signup_fires_discord_notification(client: AsyncClient):
    """First login for a provider_id is a new signup → notify once with the total."""
    with (
        patch("app.api.auth.verify_google_token", return_value=_google_claims("notify-new-sub")),
        patch("app.api.auth.discord_notify.notify_signup", new=AsyncMock()) as notify,
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "a"})
        await asyncio.sleep(0.05)  # let the fire-and-forget task run

    assert resp.status_code == 200
    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert kwargs["provider"] == "google"
    assert kwargs["total_users"] >= 1


@pytest.mark.asyncio
async def test_returning_login_does_not_notify(client: AsyncClient):
    """Second login with the same provider_id is an update, not a signup → no notify."""
    with (
        patch("app.api.auth.verify_google_token", return_value=_google_claims("notify-returning-sub")),
        patch("app.api.auth.discord_notify.notify_signup", new=AsyncMock()) as notify,
    ):
        await client.post("/v1/auth/social", json={"provider": "google", "id_token": "a"})
        await asyncio.sleep(0.05)
        notify.reset_mock()
        await client.post("/v1/auth/social", json={"provider": "google", "id_token": "b"})
        await asyncio.sleep(0.05)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_provider_returns_400(client: AsyncClient):
    resp = await client.post("/v1/auth/social", json={"provider": "twitter", "id_token": "fake"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_invalid_google_token_returns_401(client: AsyncClient):
    from fastapi import HTTPException

    with patch("app.api.auth.verify_google_token", side_effect=HTTPException(status_code=401)):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "bad"})

    assert resp.status_code == 401


# ── /auth/refresh ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_issues_new_access_token(client: AsyncClient):
    with patch("app.api.auth.verify_google_token", return_value=_google_claims()):
        login = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})

    refresh_token = login.json()["refresh_token"]
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})

    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_invalid_refresh_token_returns_401(client: AsyncClient):
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_expired_refresh_token_returns_401(client: AsyncClient, pool):
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    past = datetime.now(UTC) - timedelta(seconds=1)

    async with pool.connection() as conn, conn.cursor() as cur:
        # Insert a user first (foreign key constraint)
        await cur.execute(
            """
            INSERT INTO ai.user_profiles (provider, provider_id, email)
            VALUES ('google', 'expired-test-sub', 'expired@test.com')
            RETURNING user_id
            """
        )
        user_id = (await cur.fetchone())[0]
        await cur.execute(
            "INSERT INTO ai.refresh_tokens (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (user_id, token_hash, past),
        )
        await conn.commit()

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": raw})
    assert resp.status_code == 401


# ── /auth/revoke ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_then_refresh_returns_401(client: AsyncClient):
    with patch("app.api.auth.verify_google_token", return_value=_google_claims()):
        login = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})

    refresh_token = login.json()["refresh_token"]

    revoke = await client.post("/v1/auth/logout", json={"refresh_token": refresh_token})
    assert revoke.status_code == 204

    retry = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert retry.status_code == 401


@pytest.mark.asyncio
async def test_logout_deactivates_only_the_current_device(client: AsyncClient, pool):
    with patch("app.api.auth.verify_google_token", return_value=_google_claims("logout-device-sub")):
        login = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})

    body = login.json()
    auth = {"Authorization": f"Bearer {body['access_token']}"}
    first = await client.post(
        "/v1/devices",
        headers=auth,
        json={"push_token": "tok-logout-current", "provider": "apns", "environment": "production"},
    )
    second = await client.post(
        "/v1/devices",
        headers=auth,
        json={"push_token": "tok-logout-other", "provider": "apns", "environment": "production"},
    )

    logout = await client.post(
        "/v1/auth/logout",
        json={"refresh_token": body["refresh_token"], "device_id": first.json()["device_id"]},
    )
    assert logout.status_code == 204

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT device_id::text, status FROM ai.devices WHERE device_id IN (%s, %s) ORDER BY device_id",
            (first.json()["device_id"], second.json()["device_id"]),
        )
        statuses = dict(await cur.fetchall())

    assert statuses[first.json()["device_id"]] == "inactive"
    assert statuses[second.json()["device_id"]] == "active"
