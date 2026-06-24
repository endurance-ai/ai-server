"""JWT issuance and verification for consumer auth.

Issues short-lived access tokens (HS256) and long-lived refresh tokens.
Refresh tokens are stored as SHA-256 hashes in ai.refresh_tokens for revocation.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from jose import JWTError, jwt

from app.core.config import settings

_ALGORITHM = "HS256"


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(user_id: UUID) -> str:
    expire = _now() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "type": "access", "exp": expire},
        settings.JWT_SECRET,
        algorithm=_ALGORITHM,
    )


def create_refresh_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash).

    Caller stores hash in DB; client receives raw token.
    """
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


def verify_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access token. Raises HTTP 401 on failure."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token type mismatch",
        )
    return payload


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def refresh_token_expiry() -> datetime:
    return _now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
