"""POST /v1/uploads integration tests."""

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


def _configure_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings
    from app.core.di import provide_settings
    from app.main import app

    settings = Settings()
    monkeypatch.setattr(settings, "UPLOADS_S3_BUCKET", "kiko-test-uploads")
    monkeypatch.setattr(settings, "UPLOADS_S3_REGION", "ap-northeast-2")
    monkeypatch.setattr(settings, "UPLOADS_S3_ACCESS_KEY_ID", "AKIATESTKEY")
    monkeypatch.setattr(settings, "UPLOADS_S3_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setattr(settings, "UPLOADS_PUBLIC_BASE_URL", "https://cdn.example.com")
    monkeypatch.setattr(settings, "UPLOADS_MAX_SIZE_BYTES", 1_048_576)
    monkeypatch.setattr(settings, "UPLOADS_PRESIGN_EXPIRES_SECONDS", 300)
    monkeypatch.setitem(app.dependency_overrides, provide_settings, lambda: settings)


@pytest.mark.asyncio
async def test_create_upload_returns_presigned_put_url(client: AsyncClient, pool, monkeypatch: pytest.MonkeyPatch):
    _configure_uploads(monkeypatch)
    auth, user_id = await _login(client)

    resp = await client.post(
        "/v1/uploads",
        headers={"Authorization": auth},
        json={"filename": "look.png", "content_type": "image/png", "size_bytes": 12345},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"upload_id", "upload_url", "image_url", "expires_at", "max_size_bytes"}
    assert data["max_size_bytes"] == 1_048_576
    assert data["image_url"].startswith(f"https://cdn.example.com/uploads/{user_id}/")
    assert data["image_url"].endswith("/look.png")
    assert data["upload_url"].startswith("https://kiko-test-uploads.s3.ap-northeast-2.amazonaws.com/uploads/")
    assert "X-Amz-Signature=" in data["upload_url"]
    assert "X-Amz-SignedHeaders=content-type%3Bhost" in data["upload_url"]

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT user_id, content_type, size_bytes, image_url, status FROM ai.uploads")
        row = await cur.fetchone()
    assert str(row[0]) == user_id
    assert row[1:] == ("image/png", 12345, data["image_url"], "pending")


@pytest.mark.asyncio
async def test_create_upload_rejects_oversized_image(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    _configure_uploads(monkeypatch)
    auth, _ = await _login(client)

    resp = await client.post(
        "/v1/uploads",
        headers={"Authorization": auth},
        json={"filename": "large.jpg", "content_type": "image/jpeg", "size_bytes": 1_048_577},
    )

    assert resp.status_code == 413
    assert resp.json()["detail"] == "upload_too_large"


@pytest.mark.asyncio
async def test_create_upload_rejects_unsupported_content_type(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    _configure_uploads(monkeypatch)
    auth, _ = await _login(client)

    resp = await client.post(
        "/v1/uploads",
        headers={"Authorization": auth},
        json={"filename": "x.gif", "content_type": "image/gif", "size_bytes": 100},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_upload_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/v1/uploads",
        json={"filename": "look.webp", "content_type": "image/webp", "size_bytes": 100},
    )

    assert resp.status_code in (401, 403)
