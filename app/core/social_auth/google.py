"""Google ID token verification.

Uses google-auth to verify tokens issued by Google Sign-In (iOS SDK).
The id_token comes from the iOS client; we verify it server-side against
the GOOGLE_CLIENT_ID (iOS OAuth 2.0 client, not web client).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class GoogleClaims:
    sub: str
    email: str | None
    name: str | None
    picture: str | None


def verify_google_token(token: str) -> GoogleClaims:
    """Verify a Google ID token and return extracted claims.

    Raises HTTP 401 on any verification failure.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Sign-In not configured",
        )
    try:
        payload = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google ID token",
        ) from exc

    return GoogleClaims(
        sub=payload["sub"],
        email=payload.get("email"),
        name=payload.get("name"),
        picture=payload.get("picture"),
    )
