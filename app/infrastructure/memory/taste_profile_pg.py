"""PostgresTasteProfileStore — SPEC-MEMORY-001.

Postgres-backed implementation of the `TasteProfileStore` Protocol from
`app.infrastructure.memory.taste_profile`. Persists weighted dicts as JSONB so float64
precision round-trips. `last_active` is stored as `timestamptz(6)` and
coerced float ↔ datetime at the boundary (REQ-MEMORY-PERSIST-002).

The Protocol surface is sync; this store routes async psycopg work to the
dedicated pool loop via `app.providers.db_pool.run_in_pool_loop`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from app.infrastructure.memory.taste_profile import TasteProfile
from app.observability.langfuse import observe
from app.providers.db_pool import get_pool, run_in_pool_loop

logger = logging.getLogger(__name__)


def _ts_to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def _dt_to_ts(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# @MX:ANCHOR: [AUTO] PostgresTasteProfileStore implements TasteProfileStore Protocol
# @MX:REASON: every LangGraph node that touches taste preferences resolves through get_taste_store()
class PostgresTasteProfileStore:
    """Persistent taste profile store backed by Postgres JSONB."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, user_key: str) -> asyncio.Lock:
        lock = self._locks.get(user_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_key] = lock
        return lock

    def get_or_create(self, user_key: str) -> TasteProfile:
        return run_in_pool_loop(_aget_or_create(user_key))

    def update(self, profile: TasteProfile) -> None:
        run_in_pool_loop(_aupdate(profile))

    def delete(self, user_key: str) -> None:
        run_in_pool_loop(_adelete(user_key))

    async def start(self) -> None:
        # No background tasks — stale-but-non-evicted policy (REQ-MEMORY-PERSIST-003).
        return None

    async def stop(self) -> None:
        return None


@observe(name="memory.taste.get_or_create", as_type="span")
async def _aget_or_create(user_key: str) -> TasteProfile:
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_taste_profile (user_key) VALUES (%s)
            ON CONFLICT (user_key) DO UPDATE
              SET user_key = EXCLUDED.user_key
            RETURNING user_key, liked_brands, disliked_brands, liked_keywords,
                      disliked_keywords, price_min_observed, price_max_observed,
                      last_active, disliked_brands_ts, disliked_keywords_ts, gender
            """,
            (user_key,),
        )
        row = await cur.fetchone()
        await conn.commit()
    return _row_to_profile(row)


@observe(name="memory.taste.update", as_type="span")
async def _aupdate(profile: TasteProfile) -> None:
    last_active_dt = _ts_to_dt(profile.last_active or datetime.now(tz=UTC).timestamp())
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_taste_profile (
                user_key, liked_brands, disliked_brands,
                liked_keywords, disliked_keywords,
                price_min_observed, price_max_observed,
                last_active, updated_at,
                disliked_brands_ts, disliked_keywords_ts, gender
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_key) DO UPDATE SET
                liked_brands         = EXCLUDED.liked_brands,
                disliked_brands      = EXCLUDED.disliked_brands,
                liked_keywords       = EXCLUDED.liked_keywords,
                disliked_keywords    = EXCLUDED.disliked_keywords,
                price_min_observed   = EXCLUDED.price_min_observed,
                price_max_observed   = EXCLUDED.price_max_observed,
                last_active          = EXCLUDED.last_active,
                updated_at           = EXCLUDED.updated_at,
                disliked_brands_ts   = EXCLUDED.disliked_brands_ts,
                disliked_keywords_ts = EXCLUDED.disliked_keywords_ts,
                gender               = EXCLUDED.gender
            """,
            (
                profile.user_key,
                Jsonb(profile.liked_brands),
                Jsonb(profile.disliked_brands),
                Jsonb(profile.liked_keywords),
                Jsonb(profile.disliked_keywords),
                profile.price_min_observed,
                profile.price_max_observed,
                last_active_dt,
                last_active_dt,
                Jsonb(profile.disliked_brands_ts),
                Jsonb(profile.disliked_keywords_ts),
                profile.gender,
            ),
        )
        await conn.commit()


async def _adelete(user_key: str) -> None:
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM ai.user_taste_profile WHERE user_key = %s", (user_key,))
        await conn.commit()


def _row_to_profile(row: Any) -> TasteProfile:
    # SPEC-AGENT-V3-REACT Gap4 — +2 trailing positions with backward-compat
    # default ({}) so a (defensively short) pre-migration tuple still loads
    # without IndexError. The migration default '{}' is the primary guarantee;
    # this slice is the belt-and-braces fallback (REQ-AGENT-V3-DISLIKE-SCHEMA-001).
    row = list(row)
    (
        user_key,
        liked_brands,
        disliked_brands,
        liked_keywords,
        disliked_keywords,
        price_min,
        price_max,
        last_active,
    ) = row[:8]
    disliked_brands_ts = row[8] if len(row) > 8 else {}
    disliked_keywords_ts = row[9] if len(row) > 9 else {}
    # SPEC-GENDER-PIN-001 — +1 trailing position, backward-compat default None.
    gender = row[10] if len(row) > 10 else None
    return TasteProfile(
        user_key=user_key,
        liked_brands=dict(liked_brands or {}),
        disliked_brands=dict(disliked_brands or {}),
        liked_keywords=dict(liked_keywords or {}),
        disliked_keywords=dict(disliked_keywords or {}),
        price_min_observed=price_min,
        price_max_observed=price_max,
        last_active=_dt_to_ts(last_active) if last_active else 0.0,
        disliked_brands_ts=dict(disliked_brands_ts or {}),
        disliked_keywords_ts=dict(disliked_keywords_ts or {}),
        gender=gender,
    )
