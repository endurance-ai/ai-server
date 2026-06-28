"""Image upload reservation API.

POST /v1/uploads issues a short-lived S3 presigned PUT URL. The client uploads
directly to S3 and uses the returned CloudFront URL as the final image URL.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, field_validator

from app.api.deps import get_current_user_id
from app.core.config import Settings
from app.core.di import provide_db_pool, provide_settings
from app.services.upload_signer import missing_upload_config, presign_s3_put_url, public_image_url

router = APIRouter(prefix="/v1", tags=["uploads"])

_CONTENT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class CreateUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    size_bytes: int = Field(gt=0)

    @field_validator("filename")
    @classmethod
    def filename_must_be_plain(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or "/" in cleaned or "\\" in cleaned:
            raise ValueError("filename must be a plain file name")
        return cleaned

    @field_validator("content_type")
    @classmethod
    def content_type_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _CONTENT_TYPES:
            raise ValueError("content_type must be image/jpeg, image/png, or image/webp")
        return normalized


class CreateUploadResponse(BaseModel):
    upload_id: str
    upload_url: str
    image_url: str
    expires_at: str
    max_size_bytes: int


@router.post("/uploads", response_model=CreateUploadResponse, status_code=status.HTTP_200_OK)
async def create_upload(
    body: CreateUploadRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
    settings: Settings = Depends(provide_settings),
) -> CreateUploadResponse:
    max_size = int(settings.UPLOADS_MAX_SIZE_BYTES)
    if body.size_bytes > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="upload_too_large",
        )

    missing = missing_upload_config(settings)
    if missing:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="upload_storage_not_configured")

    upload_id = uuid4()
    object_key = _build_object_key(settings.UPLOADS_KEY_PREFIX, user_id, upload_id, body.filename, body.content_type)
    expires_seconds = max(60, min(int(settings.UPLOADS_PRESIGN_EXPIRES_SECONDS), 3600))
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=expires_seconds)
    upload_url = presign_s3_put_url(
        settings=settings,
        object_key=object_key,
        content_type=body.content_type,
        expires_seconds=expires_seconds,
        now=now,
    )
    image_url = public_image_url(settings, object_key)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.uploads (
                upload_id, user_id, object_key, filename, content_type,
                size_bytes, image_url, status, expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)
            """,
            (upload_id, user_id, object_key, body.filename, body.content_type, body.size_bytes, image_url, expires_at),
        )
        await conn.commit()

    return CreateUploadResponse(
        upload_id=str(upload_id),
        upload_url=upload_url,
        image_url=image_url,
        expires_at=expires_at.isoformat(),
        max_size_bytes=max_size,
    )


def _build_object_key(prefix: str, user_id: UUID, upload_id: UUID, filename: str, content_type: str) -> str:
    clean_prefix = prefix.strip().strip("/") or "uploads"
    stem = filename.rsplit(".", 1)[0]
    safe_stem = _SAFE_FILENAME_RE.sub("-", stem).strip(".-_")[:80] or "image"
    return f"{clean_prefix}/{user_id}/{upload_id}/{safe_stem}{_CONTENT_TYPES[content_type]}"
