"""Devices & Notification Preferences API.

POST  /v1/devices              — APNs 토큰 등록
PATCH /v1/me/notifications     — 알림 카테고리 수신 동의 저장
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["devices"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class RegisterDeviceRequest(BaseModel):
    apns_token: str
    platform: str = "ios"
    app_version: str | None = None
    device_model: str | None = None


class RegisterDeviceResponse(BaseModel):
    device_id: str
    registered_at: str


class NotificationCategories(BaseModel):
    release_alerts: bool | None = None
    taste_push: bool | None = None
    system: bool | None = None


class UpdateNotificationsRequest(BaseModel):
    categories: NotificationCategories


class UpdateNotificationsResponse(BaseModel):
    categories: dict
    updated_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/devices", response_model=RegisterDeviceResponse, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: RegisterDeviceRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> RegisterDeviceResponse:
    """APNs 토큰 등록. 동일 토큰은 upsert."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.devices (user_id, apns_token, platform, app_version, device_model)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, apns_token) DO UPDATE SET
                platform     = EXCLUDED.platform,
                app_version  = EXCLUDED.app_version,
                device_model = EXCLUDED.device_model,
                registered_at = now()
            RETURNING device_id, registered_at
            """,
            (user_id, body.apns_token, body.platform, body.app_version, body.device_model),
        )
        row = await cur.fetchone()

    return RegisterDeviceResponse(device_id=str(row[0]), registered_at=row[1].isoformat())


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

    settings = row[0] if row and row[0] else {"release_alerts": True, "taste_push": True, "system": True}
    updated_at = row[1].isoformat() if row and row[1] else ""

    return UpdateNotificationsResponse(categories=settings, updated_at=updated_at)
