"""Redis-backed chat state — pager cursor + impression dedupe.

SPEC-CHAT-STATE-REDIS-001 REQ-CHAT-STATE-001..004.

Two invariants protected:
- `kiko:cursor:{chat_id}` (TTL 24h, int value): next-page start index for
  "더보기 / More" pager. Replaces the prior in-process pager-cursor dict in
  `app/agents/tools/respond.py`.
- `kiko:imp:{chat_id}` (TTL 7d, SET of product_ids): per-chat impression
  dedupe set. Replaces the prior in-process impression-dedupe dict.

All helpers fail-open (REQ-CHAT-STATE-003): redis unavailability NEVER blocks
card delivery. Failures are swallowed with a single `logger.debug` line and a
safe default (cursor=0, is_logged=False, write/del=no-op).

Single Redis pool instance (module-level, lazy init). Created on first call
or via `warm_pool()` from `app.main` lifespan startup. Multi-worker safe
because each worker has its own pool and Redis itself is the single source.

@MX:ANCHOR: [AUTO] sole owner of redis chat-state surface — every cursor /
  impression-dedupe operation goes through these 5 helpers. No direct
  redis-py calls from `respond.py` or any other caller.
@MX:REASON: SPEC-CHAT-STATE-REDIS-001 REQ-CHAT-STATE-003 fail-open uniformity
  — caller passes through without try/except; swallow is centralized here.
@MX:SPEC: SPEC-CHAT-STATE-REDIS-001
"""

from __future__ import annotations

import logging
from typing import Any, Final

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_CURSOR_TTL_SECONDS: Final[int] = 86_400  # 24h
_IMP_TTL_SECONDS: Final[int] = 604_800  # 7d
_CAP_SHOWN_TTL_SECONDS: Final[int] = 21_600  # 6h — full-box cooldown
_CAP_SILENCED_TTL_SECONDS: Final[int] = 86_400  # 24h — explicit-reject silence
_CAP_SEEN_TTL_SECONDS: Final[int] = 129_600  # 36h — covers midnight reset for any hit-time

# Module-level singleton client. Lazy: created on first helper call or via
# `warm_pool()`. Reset to None by `close_pool()` (lifespan shutdown) or by
# test fixtures (monkeypatch to a fakeredis instance).
_pool: Redis | None = None


def _cursor_key(chat_id: int) -> str:
    return f"kiko:cursor:{int(chat_id)}"


def _imp_key(chat_id: int) -> str:
    return f"kiko:imp:{int(chat_id)}"


def _cap_shown_key(chat_id: int) -> str:
    return f"kiko:cap_shown:{int(chat_id)}"


def _cap_silenced_key(chat_id: int) -> str:
    return f"kiko:cap_silenced:{int(chat_id)}"


def _cap_seen_key(chat_id: int) -> str:
    return f"kiko:cap_seen:{int(chat_id)}"


def _get_client() -> Redis | None:
    """Return the module-level singleton client, lazy-creating it on first call.

    Fail-open: returns None on any creation error. asyncio single-event-loop
    assumption means no lock is needed for the `is None` check + assignment.
    """
    global _pool
    if _pool is not None:
        return _pool
    try:
        # `decode_responses=True` keeps cursor int parsing simple and SET
        # membership returns str (matching product_id str dtype).
        _pool = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("redis pool create failed: %s", type(exc).__name__)
        _pool = None
    return _pool


def _mask_url(url: str) -> str:
    """Mask the auth portion of a redis URL for logging."""
    if "@" not in url:
        return url
    try:
        scheme, rest = url.split("://", 1)
        _auth, host = rest.split("@", 1)
        return f"{scheme}://****@{host}"
    except Exception:  # noqa: BLE001 — masking is best-effort
        return "redis://****"


async def warm_pool() -> bool:
    """Lifespan startup hook — try a PING, return True on success.

    Fail-open: logs at INFO and returns False on failure. The pool stays
    initialized (or stays None on create failure); subsequent helper calls
    re-attempt lazily.
    """
    client = _get_client()
    if client is None:
        logger.info("redis warm skipped (fail-open): pool create returned None")
        return False
    try:
        ok = await client.ping()
        if ok:
            logger.info("redis pool warmed (url=%s)", _mask_url(settings.REDIS_URL))
            return True
        logger.info("redis warm skipped (fail-open): PING returned falsy")
        return False
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.info("redis warm skipped (fail-open): %s", type(exc).__name__)
        return False


async def close_pool() -> None:
    """Lifespan shutdown hook — close the pool. Fail-open."""
    global _pool
    if _pool is None:
        return
    try:
        close = getattr(_pool, "aclose", None) or getattr(_pool, "close", None)
        if close is not None:
            result = close()
            # Support both sync (FakeRedis) and async close paths.
            if hasattr(result, "__await__"):
                await result
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("redis pool close failed: %s", type(exc).__name__)
    _pool = None


