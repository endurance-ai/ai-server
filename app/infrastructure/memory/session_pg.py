"""PostgresSessionStore — SPEC-MEMORY-001.

Postgres-backed `SessionStore` implementation. Persists the full 23-field
`Session` dataclass as a row in `ai.user_session`. JSON-shaped fields go to
JSONB columns via `_to_jsonable` (REQ-MEMORY-SESSION-001 cascade).

Lazy TTL expiry is a single atomic statement per REQ-MEMORY-SESSION-002:
INSERT ... ON CONFLICT (chat_id) DO UPDATE SET <defaults>
  WHERE ai.user_session.ttl_expires_at < now()
which collapses concurrent expired reads to one row deterministically.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from psycopg.types.json import Jsonb

from app.channels._jsonable import to_jsonable as _to_jsonable
from app.core.config import settings
from app.infrastructure.memory.session import Session, SessionState
from app.observability.langfuse import observe
from app.providers.db_pool import get_pool, run_in_pool_loop

logger = logging.getLogger(__name__)


def _ts_to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def _dt_to_ts(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# @MX:ANCHOR: [AUTO] PostgresSessionStore implements SessionStore Protocol
# @MX:REASON: every LangGraph node calls get_store(); swap target for InMemorySessionStore
class PostgresSessionStore:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._cleanup_task: asyncio.Task | None = None

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    def get_or_create(self, chat_id: int) -> Session:
        return run_in_pool_loop(_aget_or_create(chat_id))

    def update(self, session: Session) -> None:
        run_in_pool_loop(_aupdate(session))

    def delete(self, chat_id: int) -> None:
        run_in_pool_loop(_adelete(chat_id))

    async def start(self) -> None:
        # Background cleanup job (REQ-MEMORY-SESSION-002 storage hygiene).
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        interval = float(settings.SESSION_CLEANUP_INTERVAL_S)
        while True:
            try:
                await asyncio.sleep(interval)
                deleted = await asyncio.to_thread(run_in_pool_loop, _acleanup_expired())
                if deleted:
                    logger.info("[MEMORY][cleanup] expired_deleted=%d", deleted)
                # SPEC-IMPLICIT-FB-001 / REQ-FB-CLEANUP-001 — also clean card_impression rows.
                try:
                    from app.channels.implicit_feedback import cleanup_card_impressions

                    await cleanup_card_impressions()
                except Exception:
                    logger.exception("[IMPLICIT_FB][cleanup] error")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[MEMORY][cleanup] error")


# ─── module-level coroutines (run on the pool's loop) ──────────────────────


@observe(name="memory.session.get_or_create", as_type="span")
async def _aget_or_create(chat_id: int) -> Session:
    """Atomic insert-or-reset-on-expiry per REQ-MEMORY-SESSION-002.

    Strategy: single INSERT ... ON CONFLICT ... DO UPDATE SET <defaults>
    WHERE ttl_expires_at < now(). If expired, defaults overwrite. If fresh,
    the WHERE clause causes the UPDATE branch to no-op (row stays).
    """
    ttl_s = int(settings.SESSION_TTL_SECONDS)
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        # Use a SINGLE statement that either inserts (when missing) or resets
        # to defaults when expired (the WHERE in the DO UPDATE branch limits
        # the reset). When the row exists and is fresh, ON CONFLICT picks the
        # UPDATE branch but the WHERE evaluates false → no rows updated, and
        # RETURNING returns nothing for that case. We then SELECT to pull the
        # fresh row. This is two queries but inside one transaction; concurrent
        # expired reads collapse on the UPDATE WHERE clause atomically.
        await cur.execute(
            """
            WITH upsert AS (
                INSERT INTO ai.user_session (chat_id, ttl_expires_at)
                VALUES (%(chat_id)s, now() + (%(ttl)s || ' seconds')::interval)
                ON CONFLICT (chat_id) DO UPDATE SET
                    state                              = 'idle',
                    from_user_id                       = NULL,
                    image_url                          = NULL,
                    detected_items                     = '[]'::jsonb,
                    selected_item_index                = NULL,
                    vision_keywords                    = '[]'::jsonb,
                    vision_item                        = NULL,
                    vision_result                      = NULL,
                    vision_selected_item_index         = NULL,
                    vision_outfit_style_node_primary   = NULL,
                    vision_outfit_style_node_secondary = NULL,
                    vision_outfit_mood_tags            = '[]'::jsonb,
                    vision_outfit_gender               = NULL,
                    user_intent                        = NULL,
                    last_results                       = '[]'::jsonb,
                    shown_product_ids                  = '[]'::jsonb,
                    last_critique_summary              = NULL,
                    boost_keywords                     = '[]'::jsonb,
                    clarify_axis                       = NULL,
                    clarify_value                      = NULL,
                    lang                               = 'en',
                    last_active                        = now(),
                    ttl_expires_at                     = now() + (%(ttl)s || ' seconds')::interval
                WHERE ai.user_session.ttl_expires_at < now()
                RETURNING *
            )
            SELECT * FROM upsert
            UNION ALL
            SELECT * FROM ai.user_session WHERE chat_id = %(chat_id)s
                AND NOT EXISTS (SELECT 1 FROM upsert)
            LIMIT 1
            """,
            {"chat_id": chat_id, "ttl": ttl_s},
        )
        row = await cur.fetchone()
        cols = [d.name for d in cur.description] if cur.description else []
        await conn.commit()
    return _row_to_session(cols, row)


@observe(name="memory.session.update", as_type="span")
async def _aupdate(session: Session) -> None:
    ttl_s = int(settings.SESSION_TTL_SECONDS)
    now_ts = datetime.now(tz=UTC)
    ttl_expires = now_ts + timedelta(seconds=ttl_s)
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.user_session (
                chat_id, state, from_user_id, image_url, detected_items,
                selected_item_index, vision_keywords, vision_item, vision_result,
                vision_selected_item_index, vision_outfit_style_node_primary,
                vision_outfit_style_node_secondary, vision_outfit_mood_tags,
                vision_outfit_gender, user_intent, last_results, shown_product_ids,
                last_critique_summary, boost_keywords, clarify_axis, clarify_value,
                lang, last_active, ttl_expires_at,
                onboarded_at, onboard_stage, onboard_selections,
                onboard_card_message_id, last_pinterest_scrape_url,
                last_pinterest_scrape_at, last_pinterest_pins
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (chat_id) DO UPDATE SET
                state                              = EXCLUDED.state,
                from_user_id                       = EXCLUDED.from_user_id,
                image_url                          = EXCLUDED.image_url,
                detected_items                     = EXCLUDED.detected_items,
                selected_item_index                = EXCLUDED.selected_item_index,
                vision_keywords                    = EXCLUDED.vision_keywords,
                vision_item                        = EXCLUDED.vision_item,
                vision_result                      = EXCLUDED.vision_result,
                vision_selected_item_index         = EXCLUDED.vision_selected_item_index,
                vision_outfit_style_node_primary   = EXCLUDED.vision_outfit_style_node_primary,
                vision_outfit_style_node_secondary = EXCLUDED.vision_outfit_style_node_secondary,
                vision_outfit_mood_tags            = EXCLUDED.vision_outfit_mood_tags,
                vision_outfit_gender               = EXCLUDED.vision_outfit_gender,
                user_intent                        = EXCLUDED.user_intent,
                last_results                       = EXCLUDED.last_results,
                shown_product_ids                  = EXCLUDED.shown_product_ids,
                last_critique_summary              = EXCLUDED.last_critique_summary,
                boost_keywords                     = EXCLUDED.boost_keywords,
                clarify_axis                       = EXCLUDED.clarify_axis,
                clarify_value                      = EXCLUDED.clarify_value,
                lang                               = EXCLUDED.lang,
                last_active                        = EXCLUDED.last_active,
                ttl_expires_at                     = EXCLUDED.ttl_expires_at,
                onboarded_at                       = EXCLUDED.onboarded_at,
                onboard_stage                      = EXCLUDED.onboard_stage,
                onboard_selections                 = EXCLUDED.onboard_selections,
                onboard_card_message_id            = EXCLUDED.onboard_card_message_id,
                last_pinterest_scrape_url          = EXCLUDED.last_pinterest_scrape_url,
                last_pinterest_scrape_at           = EXCLUDED.last_pinterest_scrape_at,
                last_pinterest_pins                = EXCLUDED.last_pinterest_pins
            """,
            (
                session.chat_id,
                str(session.state),
                session.from_user_id,
                session.image_url,
                Jsonb(_to_jsonable(session.detected_items)),
                session.selected_item_index,
                Jsonb(_to_jsonable(session.vision_keywords)),
                session.vision_item,
                Jsonb(_to_jsonable(session.vision_result)) if session.vision_result is not None else None,
                session.vision_selected_item_index,
                session.vision_outfit_style_node_primary,
                session.vision_outfit_style_node_secondary,
                Jsonb(_to_jsonable(session.vision_outfit_mood_tags)),
                session.vision_outfit_gender,
                session.user_intent,
                Jsonb(_to_jsonable(session.last_results)),
                Jsonb(_to_jsonable(session.shown_product_ids)),
                session.last_critique_summary,
                Jsonb(_to_jsonable(session.boost_keywords)),
                session.clarify_axis,
                session.clarify_value,
                session.lang,
                now_ts,
                ttl_expires,
                session.onboarded_at,
                session.onboard_stage,
                Jsonb(_to_jsonable(session.onboard_selections)) if session.onboard_selections else Jsonb({}),
                session.onboard_card_message_id,
                session.last_pinterest_scrape_url,
                session.last_pinterest_scrape_at,
                Jsonb(_to_jsonable(session.last_pinterest_pins)) if session.last_pinterest_pins is not None else None,
            ),
        )
        await conn.commit()
    # Match InMemorySessionStore's behavior of bumping last_active on the dataclass.
    session.last_active = _dt_to_ts(now_ts)


