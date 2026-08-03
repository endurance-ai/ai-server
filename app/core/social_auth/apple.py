"""Apple ID token verification.

Apple uses JWKS (https://appleid.apple.com/auth/keys) to publish public keys.
We fetch the JWKS, select the matching key by `kid`, and verify the JWT locally.

Note: Apple only provides email on the *first* login. Subsequent logins have
email=None from the id_token; we preserve whatever was stored at first sign-in.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwk, jwt

from app.core.config import settings

_APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_APPLE_ISSUER = "https://appleid.apple.com"


@dataclass(frozen=True, slots=True)
class AppleClaims:
    sub: str
    email: str | None


def _fetch_apple_jwks() -> dict:
    response = httpx.get(_APPLE_JWKS_URL, timeout=10.0)
    response.raise_for_status()
    return response.json()


def verify_apple_token(token: str) -> AppleClaims:
    """Verify an Apple ID token and return extracted claims.

    Raises HTTP 401 on any verification failure.
    """
    if not settings.APPLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Apple Sign-In not configured",
        )

    try:
        # Decode header to pick the right key by kid
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        jwks_data = _fetch_apple_jwks()
        matching_keys = [k for k in jwks_data.get("keys", []) if k.get("kid") == kid]
        if not matching_keys:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Apple public key not found for kid",
            )

        public_key = jwk.construct(matching_keys[0])
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.APPLE_CLIENT_ID,
            issuer=_APPLE_ISSUER,
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Apple ID token",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Apple token verification failed",
        ) from exc

    return AppleClaims(
        sub=payload["sub"],
        email=payload.get("email"),
    )
