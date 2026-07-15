"""Shared FastAPI dependencies used across multiple routers."""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import jwt as jwt_utils

_bearer = HTTPBearer(auto_error=True)
_bearer_optional = HTTPBearer(auto_error=False)


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> UUID:
    """Extract and validate user_id from Bearer access_token."""
    payload = jwt_utils.verify_access_token(credentials.credentials)
    try:
        return UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        ) from exc


async def get_optional_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> UUID | None:
    """Like get_current_user_id, but anonymous/invalid tokens yield None.

    비로그인 허용 엔드포인트(GET /v1/curation)용 — 유효한 토큰이면 프로필
    기반 개인화, 아니면 게스트로 동작한다. 무효 토큰도 401 대신 게스트 취급
    (클라이언트가 만료 토큰을 들고 있어도 메인 화면은 열려야 함).
    """
    if credentials is None:
        return None
    try:
        payload = jwt_utils.verify_access_token(credentials.credentials)
        return UUID(payload["sub"])
    except (HTTPException, KeyError, ValueError):
        return None
