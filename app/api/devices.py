"""Devices & Notification Preferences API.

POST  /v1/devices              — APNs 토큰 등록
GET   /v1/me/notifications     — 알림 카테고리 수신 동의 현재값 조회
PATCH /v1/me/notifications     — 알림 카테고리 수신 동의 저장
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, model_validator

from app.api.deps import get_current_user_id
from app.core.config import settings
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["devices"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class RegisterDeviceRequest(BaseModel):
    # ``apns_token`` is retained until the current mobile build moves to the
    # provider-neutral contract. New clients must send push_token.
    push_token: str | None = None
    apns_token: str | None = None
    device_id: UUID | None = None
    platform: Literal["ios", "android"] = "ios"
    provider: Literal["apns", "fcm"] | None = None
    environment: Literal["development", "production"] | None = None
    app_version: str | None = None
    device_model: str | None = None

    @model_validator(mode="after")
    def validate_token(self) -> RegisterDeviceRequest:
        token = (self.push_token or self.apns_token or "").strip()
        if not token:
            raise ValueError("push_token is required")
        if len(token) > 512 or any(ch.isspace() for ch in token):
            raise ValueError("push_token is invalid")
        if self.provider == "apns" and self.platform != "ios":
            raise ValueError("apns provider requires ios platform")
        if self.provider == "fcm" and self.platform != "android":
            raise ValueError("fcm provider requires android platform")
        self.push_token = token
        return self


class RegisterDeviceResponse(BaseModel):
    device_id: str
    registered_at: str
    provider: str
    environment: str


class NotificationCategories(BaseModel):
    release_alerts: bool | None = None
    taste_push: bool | None = None
    system: bool | None = None
    # 알림 1차 (migration 0023) — 찜/브랜드 구독 기반 3종. PATCH 가 jsonb `||` 병합이라
    # 키를 늘려도 기존 3키와 모바일 알림 화면은 영향받지 않는다.
    restock: bool | None = None
    price_drop: bool | None = None
    brand_new_product: bool | None = None


class UpdateNotificationsRequest(BaseModel):
    categories: NotificationCategories


class UpdateNotificationsResponse(BaseModel):
    categories: dict
    updated_at: str


# Default opt-in state for users with no saved notification_settings yet.
_DEFAULT_NOTIFICATION_SETTINGS = {
    "release_alerts": True,
    "taste_push": True,
    "system": True,
    "restock": True,
    "price_drop": True,
    "brand_new_product": True,
}


def _with_defaults(stored: dict | None) -> dict:
    """저장값 위에 기본값을 깔아 항상 전체 키를 돌려준다.

    0012 의 컬럼 기본값은 3키뿐이라 기존 유저 행에는 0023 에서 추가된 3종이 없다.
    저장값이 항상 이기므로 기존 3키의 값은 그대로 유지된다.
    """
    return {**_DEFAULT_NOTIFICATION_SETTINGS, **(stored or {})}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/devices", response_model=RegisterDeviceResponse, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: RegisterDeviceRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> RegisterDeviceResponse:
    """Register or refresh one native push endpoint.

    The endpoint key is global. If the same installation signs into another
    account, ownership moves to the new account instead of leaking pushes to
    the previous user.
    """
    provider = body.provider or ("apns" if body.platform == "ios" else "fcm")
    environment = body.environment or "production"  # legacy mobile request
    topic = settings.APNS_TOPIC if provider == "apns" else settings.ANDROID_PACKAGE
    async with pool.connection() as conn, conn.cursor() as cur:
        row = None
        if body.device_id is not None:
            await cur.execute(
                """
                UPDATE ai.devices
                SET user_id = %s,
                    push_token = %s,
                    platform = %s,
                    provider = %s,
                    environment = %s,
                    topic = %s,
                    app_version = %s,
                    device_model = %s,
                    status = 'active',
                    disabled_at = NULL,
                    invalidated_at = NULL,
                    failure_count = 0,
                    last_error = NULL,
                    last_seen_at = now(),
                    registered_at = now()
                WHERE device_id = %s
                RETURNING device_id, registered_at, provider, environment
                """,
                (
                    user_id,
                    body.push_token,
                    body.platform,
                    provider,
                    environment,
                    topic,
                    body.app_version,
                    body.device_model,
                    body.device_id,
                ),
            )
            row = await cur.fetchone()

        if row is None:
            await cur.execute(
                """
                INSERT INTO ai.devices
                    (user_id, push_token, platform, provider, environment, topic,
                     app_version, device_model, status, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', now())
                ON CONFLICT (provider, environment, topic, push_token) DO UPDATE SET
                    user_id       = EXCLUDED.user_id,
                    platform     = EXCLUDED.platform,
                    app_version  = EXCLUDED.app_version,
                    device_model = EXCLUDED.device_model,
                    status        = 'active',
                    disabled_at   = NULL,
                    invalidated_at = NULL,
                    failure_count = 0,
                    last_error    = NULL,
                    last_seen_at  = now(),
                    registered_at = now()
                RETURNING device_id, registered_at, provider, environment
                """,
                (
                    user_id,
                    body.push_token,
                    body.platform,
                    provider,
                    environment,
                    topic,
                    body.app_version,
                    body.device_model,
                ),
            )
            row = await cur.fetchone()

    return RegisterDeviceResponse(
        device_id=str(row[0]),
        registered_at=row[1].isoformat(),
        provider=row[2],
        environment=row[3],
    )


@router.get("/me/notifications", response_model=UpdateNotificationsResponse, status_code=status.HTTP_200_OK)
async def get_notifications(
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> UpdateNotificationsResponse:
    """현재 알림 수신 동의값 조회. 알림 화면 진입 시 토글 초기값으로 사용."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT notification_settings, updated_at FROM ai.user_profiles WHERE user_id = %s",
            (user_id,),
        )
        row = await cur.fetchone()

    settings = _with_defaults(row[0] if row else None)
    updated_at = row[1].isoformat() if row and row[1] else ""

    return UpdateNotificationsResponse(categories=settings, updated_at=updated_at)


@router.patch("/me/notifications", response_model=UpdateNotificationsResponse, status_code=status.HTTP_200_OK)
async def update_notifications(
    body: UpdateNotificationsRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> UpdateNotificationsResponse:
    """카테고리별 푸시 알림 수신 동의 갱신. 전달된 키만 업데이트."""
    updates = {k: v for k, v in body.categories.model_dump().items() if v is not None}

    async with pool.connection() as conn, conn.cursor() as cur:
        if updates:
            await cur.execute(
                """
                UPDATE ai.user_profiles
                SET notification_settings = notification_settings || %s::jsonb,
                    updated_at = now()
                WHERE user_id = %s
                RETURNING notification_settings, updated_at
                """,
                (__import__("json").dumps(updates), user_id),
            )
        else:
            await cur.execute(
                "SELECT notification_settings, updated_at FROM ai.user_profiles WHERE user_id = %s",
                (user_id,),
            )
        row = await cur.fetchone()

    settings = _with_defaults(row[0] if row else None)
    updated_at = row[1].isoformat() if row and row[1] else ""

    return UpdateNotificationsResponse(categories=settings, updated_at=updated_at)
