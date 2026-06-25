"""Consumer social login API (SPEC-AUTH-SOCIAL-001).

POST /v1/auth/social   — verify Google/Apple id_token, issue JWT pair
POST /v1/auth/refresh  — issue new access_token from refresh_token
POST /v1/auth/logout   — revoke refresh_token (logout)
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.core import jwt as jwt_utils
from app.core.di import provide_db_pool
from app.core.social_auth.apple import AppleClaims, verify_apple_token
from app.core.social_auth.google import GoogleClaims, verify_google_token

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=True)


# ── Request / Response schemas ────────────────────────────────────────────────


class SocialLoginRequest(BaseModel):
    provider: str  # "google" | "apple"
    id_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _upsert_user(
    pool: AsyncConnectionPool,
    provider: str,
    provider_id: str,
    email: str | None,
    display_name: str | None,
    avatar_url: str | None,
) -> UUID:
    """Insert or update user_profiles. Returns user_id."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_profiles
                (provider, provider_id, email, display_name, avatar_url)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (provider, provider_id) DO UPDATE SET
                email        = COALESCE(EXCLUDED.email, ai.user_profiles.email),
                display_name = COALESCE(EXCLUDED.display_name, ai.user_profiles.display_name),
                avatar_url   = COALESCE(EXCLUDED.avatar_url, ai.user_profiles.avatar_url),
                updated_at   = now()
            RETURNING user_id
            """,
            (provider, provider_id, email, display_name, avatar_url),
        )
        row = await cur.fetchone()
    return row[0]


async def _store_refresh_token(
    pool: AsyncConnectionPool,
    user_id: UUID,
    token_hash: str,
) -> None:
    expiry = jwt_utils.refresh_token_expiry()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.refresh_tokens (user_id, token_hash, expires_at)
            VALUES (%s, %s, %s)
            """,
            (user_id, token_hash, expiry),
        )


async def _lookup_refresh_token(
    pool: AsyncConnectionPool,
    token_hash: str,
) -> UUID | None:
    """Return user_id if token is valid and not revoked, else None."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user_id FROM ai.refresh_tokens
            WHERE token_hash = %s
              AND revoked_at IS NULL
              AND expires_at > now()
            """,
            (token_hash,),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def _revoke_refresh_token(pool: AsyncConnectionPool, token_hash: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE ai.refresh_tokens SET revoked_at = now() WHERE token_hash = %s",
            (token_hash,),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/social", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def social_login(
    body: SocialLoginRequest,
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> TokenResponse:
    """Verify Google or Apple id_token, upsert user_profiles, issue JWT pair."""
    if body.provider == "google":
        claims: GoogleClaims | AppleClaims = verify_google_token(body.id_token)
        user_id = await _upsert_user(
            pool,
            "google",
            claims.sub,
            claims.email,
            claims.name,
            claims.picture,  # type: ignore[attr-defined]
        )
    elif body.provider == "apple":
        claims = verify_apple_token(body.id_token)
        user_id = await _upsert_user(pool, "apple", claims.sub, claims.email, None, None)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider: {body.provider}",
        )

    access_token = jwt_utils.create_access_token(user_id)
    raw_refresh, token_hash = jwt_utils.create_refresh_token()
    await _store_refresh_token(pool, user_id, token_hash)

    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        user_id=str(user_id),
    )


@router.post("/refresh", response_model=AccessTokenResponse, status_code=status.HTTP_200_OK)
async def refresh_token(
    body: RefreshRequest,
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> AccessTokenResponse:
    """Issue a new access_token from a valid refresh_token."""
    token_hash = jwt_utils.hash_refresh_token(body.refresh_token)
    user_id = await _lookup_refresh_token(pool, token_hash)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token invalid or expired",
        )
    return AccessTokenResponse(access_token=jwt_utils.create_access_token(user_id))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    body: RefreshRequest,
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> None:
    """Revoke a refresh_token (logout)."""
    token_hash = jwt_utils.hash_refresh_token(body.refresh_token)
    await _revoke_refresh_token(pool, token_hash)
