"""User profile API.

GET    /v1/me  — current user's profile
PATCH  /v1/me  — update display_name and/or gender
DELETE /v1/me  — delete account (cascade, requires confirm:true)
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, field_validator

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["me"])

_VALID_GENDERS = {"male", "female", "other"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class UserProfileResponse(BaseModel):
    user_id: str
    provider: str
    email: str | None
    display_name: str | None
    avatar_url: str | None
    gender: str | None
    tier: str
    tier_expires_at: str | None
    created_at: str


class UserPatchRequest(BaseModel):
    display_name: str | None = None
    gender: str | None = None

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_GENDERS:
            raise ValueError(f"gender must be one of {_VALID_GENDERS}")
        return v


class UserPatchResponse(BaseModel):
    user_id: str
    display_name: str | None
    gender: str | None


class DeleteMeRequest(BaseModel):
    confirm: bool
    reason: str | None = None


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _get_profile(pool: AsyncConnectionPool, user_id: UUID) -> dict | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user_id, provider, email, display_name, avatar_url,
                   gender, tier, tier_expires_at, created_at
            FROM ai.user_profiles
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "user_id": str(row[0]),
        "provider": row[1],
        "email": row[2],
        "display_name": row[3],
        "avatar_url": row[4],
        "gender": row[5],
        "tier": row[6],
        "tier_expires_at": row[7].isoformat() if row[7] else None,
        "created_at": row[8].isoformat() if isinstance(row[8], datetime) else str(row[8]),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/me", response_model=UserProfileResponse, status_code=status.HTTP_200_OK)
async def get_me(
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> UserProfileResponse:
    """Return the current user's profile."""
    profile = await _get_profile(pool, user_id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserProfileResponse(**profile)


@router.patch("/me", response_model=UserPatchResponse, status_code=status.HTTP_200_OK)
async def update_me(
    body: UserPatchRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> UserPatchResponse:
    """Update display_name and/or gender. At least one field required."""
    if body.display_name is None and body.gender is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one of display_name or gender must be provided",
        )

    fields: list[str] = []
    values: list = []
    if body.display_name is not None:
        fields.append("display_name = %s")
        values.append(body.display_name)
    if body.gender is not None:
        fields.append("gender = %s")
        values.append(body.gender)
    fields.append("updated_at = now()")
    values.append(user_id)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"UPDATE ai.user_profiles SET {', '.join(fields)} WHERE user_id = %s "  # noqa: S608
            "RETURNING user_id, display_name, gender",
            values,
        )
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserPatchResponse(user_id=str(row[0]), display_name=row[1], gender=row[2])


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    body: DeleteMeRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> None:
    """Delete account and all associated data. Requires confirm:true."""
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="confirm must be true to delete account",
        )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM ai.user_profiles WHERE user_id = %s RETURNING user_id",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
