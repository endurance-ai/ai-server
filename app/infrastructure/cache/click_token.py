"""Redis-backed click token for CTR measurement (beta observability).

Mints short, unguessable tokens that wrap outbound product URLs through the
redirect proxy (`GET /r/{token}`). Tap → proxy looks up token → emits
`card_clicked` → 302 to product_url. Without this layer, AI server never sees
the click (Telegram opens external URL directly from user's device).

Keyspace:
- `kiko:click:{token}` (TTL 24h, JSON value): `{chat_id, rec_id, product_id,
  product_url}`. TTL chosen to outlive any reasonable tap delay; tokens are
  one-shot in spirit but multi-tap-safe (lookup doesn't delete).

All helpers fail-open: redis unavailability NEVER blocks card delivery.
`mint_token` returns None on failure → caller falls back to raw product_url.
`lookup_token` returns None on miss/error → redirect endpoint returns 404.

Multi-worker safe via the shared Redis instance (each worker has its own
asyncio client pool).
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Final

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_CLICK_TTL_SECONDS: Final[int] = 86_400  # 24h
_TOKEN_BYTES: Final[int] = 12  # 96-bit entropy → 16-char base64-urlsafe

_pool: Redis | None = None


def _click_key(token: str) -> str:
    return f"kiko:click:{token}"


def _get_client() -> Redis | None:
    """Lazy singleton client. Fail-open: returns None on creation error."""
    global _pool
    if _pool is not None:
        return _pool
    try:
        _pool = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("[click_token] pool create failed: %s", type(exc).__name__)
        _pool = None
    return _pool


async def mint_token(
    chat_id: int,
    rec_id: str | None,
    product_id: str,
    product_url: str,
) -> str | None:
    """Mint a short token, write `kiko:click:{token}` with TTL 24h.

    Returns the token on success, None on Redis failure (fail-open — caller
    falls back to raw product_url so the card still works).
    """
    client = _get_client()
    if client is None:
        return None
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    payload = json.dumps(
        {
            "chat_id": int(chat_id),
            "rec_id": rec_id,
            "product_id": str(product_id),
            "product_url": str(product_url),
        }
    )
    try:
        await client.setex(_click_key(token), _CLICK_TTL_SECONDS, payload)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("[click_token] setex failed: %s", type(exc).__name__)
        return None
    return token


async def lookup_token(token: str) -> dict[str, Any] | None:
    """Look up a token. Returns parsed dict on hit, None on miss/error."""
    if not token:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(_click_key(token))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("[click_token] get failed: %s", type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[click_token] json decode failed: %s", type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None
