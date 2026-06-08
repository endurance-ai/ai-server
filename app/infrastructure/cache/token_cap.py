"""SPEC-DAILY-TOKEN-CAP-001 — Redis-backed per-user daily token cap.

Key: `kiko:cap:{chat_id}` (int counter, INCR).
TTL: seconds until next KST midnight (auto-reset daily).

All helpers are fail-open: Redis unavailability never blocks message handling.

@MX:ANCHOR: [AUTO] sole owner of daily token cap Redis surface
@MX:REASON: fail-open uniformity — callers never need try/except; swallow is centralized here.
@MX:SPEC: SPEC-DAILY-TOKEN-CAP-001
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Final

from app.core.config import settings

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_CAP_KEY_PREFIX: Final[str] = "kiko:cap:"


def _cap_key(chat_id: int) -> str:
    return f"{_CAP_KEY_PREFIX}{int(chat_id)}"


def _seconds_until_kst_midnight() -> int:
    """Return the number of seconds from now until next KST 00:00:00."""
    now_kst = datetime.now(_KST)
    next_midnight = (now_kst + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((next_midnight - now_kst).total_seconds()))


def _get_client():
    """Return the shared Redis client from chat_state module (lazy, fail-open)."""
    try:
        from app.infrastructure.cache.chat_state import _get_client as _base_get_client

        return _base_get_client()
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap: redis client unavailable: %s", type(exc).__name__)
        return None


async def is_over_limit(chat_id: int) -> bool:
    """Return True if chat_id has exceeded the daily token cap.

    Fail-open → False (let the request through) when:
    - cap feature is disabled
    - Redis is unavailable
    - any unexpected error
    """
    if not settings.DAILY_TOKEN_CAP_ENABLED:
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        raw = await client.get(_cap_key(chat_id))
        if raw is None:
            return False
        return int(raw) >= settings.DAILY_TOKEN_CAP
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap.is_over_limit fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return False


async def increment(chat_id: int, tokens: int) -> int:
    """Add `tokens` to chat_id's daily counter. Returns new total. Fail-open → 0.

    Sets TTL to seconds until next KST midnight on first write (SETNX pattern via
    pipeline: INCRBY + EXPIREAT if key is new).
    """
    if not settings.DAILY_TOKEN_CAP_ENABLED or tokens <= 0:
        return 0
    client = _get_client()
    if client is None:
        return 0
    key = _cap_key(chat_id)
    ttl = _seconds_until_kst_midnight()
    try:
        pipe = client.pipeline()
        pipe.incrby(key, int(tokens))
        pipe.expire(key, ttl, nx=True)  # Set TTL only if not already set
        results = await pipe.execute()
        new_total = int(results[0])
        logger.debug("token_cap.increment chat=%s +%d → %d (ttl=%ds)", chat_id, tokens, new_total, ttl)
        return new_total
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap.increment fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return 0


async def get_usage(chat_id: int) -> int:
    """Return current daily token usage for chat_id. Fail-open → 0."""
    client = _get_client()
    if client is None:
        return 0
    try:
        raw = await client.get(_cap_key(chat_id))
        return int(raw) if raw is not None else 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap.get_usage fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return 0
