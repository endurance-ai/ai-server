"""Subscription status API.

GET /v1/subscription — 내 구독 상태 조회
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["subscription"])


class SubscriptionResponse(BaseModel):
    tier: str
    tier_expires_at: str | None
    is_active: bool


@router.get("/subscription", response_model=SubscriptionResponse, status_code=status.HTTP_200_OK)
async def get_subscription(
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> SubscriptionResponse:
    """현재 구독 티어 및 만료일 반환."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT tier, tier_expires_at FROM ai.user_profiles WHERE user_id = %s",
            (user_id,),
        )
        row = await cur.fetchone()

    if not row:
        return SubscriptionResponse(tier="free", tier_expires_at=None, is_active=False)

    tier, expires_at = row
    is_active = tier != "free" and (expires_at is None or expires_at > datetime.now(UTC))

    return SubscriptionResponse(
        tier=tier,
        tier_expires_at=expires_at.isoformat() if expires_at else None,
        is_active=is_active,
    )
