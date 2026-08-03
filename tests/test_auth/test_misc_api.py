"""POST /v1/feedback, POST /v1/devices, PATCH /v1/me/notifications,
GET /v1/legal/versions, POST /v1/legal/consents integration tests.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.social_auth.google import GoogleClaims


async def _login(client: AsyncClient, sub: str | None = None) -> tuple[str, str]:
    sub = sub or f"sub-{uuid4()}"
    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=sub, email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})
    data = resp.json()
    return f"Bearer {data['access_token']}", data["user_id"]


# ── POST /v1/feedback ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_feedback_success(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/feedback",
        headers={"Authorization": auth},
        json={"rating": "positive", "reasons": ["mood_match"], "comment": "좋아요", "consent": True},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "feedback_id" in data
    assert data["exported_to_training"] is False


@pytest.mark.asyncio
async def test_submit_feedback_with_search_id(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/feedback",
        headers={"Authorization": auth},
        json={
            "search_id": str(uuid4()),
            "rating": "negative",
            "reasons": ["mood_off", "too_expensive"],
            "comment": "별로예요",
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_submit_feedback_invalid_rating(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/feedback",
        headers={"Authorization": auth},
        json={"rating": "neutral", "comment": "ok"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_chips_only_no_comment(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/feedback",
        headers={"Authorization": auth},
        json={"rating": "positive", "reasons": ["mood_match"]},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_submit_feedback_invalid_reason(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/feedback",
        headers={"Authorization": auth},
        json={"rating": "positive", "reasons": ["not_a_reason"], "comment": "ok"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/v1/feedback",
        json={"rating": "positive", "comment": "ok"},
    )
    assert resp.status_code in (401, 403)


# ── POST /v1/devices ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_device_success(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={"apns_token": "tok-abc", "platform": "ios", "app_version": "1.0.0", "device_model": "iPhone 16"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "device_id" in data
    assert "registered_at" in data
    assert data["provider"] == "apns"
    assert data["environment"] == "production"


@pytest.mark.asyncio
async def test_register_device_upsert(client: AsyncClient):
    auth, _ = await _login(client)
    payload = {"apns_token": "tok-dup", "app_version": "1.0.0"}
    r1 = await client.post("/v1/devices", headers={"Authorization": auth}, json=payload)
    r2 = await client.post("/v1/devices", headers={"Authorization": auth}, json={**payload, "app_version": "1.1.0"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    # upsert: same device_id
    assert r1.json()["device_id"] == r2.json()["device_id"]


@pytest.mark.asyncio
async def test_register_device_preferred_contract_and_environment_isolation(client: AsyncClient):
    auth, _ = await _login(client)
    payload = {"push_token": "tok-env", "provider": "apns", "platform": "ios"}

    development = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={**payload, "environment": "development"},
    )
    production = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={**payload, "environment": "production"},
    )

    assert development.status_code == 201
    assert production.status_code == 201
    assert development.json()["environment"] == "development"
    assert production.json()["environment"] == "production"
    assert development.json()["device_id"] != production.json()["device_id"]


@pytest.mark.asyncio
async def test_register_device_keeps_identity_when_apns_rotates_token(client: AsyncClient, pool):
    auth, _ = await _login(client)
    first = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={"push_token": "tok-before-rotation", "provider": "apns", "environment": "production"},
    )
    rotated = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={
            "device_id": first.json()["device_id"],
            "push_token": "tok-after-rotation",
            "provider": "apns",
            "environment": "production",
        },
    )

    assert rotated.status_code == 201
    assert rotated.json()["device_id"] == first.json()["device_id"]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT push_token FROM ai.devices WHERE device_id = %s", (first.json()["device_id"],))
        assert (await cur.fetchone())[0] == "tok-after-rotation"


@pytest.mark.asyncio
async def test_register_device_transfers_endpoint_to_latest_authenticated_user(client: AsyncClient, pool):
    first_auth, first_user_id = await _login(client)
    second_auth, second_user_id = await _login(client)
    payload = {
        "push_token": "tok-owner-transfer",
        "provider": "apns",
        "environment": "production",
        "platform": "ios",
    }

    first = await client.post("/v1/devices", headers={"Authorization": first_auth}, json=payload)
    second = await client.post("/v1/devices", headers={"Authorization": second_auth}, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["device_id"] == second.json()["device_id"]

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user_id::text, count(*) OVER ()
            FROM ai.devices
            WHERE provider = 'apns'
              AND environment = 'production'
              AND push_token = 'tok-owner-transfer'
            """
        )
        owner_id, endpoint_count = await cur.fetchone()

    assert owner_id == second_user_id
    assert owner_id != first_user_id
    assert endpoint_count == 1


