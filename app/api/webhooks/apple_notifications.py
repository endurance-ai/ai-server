"""Apple App Store Server Notifications V2 webhook.

POST /webhooks/apple/notifications-v2
Apple 이 구독 상태 변경 시 보내는 S2S 알림(JWS)을 받아 ai.subscriptions 를 갱신한다.
notification_uuid 로 멱등 처리하고, signedRenewalInfo 로 auto_renew/grace 를 반영한다.
Apple 은 비-2xx 응답 시 재전송하므로, 처리 불가 케이스도 200 으로 응답한다.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from jose import JWTError

from app.core.apple_iap import decode_apple_jws, parse_renewal_info, parse_transaction
from app.core.di import provide_db_pool
from app.services import subscription_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/apple", tags=["webhooks"])


def _map_status(notification_type: str, subtype: str | None) -> str | None:
    """알림 유형 → 구독 status. None 이면 만료일 기준으로 서비스가 도출."""
    if notification_type in ("SUBSCRIBED", "DID_RENEW", "DID_RECOVER"):
        return "active"
    if notification_type in ("EXPIRED", "GRACE_PERIOD_EXPIRED"):
        return "expired"
    if notification_type in ("REFUND", "REVOKE"):
        return "revoked"
    if notification_type == "DID_FAIL_TO_RENEW":
        return "grace" if subtype == "GRACE_PERIOD" else "expired"
    return None  # DID_CHANGE_RENEWAL_STATUS 등 → 만료일 기준 도출


def _safe_decode(jws: str | None):
    if not jws:
        return None
    try:
        return decode_apple_jws(jws)
    except (JWTError, Exception) as e:  # noqa: BLE001 — 외부 입력, 어떤 실패든 무시
        logger.warning("Apple webhook sub-JWS decode failed: %s", e)
        return None


@router.post("/notifications-v2", status_code=status.HTTP_200_OK)
async def apple_notifications_v2(request: Request) -> dict:
    """Apple S2S 알림 V2 처리 (멱등)."""
    body = await request.json()
    signed_payload = body.get("signedPayload")
    if not signed_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="signedPayload missing")

    notification = _safe_decode(signed_payload)
    if notification is None:
        return {"status": "ignored", "reason": "jws_decode_failed"}

    ntype = notification.get("notificationType", "")
    subtype = notification.get("subtype")
    nuuid = notification.get("notificationUUID")
    data = notification.get("data", {})

    txn_payload = _safe_decode(data.get("signedTransactionInfo"))
    renewal_payload = _safe_decode(data.get("signedRenewalInfo"))
    txn = parse_transaction(txn_payload) if txn_payload else None
    renewal = parse_renewal_info(renewal_payload) if renewal_payload else None

    original_txn_id = (txn.original_transaction_id if txn else None) or (
        renewal.original_transaction_id if renewal else None
    )

    pool = provide_db_pool()

    # 멱등성: 처음 보는 알림만 처리
    if nuuid:
        fresh = await subscription_service.record_event(
            pool, nuuid, ntype, subtype, original_txn_id, raw={"type": ntype, "subtype": subtype}
        )
        if not fresh:
            logger.info("Apple notification %s (uuid=%s) already processed — skip", ntype, nuuid)
            return {"status": "duplicate"}

    logger.info("Apple notification: type=%s subtype=%s", ntype, subtype)

    if txn is None or original_txn_id is None:
        return {"status": "ignored", "reason": "no_transaction"}

    user_id = await subscription_service.find_user_by_original_txn(pool, original_txn_id)
    if user_id is None:
        logger.warning("Apple webhook: no user for original_txn_id=%s", original_txn_id)
        return {"status": "ignored", "reason": "unknown_user"}

    await subscription_service.upsert_from_transaction(
        pool,
        user_id,
        txn,
        status=_map_status(ntype, subtype),
        auto_renew=(renewal.auto_renew if renewal else None),
        notification_type=ntype,
    )
    logger.info("Apple webhook applied: user=%s type=%s", user_id, ntype)
    return {"status": "ok"}
