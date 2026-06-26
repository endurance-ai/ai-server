"""IAP/subscription API (명세 계약) integration tests.

GET  /v1/iap/products  · POST /v1/iap/verify · POST /v1/iap/restore
GET  /v1/subscription  · POST /webhooks/apple/notifications-v2
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.apple_iap import RenewalInfo, TransactionInfo
from app.core.social_auth.google import GoogleClaims

_PRODUCT_ID = "com.kiko.subscription.pro.monthly"
_DUMMY_JWS = "header.payload.sig"  # decode_apple_jws는 항상 mock


def _make_txn(
    product_id: str = _PRODUCT_ID,
    expires_delta_days: int = 30,
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
        environment="Sandbox",
    )


async def _login(client: AsyncClient, sub: str | None = None) -> tuple[str, str]:
    sub = sub or f"sub-{uuid4()}"
    with patch(
        "app.api.auth.verify_google_token",
        return_value=GoogleClaims(sub=sub, email="u@test.com", name="User", picture=None),
    ):
        resp = await client.post("/v1/auth/social", json={"provider": "google", "id_token": "t"})
    data = resp.json()
    return f"Bearer {data['access_token']}", data["user_id"]


async def _verify(client: AsyncClient, auth: str, txn: TransactionInfo) -> dict:
    with (
        patch("app.api.iap.decode_apple_jws", return_value={}),
        patch("app.api.iap.parse_transaction", return_value=txn),
    ):
        resp = await client.post(
            "/v1/iap/verify",
            headers={"Authorization": auth},
            json={"signed_transaction_jws": _DUMMY_JWS, "transaction_id": txn.transaction_id},
        )
    return resp


# ── GET /v1/iap/products ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_iap_products(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.get("/v1/iap/products", headers={"Authorization": auth})
    assert resp.status_code == 200
    products = resp.json()  # 명세: bare array
    assert isinstance(products, list)
    assert len(products) == 3
    ids = {p["product_id"] for p in products}
    assert "com.kiko.subscription.pro.monthly" in ids
    # 명세 필드 존재
    p = products[0]
    assert {"name", "description", "period", "price_tier", "currency_hint", "eula_url", "terms_url"} <= p.keys()


@pytest.mark.asyncio
async def test_get_iap_products_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/iap/products")
    assert resp.status_code in (401, 403)


# ── POST /v1/iap/verify ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_returns_subscription_object(client: AsyncClient):
    auth, _ = await _login(client)
    txn = _make_txn(orig_txn_id="orig-1")
    resp = await _verify(client, auth, txn)
    assert resp.status_code == 200
    sub = resp.json()["subscription"]
    assert sub["product_id"] == _PRODUCT_ID
    assert sub["status"] == "active"
    assert sub["auto_renew"] is True
    assert sub["original_transaction_id"] == "orig-1"
    assert sub["expires_at"] is not None


@pytest.mark.asyncio
async def test_verify_reflected_in_subscription_get(client: AsyncClient):
    auth, _ = await _login(client)
    await _verify(client, auth, _make_txn())

    resp = await client.get("/v1/subscription", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["product_id"] == _PRODUCT_ID
    assert data["auto_renew"] is True
    assert data["will_renew_at"] is not None
    assert data["manage_url"]


@pytest.mark.asyncio
async def test_verify_invalid_jws_returns_422(client: AsyncClient):
    auth, _ = await _login(client)
    from jose import JWTError

    with patch("app.api.iap.decode_apple_jws", side_effect=JWTError("bad")):
        resp = await client.post(
            "/v1/iap/verify",
            headers={"Authorization": auth},
            json={"signed_transaction_jws": "bad.jws.token"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_verify_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/iap/verify", json={"signed_transaction_jws": _DUMMY_JWS})
    assert resp.status_code in (401, 403)


# ── GET /v1/subscription ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscription_default_none(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.get("/v1/subscription", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "none"
    assert data["product_id"] is None
    assert data["manage_url"]


@pytest.mark.asyncio
async def test_subscription_requires_auth(client: AsyncClient):
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
            json={"transactions": [{"signed_transaction_jws": _DUMMY_JWS}]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored"] is True
    assert data["subscription"]["status"] == "active"
    assert data["subscription"]["product_id"] == _PRODUCT_ID


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
            json={"transactions": [{"signed_transaction_jws": _DUMMY_JWS}]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored"] is False
    assert data["subscription"]["status"] == "expired"


@pytest.mark.asyncio
async def test_restore_empty_list_returns_422(client: AsyncClient):
    auth, _ = await _login(client)
    resp = await client.post(
        "/v1/iap/restore",
        headers={"Authorization": auth},
        json={"transactions": []},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_restore_requires_auth(client: AsyncClient):
    resp = await client.post("/v1/iap/restore", json={"transactions": [{"signed_transaction_jws": _DUMMY_JWS}]})
    assert resp.status_code in (401, 403)


# ── POST /webhooks/apple/notifications-v2 ────────────────────────────────────


async def _send_webhook(client: AsyncClient, notification: dict, txn: TransactionInfo, renewal: RenewalInfo | None):
    """signedPayload→notification, signedTransactionInfo→txn, signedRenewalInfo→renewal 로 mock."""
    decode_returns = [notification]
    if notification.get("data", {}).get("signedTransactionInfo"):
        decode_returns.append({})  # signedTransactionInfo decode
    if notification.get("data", {}).get("signedRenewalInfo"):
        decode_returns.append({})  # signedRenewalInfo decode
    with (
        patch("app.api.webhooks.apple_notifications.decode_apple_jws", side_effect=decode_returns),
        patch("app.api.webhooks.apple_notifications.parse_transaction", return_value=txn),
        patch("app.api.webhooks.apple_notifications.parse_renewal_info", return_value=renewal),
    ):
        return await client.post("/webhooks/apple/notifications-v2", json={"signedPayload": _DUMMY_JWS})


@pytest.mark.asyncio
async def test_webhook_renew_updates_subscription(client: AsyncClient):
    auth, _ = await _login(client)
    orig = "orig-renew-1"
    await _verify(client, auth, _make_txn(orig_txn_id=orig, expires_delta_days=10))

    renew_txn = _make_txn(orig_txn_id=orig, expires_delta_days=60)
    notification = {
        "notificationType": "DID_RENEW",
        "notificationUUID": str(uuid4()),
        "data": {"signedTransactionInfo": _DUMMY_JWS},
    }
    resp = await _send_webhook(client, notification, renew_txn, None)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # 만료일 연장 반영 확인
    sub = (await client.get("/v1/subscription", headers={"Authorization": auth})).json()
    assert sub["status"] == "active"


@pytest.mark.asyncio
async def test_webhook_is_idempotent(client: AsyncClient):
    auth, _ = await _login(client)
    orig = "orig-idem-1"
    await _verify(client, auth, _make_txn(orig_txn_id=orig))

    nuuid = str(uuid4())
    txn = _make_txn(orig_txn_id=orig, expires_delta_days=60)
    notification = {
        "notificationType": "DID_RENEW",
        "notificationUUID": nuuid,
        "data": {"signedTransactionInfo": _DUMMY_JWS},
    }
    r1 = await _send_webhook(client, notification, txn, None)
    r2 = await _send_webhook(client, notification, txn, None)
    assert r1.json()["status"] == "ok"
    assert r2.json()["status"] == "duplicate"


@pytest.mark.asyncio
async def test_webhook_refund_revokes(client: AsyncClient):
    auth, _ = await _login(client)
    orig = "orig-refund-1"
    await _verify(client, auth, _make_txn(orig_txn_id=orig))

    txn = _make_txn(orig_txn_id=orig)
    notification = {
        "notificationType": "REFUND",
        "notificationUUID": str(uuid4()),
        "data": {"signedTransactionInfo": _DUMMY_JWS},
    }
    resp = await _send_webhook(client, notification, txn, None)
    assert resp.status_code == 200

    sub = (await client.get("/v1/subscription", headers={"Authorization": auth})).json()
    assert sub["status"] == "revoked"


@pytest.mark.asyncio
async def test_webhook_missing_payload_returns_400(client: AsyncClient):
    resp = await client.post("/webhooks/apple/notifications-v2", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_invalid_jws_returns_ok_not_500(client: AsyncClient):
    from jose import JWTError

    with patch(
        "app.api.webhooks.apple_notifications.decode_apple_jws",
        side_effect=JWTError("bad"),
    ):
        resp = await client.post("/webhooks/apple/notifications-v2", json={"signedPayload": "bad.jws"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_no_transaction_returns_ignored(client: AsyncClient):
    notification = {"notificationType": "TEST", "notificationUUID": str(uuid4()), "data": {}}
    with patch("app.api.webhooks.apple_notifications.decode_apple_jws", return_value=notification):
        resp = await client.post("/webhooks/apple/notifications-v2", json={"signedPayload": _DUMMY_JWS})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