@pytest.mark.asyncio
async def test_register_device_rejects_whitespace_token(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={"push_token": "bad token", "provider": "apns", "environment": "production"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_device_rejects_provider_platform_mismatch(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/devices",
        headers={"Authorization": auth},
        json={"push_token": "ios-fcm", "provider": "fcm", "platform": "ios"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_device_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/devices", json={"apns_token": "tok"})
    assert resp.status_code in (401, 403)


# ── PATCH /v1/me/notifications ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_notifications_partial(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.patch(
        "/v1/me/notifications",
        headers={"Authorization": auth},
        json={"categories": {"taste_push": False}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["categories"]["taste_push"] is False
    # unspecified keys should still be true (default)
    assert data["categories"]["release_alerts"] is True
    assert data["categories"]["system"] is True


@pytest.mark.asyncio
async def test_update_notifications_empty_categories(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.patch(
        "/v1/me/notifications",
        headers={"Authorization": auth},
        json={"categories": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    # no changes — defaults returned
    assert "categories" in data


@pytest.mark.asyncio
async def test_update_notifications_requires_auth(client: AsyncClient):
    resp = await client.patch("/v1/me/notifications", json={"categories": {"system": False}})
    assert resp.status_code in (401, 403)


# ── GET /v1/me/notifications ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_notifications_defaults(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.get("/v1/me/notifications", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    # New user with no saved settings → all categories opted in by default.
    assert data["categories"]["release_alerts"] is True
    assert data["categories"]["taste_push"] is True
    assert data["categories"]["system"] is True


@pytest.mark.asyncio
async def test_get_notifications_reflects_patch(client: AsyncClient):
    auth, _ = await _login(client)
    await client.patch(
        "/v1/me/notifications",
        headers={"Authorization": auth},
        json={"categories": {"taste_push": False}},
    )
    resp = await client.get("/v1/me/notifications", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["categories"]["taste_push"] is False
    assert data["categories"]["release_alerts"] is True


@pytest.mark.asyncio
async def test_get_notifications_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/me/notifications")
    assert resp.status_code in (401, 403)


# ── GET /v1/legal/versions ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_legal_versions_no_consents(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.get("/v1/legal/versions", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"]["tos"] == "v1.0"
    assert data["current"]["privacy"] == "v1.0"
    assert data["latest"]["tos"] == "v1.0"
    assert data["my_consents"] == []


@pytest.mark.asyncio
async def test_get_legal_versions_with_consents(client: AsyncClient):
    auth, _ = await _login(client)
    # consent 먼저 기록
    await client.post(
        "/v1/legal/consents",
        headers={"Authorization": auth},
        json={"document_type": "tos", "version": "v1.0"},
    )
    resp = await client.get("/v1/legal/versions", headers={"Authorization": auth})
    assert resp.status_code == 200
    consents = resp.json()["my_consents"]
    assert len(consents) == 1
    assert consents[0]["document_type"] == "tos"


@pytest.mark.asyncio
async def test_get_legal_versions_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/legal/versions")
    assert resp.status_code in (401, 403)


# ── POST /v1/legal/consents ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_consent_tos(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/legal/consents",
        headers={"Authorization": auth},
        json={"document_type": "tos", "version": "v1.0"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "consented_at" in data


@pytest.mark.asyncio
async def test_record_consent_privacy(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/legal/consents",
        headers={"Authorization": auth},
        json={"document_type": "privacy", "version": "v1.0"},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_record_consent_invalid_doc_type(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/legal/consents",
        headers={"Authorization": auth},
        json={"document_type": "eula", "version": "v1.0"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_record_consent_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/legal/consents", json={"document_type": "tos", "version": "v1.0"})
    assert resp.status_code in (401, 403)