async def _adelete(chat_id: int) -> None:
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM ai.user_session WHERE chat_id = %s", (chat_id,))
        await conn.commit()


async def _acleanup_expired() -> int:
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM ai.user_session WHERE ttl_expires_at < now()")
        deleted = cur.rowcount or 0
        await conn.commit()
    return int(deleted)


def _rehydrate_candidates(raw: Any) -> list[Any]:
    """JSONB round-trip restores dict shape; downstream callers (critique_apply,
    implicit_feedback, search exclude_shown) expect attribute access via
    `getattr(c, 'id')`. Wrap each dict in SimpleNamespace so attribute access
    stays uniform regardless of whether the row came from memory or DB.
    """
    out: list[Any] = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(SimpleNamespace(**item))
        else:
            out.append(item)
    return out


def _row_to_session(cols: list[str], row: Any) -> Session:
    data = dict(zip(cols, row, strict=False))
    last_active = data.get("last_active")
    # @MX:SPEC: SPEC-ONBOARD-CARDS-001 — 7 onboarding columns (migration 0004).
    # `getattr(data, key, None)` style via .get() so pre-0004 schemas (or partial
    # row reads) degrade gracefully to defaults.
    return Session(
        chat_id=data["chat_id"],
        state=SessionState(data.get("state") or "idle"),
        from_user_id=data.get("from_user_id"),
        image_url=data.get("image_url"),
        detected_items=list(data.get("detected_items") or []),
        selected_item_index=data.get("selected_item_index"),
        vision_keywords=list(data.get("vision_keywords") or []),
        vision_item=data.get("vision_item"),
        vision_result=data.get("vision_result"),
        vision_selected_item_index=data.get("vision_selected_item_index"),
        vision_outfit_style_node_primary=data.get("vision_outfit_style_node_primary"),
        vision_outfit_style_node_secondary=data.get("vision_outfit_style_node_secondary"),
        vision_outfit_mood_tags=list(data.get("vision_outfit_mood_tags") or []),
        vision_outfit_gender=data.get("vision_outfit_gender"),
        user_intent=data.get("user_intent"),
        last_results=_rehydrate_candidates(data.get("last_results")),
        shown_product_ids=list(data.get("shown_product_ids") or []),
        last_critique_summary=data.get("last_critique_summary"),
        boost_keywords=list(data.get("boost_keywords") or []),
        clarify_axis=data.get("clarify_axis"),
        clarify_value=data.get("clarify_value"),
        lang=data.get("lang") or "en",
        last_active=_dt_to_ts(last_active) if last_active else 0.0,
        onboarded_at=data.get("onboarded_at"),
        onboard_stage=data.get("onboard_stage"),
        onboard_selections=dict(data.get("onboard_selections") or {}),
        onboard_card_message_id=data.get("onboard_card_message_id"),
        last_pinterest_scrape_url=data.get("last_pinterest_scrape_url"),
        last_pinterest_scrape_at=data.get("last_pinterest_scrape_at"),
        last_pinterest_pins=data.get("last_pinterest_pins"),
    )
