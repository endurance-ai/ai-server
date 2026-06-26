"""Subscription lifecycle service (단일 소스).

ai.subscriptions 를 original_transaction_id 기준으로 upsert 하고, user_profiles.tier
를 파생 캐시로 동기화한다. iap.py(verify/restore) 와 apple_notifications(webhook) 가
공유한다.

status 도출:
- active : 만료 전 + revoke 아님
- grace  : 결제 재시도 유예 (webhook DID_FAIL_TO_RENEW + GRACE_PERIOD)
- expired: 만료됨
- revoked: 환불/취소 (REFUND/REVOKE)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.core.apple_iap import TransactionInfo
from app.core.iap_catalog import duration_days, tier_for

logger = logging.getLogger(__name__)

# tier 가 활성으로 간주되는 status (user_profiles.tier 캐시 반영)
_ENTITLED = {"active", "grace"}


@dataclass
class SubState:
    status: str
    product_id: str
    tier: str
    expires_at: datetime | None
    auto_renew: bool
    original_transaction_id: str


def _effective_status(status: str, expires_at: datetime | None) -> str:
    """저장된 status 를 현재 시각 기준으로 보정 (active 인데 만료 지났으면 expired)."""
    if status == "active" and expires_at is not None and expires_at <= datetime.now(UTC):
        return "expired"
    return status


async def upsert_from_transaction(
    pool: AsyncConnectionPool,
    user_id: UUID,
    txn: TransactionInfo,
    *,
    status: str | None = None,
    auto_renew: bool | None = None,
    notification_type: str | None = None,
) -> SubState:
    """트랜잭션으로 subscriptions upsert + iap_transactions 기록 + tier 동기화."""
    tier = tier_for(txn.product_id)
    expires_at = txn.expires_date or (datetime.now(UTC) + timedelta(days=duration_days(txn.product_id)))
    resolved_status = status or ("active" if expires_at > datetime.now(UTC) else "expired")
    resolved_auto_renew = auto_renew if auto_renew is not None else True
    entitled = resolved_status in _ENTITLED

    async with pool.connection() as conn, conn.cursor() as cur:
        # 구독 현재 상태 upsert (original_transaction_id 기준)
        await cur.execute(
            """
            INSERT INTO ai.subscriptions
                (user_id, original_transaction_id, product_id, tier, status,
                 expires_at, auto_renew, environment, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (original_transaction_id) DO UPDATE SET
                product_id = EXCLUDED.product_id,
                tier       = EXCLUDED.tier,
                status     = EXCLUDED.status,
                expires_at = EXCLUDED.expires_at,
                auto_renew = EXCLUDED.auto_renew,
                updated_at = now()
            """,
            (
                user_id,
                txn.original_transaction_id,
                txn.product_id,
                tier,
                resolved_status,
                expires_at,
                resolved_auto_renew,
                txn.environment,
            ),
        )
        # 영수증 이력 (감사용)
        await cur.execute(
            """
            INSERT INTO ai.iap_transactions
                (user_id, apple_txn_id, apple_original_txn_id, product_id,
                 tier, purchase_date, expires_date, environment, notification_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (apple_txn_id) DO NOTHING
            """,
            (
                user_id,
                txn.transaction_id,
                txn.original_transaction_id,
                txn.product_id,
                tier,
                txn.purchase_date,
                expires_at,
                txn.environment,
                notification_type,
            ),
        )
        # tier 파생 캐시 동기화 (검색 gender gate / daily cap 소비자 유지)
        await cur.execute(
            """
            UPDATE ai.user_profiles
            SET tier = %s, tier_expires_at = %s, updated_at = now()
            WHERE user_id = %s
            """,
            (tier if entitled else "free", expires_at if entitled else None, user_id),
        )

    return SubState(
        status=resolved_status,
        product_id=txn.product_id,
        tier=tier,
        expires_at=expires_at,
        auto_renew=resolved_auto_renew,
        original_transaction_id=txn.original_transaction_id,
    )


async def find_user_by_original_txn(pool: AsyncConnectionPool, original_transaction_id: str) -> UUID | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id FROM ai.subscriptions WHERE original_transaction_id = %s",
            (original_transaction_id,),
        )
        row = await cur.fetchone()
        if row:
            return row[0]
        # 폴백: 과거 영수증 이력에서 조회
        await cur.execute(
            "SELECT user_id FROM ai.iap_transactions WHERE apple_original_txn_id = %s LIMIT 1",
            (original_transaction_id,),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def get_current(pool: AsyncConnectionPool, user_id: UUID) -> SubState | None:
    """유저의 최신 구독 1건 (status 는 현재 시각 기준 보정)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT original_transaction_id, product_id, tier, status, expires_at, auto_renew
            FROM ai.subscriptions
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return SubState(
        original_transaction_id=row[0],
        product_id=row[1],
        tier=row[2],
        status=_effective_status(row[3], row[4]),
        expires_at=row[4],
        auto_renew=row[5],
    )


async def record_event(
    pool: AsyncConnectionPool,
    notification_uuid: str,
    notification_type: str | None,
    subtype: str | None,
    original_transaction_id: str | None,
    raw: dict | None = None,
) -> bool:
    """webhook 멱등성: 처음 보는 notification_uuid 면 True, 중복이면 False."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.iap_events
                (notification_uuid, notification_type, subtype, original_transaction_id, raw_payload)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (notification_uuid) DO NOTHING
            RETURNING event_id
            """,
            (notification_uuid, notification_type, subtype, original_transaction_id, Jsonb(raw) if raw else None),
        )
        row = await cur.fetchone()
    return row is not None
