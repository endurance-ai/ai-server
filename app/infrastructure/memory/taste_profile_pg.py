"""PostgresTasteProfileStore — SPEC-MEMORY-001.

Postgres-backed implementation of the `TasteProfileStore` Protocol from
`app.channels.taste_profile`. Persists weighted dicts as JSONB so float64
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

    def seed_from_onboarding(self, user_key: str, weights: dict[str, float]) -> None:
        """Additive merge into `liked_keywords` for onboarding seed.

        Same contract as InMemory: per-keyword cap, additive only, NEVER overwrite.
        Uses `jsonb_set` via load → merge → persist so we do NOT compound the 0.9
        decay across unrelated keywords (one `seed_from_onboarding` call should
        affect ONLY the keys being seeded). Implementation choice: load the
        profile, add `min(weight, cap)` per key in-process, then re-persist.
        `GREATEST(current, seed)` was considered but rejected — additive lets a
        keyword that appears in BOTH cards AND Pinterest accumulate naturally
        (still capped per-call).

        @MX:SPEC: SPEC-ONBOARD-CARDS-001
        """
        run_in_pool_loop(_aseed_from_onboarding(user_key, weights))

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
                      last_active
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
                last_active, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_key) DO UPDATE SET
                liked_brands       = EXCLUDED.liked_brands,
                disliked_brands    = EXCLUDED.disliked_brands,
                liked_keywords     = EXCLUDED.liked_keywords,
                disliked_keywords  = EXCLUDED.disliked_keywords,
                price_min_observed = EXCLUDED.price_min_observed,
                price_max_observed = EXCLUDED.price_max_observed,
                last_active        = EXCLUDED.last_active,
                updated_at         = EXCLUDED.updated_at
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
            ),
        )
        await conn.commit()


@observe(name="memory.taste.seed_from_onboarding", as_type="span")
async def _aseed_from_onboarding(user_key: str, weights: dict[str, float]) -> None:
    """Load → merge → persist, atomic under the per-key asyncio.Lock that
    callers hold via `lock_for(user_key)` (REQ-MEMORY-PROTOCOL-001)."""
    if not weights:
        return
    from app.core.config import settings as _settings

    cap = float(getattr(_settings, "ONBOARDING_SEED_MAX_WEIGHT", 0.7))
    profile = await _aget_or_create(user_key)
    mutated = False
    for raw_kw, raw_w in weights.items():
        if not raw_kw:
            continue
        try:
            w = float(raw_w)
        except (TypeError, ValueError):
            continue
        if w <= 0.0:
            continue
        kw = raw_kw.strip().lower()
        if not kw:
            continue
        applied = min(w, cap)
        profile.liked_keywords[kw] = profile.liked_keywords.get(kw, 0.0) + applied
        profile.disliked_keywords.pop(kw, None)
        mutated = True
    if not mutated:
        return
    profile._cap()
    await _aupdate(profile)


async def _adelete(user_key: str) -> None:
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM ai.user_taste_profile WHERE user_key = %s", (user_key,))
        await conn.commit()


def _row_to_profile(row: Any) -> TasteProfile:
    (
        user_key,
        liked_brands,
        disliked_brands,
        liked_keywords,
        disliked_keywords,
        price_min,
        price_max,
        last_active,
    ) = row
    return TasteProfile(
        user_key=user_key,
        liked_brands=dict(liked_brands or {}),
        disliked_brands=dict(disliked_brands or {}),
        liked_keywords=dict(liked_keywords or {}),
        disliked_keywords=dict(disliked_keywords or {}),
        price_min_observed=price_min,
        price_max_observed=price_max,
        last_active=_dt_to_ts(last_active) if last_active else 0.0,
    )
