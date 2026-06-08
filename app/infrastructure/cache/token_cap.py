"""SPEC-DAILY-TOKEN-CAP-001 — Redis-backed per-user daily token cap with tier support.

Tiers (stored at kiko:tier:{chat_id}, no TTL):
  free       → CAP_TIER_FREE (default, 200K)
  standard   → CAP_TIER_STANDARD (500K)
  pro        → CAP_TIER_PRO (1M)
  developer  → CAP_TIER_DEVELOPER (0 = unlimited)

Daily counter key: kiko:cap:{chat_id}  (int, TTL = seconds until KST midnight)

All helpers are fail-open: Redis unavailability never blocks message handling.

@MX:ANCHOR: [AUTO] sole owner of daily token cap + user tier Redis surface
@MX:REASON: fail-open uniformity — callers never need try/except; swallow is centralized here.
@MX:SPEC: SPEC-DAILY-TOKEN-CAP-001
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Final, Literal

from app.core.config import settings

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_CAP_KEY_PREFIX: Final[str] = "kiko:cap:"
_TIER_KEY_PREFIX: Final[str] = "kiko:tier:"

UserTier = Literal["free", "standard", "pro", "developer"]
_VALID_TIERS: frozenset[str] = frozenset({"free", "standard", "pro", "developer"})
_DEFAULT_TIER: UserTier = "free"


def _cap_key(chat_id: int) -> str:
    return f"{_CAP_KEY_PREFIX}{int(chat_id)}"


def _tier_key(chat_id: int) -> str:
    return f"{_TIER_KEY_PREFIX}{int(chat_id)}"


def _seconds_until_kst_midnight() -> int:
    """Return the number of seconds from now until next KST 00:00:00."""
    now_kst = datetime.now(_KST)
    next_midnight = (now_kst + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((next_midnight - now_kst).total_seconds()))


def _tier_cap(tier: str) -> int:
    """Return the daily token cap for `tier`. 0 means unlimited.

    CAP_TIER_* settings are canonical. Unknown tiers fall back to free.
    """
    if tier == "standard":
        return settings.CAP_TIER_STANDARD
    if tier == "pro":
        return settings.CAP_TIER_PRO
    if tier == "developer":
        return settings.CAP_TIER_DEVELOPER
    return settings.effective_free_cap  # free + unknown tiers


def _get_client():
    """Return the shared Redis client from chat_state module (lazy, fail-open)."""
    try:
        from app.infrastructure.cache.chat_state import _get_client as _base_get_client

        return _base_get_client()
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap: redis client unavailable: %s", type(exc).__name__)
        return None


# ── Tier management ───────────────────────────────────────────────────────────


async def get_user_tier(chat_id: int) -> UserTier:
    """Return the tier for chat_id. Fail-open → 'free'."""
    client = _get_client()
    if client is None:
        return _DEFAULT_TIER
    try:
        raw = await client.get(_tier_key(chat_id))
        if raw and raw in _VALID_TIERS:
            return raw  # type: ignore[return-value]
        return _DEFAULT_TIER
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap.get_user_tier fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return _DEFAULT_TIER


async def set_user_tier(chat_id: int, tier: UserTier) -> bool:
    """Set the tier for chat_id (permanent, no TTL). Returns True on success."""
    if tier not in _VALID_TIERS:
        logger.warning("token_cap.set_user_tier: invalid tier=%r chat=%s", tier, chat_id)
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        await client.set(_tier_key(chat_id), tier)
        logger.info("token_cap.set_user_tier chat=%s tier=%s", chat_id, tier)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap.set_user_tier fail chat=%s: %s", chat_id, type(exc).__name__)
        return False


# ── Cap check & counter ───────────────────────────────────────────────────────


async def is_over_limit(chat_id: int) -> bool:
    """Return True if chat_id has exceeded their tier's daily token cap.

    Fail-open → False when:
    - cap feature is disabled
    - tier is developer (unlimited)
    - cap value is 0 (unlimited)
    - Redis is unavailable
    - any unexpected error
    """
    if not settings.DAILY_TOKEN_CAP_ENABLED:
        return False
    tier = await get_user_tier(chat_id)
    cap = _tier_cap(tier)
    if cap == 0:
        return False  # unlimited tier
    client = _get_client()
    if client is None:
        return False
    try:
        raw = await client.get(_cap_key(chat_id))
        if raw is None:
            return False
        return int(raw) >= cap
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap.is_over_limit fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return False


async def increment(chat_id: int, tokens: int) -> int:
    """Add `tokens` to chat_id's daily counter. Returns new total. Fail-open → 0.

    Sets TTL to seconds until next KST midnight on first write (EXPIRE NX).
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


async def reset_usage(chat_id: int) -> bool:
    """Delete the daily counter for chat_id (admin reset). Fail-open → False."""
    client = _get_client()
    if client is None:
        return False
    try:
        await client.delete(_cap_key(chat_id))
        logger.info("token_cap.reset_usage chat=%s", chat_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_cap.reset_usage fail chat=%s: %s", chat_id, type(exc).__name__)
        return False
