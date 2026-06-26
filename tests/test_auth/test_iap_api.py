"""GET /v1/iap/products, POST /v1/iap/verify, GET /v1/subscription,
POST /v1/iap/restore, POST /webhooks/apple/notifications-v2 integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.apple_iap import TransactionInfo
from app.core.social_auth.google import GoogleClaims

_PRODUCT_ID = "com.kiko.subscription.pro.monthly"
_PRODUCT_ID_YEARLY = "com.kiko.subscription.pro.yearly"
_DUMMY_JWS = "header.payload.sig"  # decode_apple_jws는 항상 mock


def _make_txn(
    product_id: str = _PRODUCT_ID,
    expires_delta_days: int = 30,
    environment: str = "Sandbox",
    txn_id: str | None = None,
    orig_txn_id: str | None = None,
) -> TransactionInfo:
    now = datetime.now(UTC)
    return TransactionInfo(
        transaction_id=txn_id or str(uuid4()),
        original_transaction_id=orig_txn_id or str(uuid4()),
        product_id=product_id,
        purchase_date=now,
        expires_date=now + timedelta(days=expires_delta_days),
        environment=environment,
    )


async def _login(client: AsyncClient, sub: str | None = None) -> tuple[str, str]:
    sub = sub or f"sub-{uuid4()}"
    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=sub, email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/auth/social", json={"provider": "google", "id_token": "t"})
    data = resp.json()
    return f"Bearer {data['access_token']}", data["user_id"]


# ── GET /v1/iap/products ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_iap_products(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.get("/v1/iap/products", headers={"Authorization": auth})
    assert resp.status_code == 200
    products = resp.json()["products"]
    assert len(products) == 3
    ids = {p["product_id"] for p in products}
    assert "com.kiko.subscription.basic.monthly" in ids
    assert "com.kiko.subscription.pro.monthly" in ids
    assert "com.kiko.subscription.pro.yearly" in ids


@pytest.mark.asyncio
async def test_get_iap_products_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/iap/products")
    assert resp.status_code in (401, 403)


# ── POST /v1/iap/verify ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_transaction_activates_tier(client: AsyncClient):
    auth, _ = await _login(client)
    txn = _make_txn()
    with (
        patch("app.api.iap.decode_apple_jws", return_value={}),
        patch("app.api.iap.parse_transaction", return_value=txn),
    ):
        resp = await client.post(
            "/v1/iap/verify",
            headers={"Authorization": auth},
            json={"jws_transaction": _DUMMY_JWS},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "pro"
    assert data["tier_expires_at"] is not None
    assert data["transaction_id"] == txn.transaction_id


@pytest.mark.asyncio
async def test_verify_transaction_updates_subscription(client: AsyncClient, pool):
    auth, user_id = await _login(client)
    txn = _make_txn()
    with (
        patch("app.api.iap.decode_apple_jws", return_value={}),
        patch("app.api.iap.parse_transaction", return_value=txn),
    ):
        await client.post(
            "/v1/iap/verify",
            headers={"Authorization": auth},
            json={"jws_transaction": _DUMMY_JWS},
        )

    # subscription 조회로 실제 DB 반영 확인
    resp = await client.get("/v1/subscription", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "pro"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_verify_invalid_jws_returns_422(client: AsyncClient):
    auth, _ = await _login(client)
    from jose import JWTError

    with patch("app.api.iap.decode_apple_jws", side_effect=JWTError("bad")):
        resp = await client.post(
            "/v1/iap/verify",
            headers={"Authorization": auth},
            json={"jws_transaction": "bad.jws.token"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_verify_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/iap/verify", json={"jws_transaction": _DUMMY_JWS})
    assert resp.status_code in (401, 403)


# ── GET /v1/subscription ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_subscription_default_free(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.get("/v1/subscription", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "free"
    assert data["tier_expires_at"] is None
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_get_subscription_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/subscription")
    assert resp.status_code in (401, 403)


# ── POST /v1/iap/restore ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_valid_subscription(client: AsyncClient):
    auth, _ = await _login(client)
    txn = _make_txn(expires_delta_days=20)
    with (
        patch("app.api.iap.decode_apple_jws", return_value={}),
        patch("app.api.iap.parse_transaction", return_value=txn),
    ):
        resp = await client.post(
            "/v1/iap/restore",
            headers={"Authorization": auth},
            json={"jws_transactions": [_DUMMY_JWS]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored"] is True
    assert data["tier"] == "pro"


@pytest.mark.asyncio
async def test_restore_expired_subscription(client: AsyncClient):
    auth, _ = await _login(client)
    txn = _make_txn(expires_delta_days=-1)  # 이미 만료
    with (
        patch("app.api.iap.decode_apple_jws", return_value={}),
        patch("app.api.iap.parse_transaction", return_value=txn),
    ):
        resp = await client.post(
            "/v1/iap/restore",
            headers={"Authorization": auth},
            json={"jws_transactions": [_DUMMY_JWS]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored"] is False
    assert data["tier"] == "free"


@pytest.mark.asyncio
async def test_restore_empty_list_returns_422(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/iap/restore",
        headers={"Authorization": auth},
        json={"jws_transactions": []},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_restore_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/iap/restore", json={"jws_transactions": [_DUMMY_JWS]})
    assert resp.status_code in (401, 403)


# ── POST /webhooks/apple/notifications-v2 ────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_renew_extends_subscription(client: AsyncClient, pool):
    auth, user_id = await _login(client)

    # 먼저 기존 구독 등록 (original_txn_id가 DB에 있어야 webhook에서 user 찾을 수 있음)
    orig_txn_id = str(uuid4())
    txn_id = str(uuid4())
    txn = _make_txn(txn_id=txn_id, orig_txn_id=orig_txn_id)
    with (
        patch("app.api.iap.decode_apple_jws", return_value={}),
        patch("app.api.iap.parse_transaction", return_value=txn),
    ):
        await client.post(
            "/v1/iap/verify",
            headers={"Authorization": auth},
            json={"jws_transaction": _DUMMY_JWS},
        )

    # 갱신 알림 — 새 txn_id로
    new_txn_id = str(uuid4())
    renew_txn = _make_txn(txn_id=new_txn_id, orig_txn_id=orig_txn_id, expires_delta_days=60)
    notification = {
        "notificationType": "DID_RENEW",
        "data": {"bundleId": "com.kiko.app", "signedTransactionInfo": _DUMMY_JWS},
    }
    with (
        patch("app.api.webhooks.apple_notifications.decode_apple_jws") as mock_decode,
        patch("app.api.webhooks.apple_notifications.parse_transaction", return_value=renew_txn),
        patch("app.api.iap.decode_apple_jws", return_value={}),
        patch("app.api.iap.parse_transaction", return_value=renew_txn),
    ):
        # signedPayload 디코드 → notification dict 반환
        mock_decode.side_effect = [notification, {}]
        resp = await client.post(
            "/webhooks/apple/notifications-v2",
            json={"signedPayload": _DUMMY_JWS},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_webhook_missing_payload_returns_400(client: AsyncClient):
    resp = await client.post("/webhooks/apple/notifications-v2", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_invalid_jws_returns_ok_not_500(client: AsyncClient):
    """JWS 파싱 실패 시 Apple 재전송을 막기 위해 200 반환."""
    from jose import JWTError

    with patch(
        "app.api.webhooks.apple_notifications.decode_apple_jws",
        side_effect=JWTError("bad"),
    ):
        resp = await client.post(
            "/webhooks/apple/notifications-v2",
            json={"signedPayload": "bad.jws"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_unhandled_type_returns_ok(client: AsyncClient):
    notification = {"notificationType": "TEST", "data": {}}
    with patch("app.api.webhooks.apple_notifications.decode_apple_jws", return_value=notification):
        resp = await client.post(
            "/webhooks/apple/notifications-v2",
            json={"signedPayload": _DUMMY_JWS},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
