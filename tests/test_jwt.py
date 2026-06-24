"""Unit tests for app.core.jwt — no DB, no network required."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jose import jwt

import app.core.jwt as jwt_mod


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch):
    monkeypatch.setattr(jwt_mod, "settings", _FakeSettings())


class _FakeSettings:
    JWT_SECRET = "test-secret-key-minimum-32-characters-long"
    ACCESS_TOKEN_EXPIRE_MINUTES = 60
    REFRESH_TOKEN_EXPIRE_DAYS = 30


def test_access_token_roundtrip():
    uid = uuid4()
    token = jwt_mod.create_access_token(uid)
    payload = jwt_mod.verify_access_token(token)
    assert payload["sub"] == str(uid)
    assert payload["type"] == "access"


def test_access_token_wrong_type_rejected():
    uid = uuid4()
    # Manually craft a refresh-type token signed with same secret
    bad = jwt.encode(
        {"sub": str(uid), "type": "refresh", "exp": datetime.now(UTC) + timedelta(hours=1)},
        "test-secret-key-minimum-32-characters-long",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        jwt_mod.verify_access_token(bad)
    assert exc.value.status_code == 401


def test_access_token_expired_rejected():
    uid = uuid4()
    expired = jwt.encode(
        {"sub": str(uid), "type": "access", "exp": datetime.now(UTC) - timedelta(seconds=1)},
        "test-secret-key-minimum-32-characters-long",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        jwt_mod.verify_access_token(expired)
    assert exc.value.status_code == 401


def test_refresh_token_hash_deterministic():
    raw, h1 = jwt_mod.create_refresh_token()
    h2 = jwt_mod.hash_refresh_token(raw)
    assert h1 == h2
    assert len(raw) > 30
    assert len(h1) == 64  # SHA-256 hex


def test_refresh_tokens_are_unique():
    raw1, _ = jwt_mod.create_refresh_token()
    raw2, _ = jwt_mod.create_refresh_token()
    assert raw1 != raw2
