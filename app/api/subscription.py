"""Subscription status API (명세 계약).

GET /v1/subscription — 내 구독 상태 조회 (status/auto_renew/manage_url 포함)
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool
from app.services import subscription_service

router = APIRouter(prefix="/v1", tags=["subscription"])

# 구독 관리(해지/변경)는 App Store 가 담당 — 정적 딥링크.
_MANAGE_URL = "https://apps.apple.com/account/subscriptions"


class SubscriptionResponse(BaseModel):
    status: str  # 'active' | 'grace' | 'expired' | 'revoked' | 'none'
    product_id: str | None
    expires_at: str | None
    auto_renew: bool | None
    will_renew_at: str | None
    manage_url: str


@router.get("/subscription", response_model=SubscriptionResponse, status_code=status.HTTP_200_OK)
async def get_subscription(
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> SubscriptionResponse:
    """현재 구독 상태 반환. 구독 이력이 없으면 status='none'."""
    state = await subscription_service.get_current(pool, user_id)
    if state is None:
        return SubscriptionResponse(
            status="none",
            product_id=None,
            expires_at=None,
            auto_renew=None,
            will_renew_at=None,
            manage_url=_MANAGE_URL,
        )

    expires_iso = state.expires_at.isoformat() if state.expires_at else None
    # 자동갱신 ON + 활성/유예 상태일 때만 다음 갱신 예정일 노출.
    will_renew_at = expires_iso if (state.auto_renew and state.status in ("active", "grace")) else None
    return SubscriptionResponse(
        status=state.status,
        product_id=state.product_id,
        expires_at=expires_iso,
        auto_renew=state.auto_renew,
        will_renew_at=will_renew_at,
        manage_url=_MANAGE_URL,
    )
