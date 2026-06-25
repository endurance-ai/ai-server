"""Apple App Store Server Notifications V2 webhook.

POST /webhooks/apple/notifications-v2
Apple이 구독 상태 변경 시 서버로 전송하는 S2S 알림을 처리합니다.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from jose import JWTError
from psycopg_pool import AsyncConnectionPool

from app.core.apple_iap import decode_apple_jws, parse_transaction
from app.core.di import provide_db_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/apple", tags=["webhooks"])

# 구독 만료/환불 시 free로 다운그레이드하는 타입
_DOWNGRADE_TYPES = {"EXPIRED", "REFUND", "REVOKE"}
# 갱신 성공 시 tier 연장하는 타입
_RENEW_TYPES = {"DID_RENEW", "SUBSCRIBED", "DID_RECOVER"}


@router.post("/notifications-v2", status_code=status.HTTP_200_OK)
async def apple_notifications_v2(request: Request) -> dict:
    """Apple S2S 알림 V2 처리.

    signedPayload(JWS) → 알림 유형 파싱 → 구독 상태 갱신.
    Apple은 200 응답을 받아야 재전송하지 않습니다.
    """
    body = await request.json()
    signed_payload = body.get("signedPayload")
    if not signed_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="signedPayload missing")

    # 외부 의존성이므로 try/except로 감쌈 — 400이 아닌 200 반환해 Apple 재전송 방지
    try:
        notification = decode_apple_jws(signed_payload)
    except (JWTError, Exception) as e:
        logger.warning("Apple notification JWS decode failed: %s", e)
        return {"status": "ignored", "reason": "jws_decode_failed"}

    notification_type = notification.get("notificationType", "")
    data = notification.get("data", {})
    bundle_id = data.get("bundleId", "")

    logger.info("Apple notification received: type=%s bundle=%s", notification_type, bundle_id)

    if notification_type in _RENEW_TYPES:
        await _handle_renew(data, notification_type)
    elif notification_type in _DOWNGRADE_TYPES:
        await _handle_downgrade(data, notification_type)
    else:
        logger.info("Apple notification type %s — no action", notification_type)

    return {"status": "ok"}


async def _handle_renew(data: dict, notification_type: str) -> None:
    signed_txn = data.get("signedTransactionInfo")
    if not signed_txn:
        return

    pool: AsyncConnectionPool = provide_db_pool()

    try:
        payload = decode_apple_jws(signed_txn)
        txn = parse_transaction(payload)
    except (JWTError, KeyError, ValueError) as e:
        logger.warning("signedTransactionInfo decode failed: %s", e)
        return

    # apple_original_txn_id로 user_id 조회
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id FROM ai.iap_transactions WHERE apple_original_txn_id = %s LIMIT 1",
            (txn.original_transaction_id,),
        )
        row = await cur.fetchone()

    if not row:
        logger.warning("No user found for original_txn_id=%s", txn.original_transaction_id)
        return

    user_id = row[0]

    # iap 모듈의 _activate_subscription 로직과 동일
    from app.api.iap import _activate_subscription

    _ = await _activate_subscription(pool, user_id, txn, notification_type=notification_type)
    logger.info("Subscription renewed for user=%s txn=%s", user_id, txn.transaction_id)


async def _handle_downgrade(data: dict, notification_type: str) -> None:
    signed_txn = data.get("signedTransactionInfo")
    if not signed_txn:
        return

    pool: AsyncConnectionPool = provide_db_pool()

    try:
        payload = decode_apple_jws(signed_txn)
        txn = parse_transaction(payload)
    except (JWTError, KeyError, ValueError) as e:
        logger.warning("signedTransactionInfo decode failed: %s", e)
        return

    async with pool.connection() as conn, conn.cursor() as cur:
        # user_id 조회
        await cur.execute(
            "SELECT user_id FROM ai.iap_transactions WHERE apple_original_txn_id = %s LIMIT 1",
            (txn.original_transaction_id,),
        )
        row = await cur.fetchone()
        if not row:
            return

        user_id = row[0]

        # 트랜잭션 기록
        await cur.execute(
            """
            INSERT INTO ai.iap_transactions
                (user_id, apple_txn_id, apple_original_txn_id, product_id,
                 tier, purchase_date, expires_date, environment, notification_type)
            VALUES (%s, %s, %s, %s, 'free', %s, %s, %s, %s)
            ON CONFLICT (apple_txn_id) DO NOTHING
            """,
            (
                user_id,
                txn.transaction_id,
                txn.original_transaction_id,
                txn.product_id,
                txn.purchase_date,
                txn.expires_date,
                txn.environment,
                notification_type,
            ),
        )
        # 유저 티어 free로 다운그레이드
        await cur.execute(
            """
            UPDATE ai.user_profiles
            SET tier = 'free', tier_expires_at = NULL, updated_at = now()
            WHERE user_id = %s
            """,
            (user_id,),
        )

    logger.info("Subscription downgraded to free for user=%s type=%s", user_id, notification_type)