# --- Pager cursor (REQ-CHAT-STATE-001) ---


async def get_cursor(chat_id: int) -> int:
    """Return the next-page start index for `chat_id`. Fail-open → 0.

    Byte-identical fallback to the prior in-process pager-cursor `dict.get(chat_id, 0)`:
    a redis miss / failure / unparseable value all collapse to 0 (start from
    the first page), matching the dict-miss semantics callers already tolerate.
    """
    client = _get_client()
    if client is None:
        return 0
    try:
        raw: Any = await client.get(_cursor_key(chat_id))
        if raw is None:
            return 0
        return int(raw)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("get_cursor fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return 0


async def set_cursor(chat_id: int, n: int) -> None:
    """Set the next-page start index for `chat_id` with TTL 24h. Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(_cursor_key(chat_id), int(n), ex=_CURSOR_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("set_cursor fail-open chat=%s: %s", chat_id, type(exc).__name__)


# --- Impression dedupe (REQ-CHAT-STATE-002) ---


async def is_logged(chat_id: int, product_id: str) -> bool:
    """Return True iff `product_id` is already in the dedupe set. Fail-open → False.

    Empty / falsy `product_id` short-circuits to False (no key probed). Mirrors
    the caller's `if pid is None: pass through` guard — id-less candidates are
    never deduped, never key-collide.
    """
    if not product_id:
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(await client.sismember(_imp_key(chat_id), str(product_id)))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug(
            "is_logged fail-open chat=%s pid=%s: %s",
            chat_id,
            product_id,
            type(exc).__name__,
        )
        return False


async def mark_logged(chat_id: int, product_id: str) -> None:
    """Add `product_id` to the dedupe set + extend TTL 7d. Fail-open.

    Empty / falsy `product_id` is a no-op (no key created). EXPIRE on every
    SADD is cheap + idempotent; avoids EXPIRE NX dependency on redis 7+.
    """
    if not product_id:
        return
    client = _get_client()
    if client is None:
        return
    key = _imp_key(chat_id)
    try:
        await client.sadd(key, str(product_id))
        await client.expire(key, _IMP_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug(
            "mark_logged fail-open chat=%s pid=%s: %s",
            chat_id,
            product_id,
            type(exc).__name__,
        )


async def clear_logged(chat_id: int) -> None:
    """Drop the dedupe set for `chat_id` (new search → fresh trace binding). Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.delete(_imp_key(chat_id))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("clear_logged fail-open chat=%s: %s", chat_id, type(exc).__name__)


# --- Cap-box cooldown (option A) + explicit-reject silence (option B) ---


async def is_cap_shown(chat_id: int) -> bool:
    """True iff full cap-box was sent within the last 6h. Fail-open → False."""
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(await client.exists(_cap_shown_key(chat_id)))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("is_cap_shown fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return False


async def mark_cap_shown(chat_id: int) -> None:
    """Mark full cap-box as sent — suppresses re-fire for 6h. Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(_cap_shown_key(chat_id), "1", ex=_CAP_SHOWN_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("mark_cap_shown fail-open chat=%s: %s", chat_id, type(exc).__name__)


async def is_cap_silenced(chat_id: int) -> bool:
    """True iff user explicitly rejected the membership prompt within 24h. Fail-open → False."""
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(await client.exists(_cap_silenced_key(chat_id)))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("is_cap_silenced fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return False


async def mark_cap_silenced(chat_id: int) -> None:
    """Silence all cap-related responses for 24h (explicit user rejection). Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(_cap_silenced_key(chat_id), "1", ex=_CAP_SILENCED_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("mark_cap_silenced fail-open chat=%s: %s", chat_id, type(exc).__name__)


async def is_cap_seen(chat_id: int) -> bool:
    """True iff this chat hit the cap within the last 36h (used to fire the
    next-day welcome ack once they're back under the limit). Fail-open → False."""
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(await client.exists(_cap_seen_key(chat_id)))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("is_cap_seen fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return False


async def mark_cap_seen(chat_id: int) -> None:
    """Mark that this chat hit the cap — 36h TTL spans the midnight reset so
    the next-day welcome message fires exactly once on return. Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(_cap_seen_key(chat_id), "1", ex=_CAP_SEEN_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("mark_cap_seen fail-open chat=%s: %s", chat_id, type(exc).__name__)


async def clear_cap_seen(chat_id: int) -> None:
    """Drop the cap-seen flag (welcome message fired). Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.delete(_cap_seen_key(chat_id))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("clear_cap_seen fail-open chat=%s: %s", chat_id, type(exc).__name__)
