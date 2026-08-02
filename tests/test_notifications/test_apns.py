"""APNs 클라이언트 — JWT 서명, 페이로드 모양, 죽은 토큰 판정."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt

from app.core.config import settings
from app.services.push.apns import ApnsClient, apns_configured


@pytest.fixture
def _apns_env(monkeypatch):
    """실제 .p8 과 같은 형태(PKCS8 EC P-256 PEM)의 테스트 키를 심는다."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    monkeypatch.setattr(settings, "APNS_AUTH_KEY", pem)
    monkeypatch.setattr(settings, "APNS_KEY_ID", "KEYID12345")
    monkeypatch.setattr(settings, "APNS_TEAM_ID", "TEAM123456")
    monkeypatch.setattr(settings, "APNS_TOPIC", "com.kikoai.app")
    return public_pem


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://api.push.apple.com", transport=httpx.MockTransport(handler))


def test_configured_requires_every_secret(monkeypatch, _apns_env):
    assert apns_configured() is True
    monkeypatch.setattr(settings, "APNS_KEY_ID", "")
    assert apns_configured() is False


async def test_sandbox_uses_its_own_key_and_host(monkeypatch, _apns_env):
    monkeypatch.setattr(
        settings,
        "APNS_SANDBOX_AUTH_KEY_B64",
        base64.b64encode(settings.APNS_AUTH_KEY.encode()).decode(),
    )
    monkeypatch.setattr(settings, "APNS_SANDBOX_KEY_ID", "SANDBOX01")

    client = ApnsClient(environment="development")
    try:
        assert apns_configured("development") is True
        assert str(client._client.base_url) == "https://api.sandbox.push.apple.com"
    finally:
        await client.aclose()


def test_missing_credentials_do_not_crash_the_check(monkeypatch):
    monkeypatch.setattr(settings, "APNS_AUTH_KEY", "")
    monkeypatch.setattr(settings, "APNS_AUTH_KEY_PATH", "/nonexistent/key.p8")
    assert apns_configured() is False


async def test_send_signs_es256_and_shapes_the_alert(_apns_env):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.read().decode()
        return httpx.Response(200)

    client = ApnsClient()
    await client._client.aclose()
    client._client = _mock_client(handler)
    async with client:
        result = await client.send(
            "devtoken",
            title="재입고 알림",
            body="다시 입고됐어요",
            data={"kind": "restock"},
            apns_id="123e4567-e89b-12d3-a456-426614174000",
            expiration=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        )

    assert result.ok
    assert captured["url"].endswith("/3/device/devtoken")
    assert captured["headers"]["apns-topic"] == "com.kikoai.app"
    assert captured["headers"]["apns-push-type"] == "alert"
    assert captured["headers"]["apns-id"] == "123e4567-e89b-12d3-a456-426614174000"
    assert captured["headers"]["apns-expiration"] == str(int(datetime(2026, 8, 2, 12, 0, tzinfo=UTC).timestamp()))

    token = captured["headers"]["authorization"].removeprefix("bearer ")
    claims = jwt.decode(token, _apns_env, algorithms=["ES256"], options={"verify_aud": False})
    assert claims["iss"] == "TEAM123456"
    assert jwt.get_unverified_header(token)["kid"] == "KEYID12345"

    payload = json.loads(captured["body"])
    assert payload["aps"]["alert"] == {"title": "재입고 알림", "body": "다시 입고됐어요"}
    assert payload["kind"] == "restock"


async def test_provider_token_is_reused_across_sends(_apns_env):
    """APNs 는 20분 내 재발급을 TooManyProviderTokenUpdates 로 거절한다."""
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        return httpx.Response(200)

    client = ApnsClient()
    await client._client.aclose()
    client._client = _mock_client(handler)
    async with client:
        await client.send("a", title="t", body="b")
        await client.send("b", title="t", body="b")

    assert tokens[0] == tokens[1]


async def test_unregistered_marks_the_token_dead(_apns_env):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"reason": "Unregistered", "timestamp": 1785672000000})

    client = ApnsClient()
    await client._client.aclose()
    client._client = _mock_client(handler)
    async with client:
        result = await client.send("stale", title="t", body="b")

    assert not result.ok
    assert result.token_is_dead
    assert result.invalidated_at == datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


async def test_retry_after_is_preserved(_apns_env):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "120"}, json={"reason": "TooManyRequests"})

    client = ApnsClient()
    await client._client.aclose()
    client._client = _mock_client(handler)
    async with client:
        result = await client.send("busy", title="t", body="b")

    assert result.retryable
    assert result.retry_after_s == 120


@pytest.mark.parametrize(
    "status,reason",
    [
        (503, "ServiceUnavailable"),
        # 환경(sandbox/production)이나 apns-topic 이 어긋나도 이 두 가지가 나온다.
        # 설정 실수로 멀쩡한 기기 토큰을 전부 지우면 안 된다.
        (400, "BadDeviceToken"),
        (400, "DeviceTokenNotForTopic"),
    ],
)
async def test_non_definitive_failures_keep_the_token(_apns_env, status, reason):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"reason": reason})

    client = ApnsClient()
    await client._client.aclose()
    client._client = _mock_client(handler)
    async with client:
        result = await client.send("live", title="t", body="b")

    assert not result.ok
    assert not result.token_is_dead
