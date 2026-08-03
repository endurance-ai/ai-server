"""SPEC-IMPLICIT-FB-001 — implicit feedback capture.

Four public functions log card impressions, credit clicks, lazily attribute
expired no-click impressions, and detect rapid re-queries. All four are
@observe-decorated and silently no-op when the active memory backend is
in-memory (degraded mode). See spec.md for the full rationale.

Helpers:
- `_keywords_for_product(c)` — single source of truth for keyword extraction.
  REQ-FB-REQUERY-001 requires log_impressions and detect_and_apply_re_query
  to agree on keyword shape.
- `_BACKEND_IS_POSTGRES` module-level flag — set during lifespan startup so
  every call avoids per-call isinstance() against get_store().

# @MX:ANCHOR: [AUTO] Single hot module — every send_results / ingest / click webhook routes here
# @MX:REASON: log_impressions / record_click / attribute_expired_impressions are fan_in >= 3 (3 nodes call them)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from psycopg.types.json import Jsonb

from app.core.config import settings
from app.infrastructure.memory.taste_profile import (
    get_taste_store,
    user_key_for,
)
from app.observability.conversation_log import emit as _conv_emit
from app.observability.langfuse import (
    current_langfuse_trace_id,
    emit_feedback_score,
    observe,
    update_current_span,
)
from app.observability.pii import hash_id

logger = logging.getLogger(__name__)

# Module-level cache — set by `set_backend_is_postgres(True)` during lifespan
# startup when Postgres probe succeeds. Defaults False so unit tests without
# pool init silently skip.
_BACKEND_IS_POSTGRES: bool = False

# WARN-once flood guard — keyed by (chat_id, op) so each session sees one
# warning per operation across its lifetime, not per webhook.
_WARN_ONCE: set[tuple[int, str]] = set()


def set_backend_is_postgres(value: bool) -> None:
    """Set the module-level backend flag. Called from app lifespan after
    Postgres probe succeeds. Tests may also call this directly."""
    global _BACKEND_IS_POSTGRES
    _BACKEND_IS_POSTGRES = bool(value)


def reset_warn_once_for_tests() -> None:
    """Clear the warn-once guard (test hook only)."""
    _WARN_ONCE.clear()


def _warn_once(chat_id: int, op: str, msg: str) -> None:
    key = (int(chat_id), op)
    if key in _WARN_ONCE:
        return
    _WARN_ONCE.add(key)
    logger.warning(msg)


# @MX:ANCHOR: [AUTO] keyword extractor — byte-identical across impression-log
# and re-query call sites (REQ-FB-IMPRESSION-001 + REQ-FB-REQUERY-001)
# @MX:REASON: drift between extractors would split the implicit-signal taste profile
def _keywords_for_product(c: Any) -> list[str]:
    """Extract lowercased keyword list from a Candidate-like object.

    Order of precedence:
      1. `c.keywords` (list[str]) if present
      2. tokens from `c.subcategory + c.name`
    """
    raw_kw = _attr(c, "keywords")
    if isinstance(raw_kw, list) and raw_kw:
        out: list[str] = []
        for k in raw_kw:
            if not k:
                continue
            s = str(k).strip().lower()
            if s:
                out.append(s)
        if out:
            return out
    # fallback: tokenize subcategory + name
    parts: list[str] = []
    sub = (_attr(c, "subcategory", "") or "").strip()
    name = (_attr(c, "name", "") or "").strip()
    for src in (sub, name):
        if not src:
            continue
        for tok in src.replace(",", " ").split():
            s = tok.strip().lower()
            if s and len(s) > 1:
                parts.append(s)
    # de-dupe while preserving order
    seen: set[str] = set()
    return [k for k in parts if not (k in seen or seen.add(k))]


from app.channels._candidate_attr import attr as _attr  # noqa: E402


def _product_id_of(c: Any) -> str | None:
    pid = _attr(c, "id")
    if pid is None:
        return None
    s = str(pid).strip()
    return s or None


def _brand_of(c: Any) -> str:
    return (_attr(c, "brand", "") or "").strip().lower()


# ── Public API ────────────────────────────────────────────────────────────


@observe(name="implicit_feedback.impression_logged", as_type="span")
async def log_impressions(chat_id: int, from_user_id: int | None, products: list[Any]) -> int:
    """Insert one row per product into ai.card_impression. Returns inserted count.

    Single batched INSERT (executemany). Failure caught + logged WARN; never raises.
    """
    t0 = time.perf_counter()
    if not _BACKEND_IS_POSTGRES:
        update_current_span(
            metadata={
                "chat_id_hash": hash_id(chat_id),
                "count": 0,
                "elapsed_ms": 0,
                "backend": "in_memory_skipped",
            }
        )
        return 0

    rows: list[tuple] = []
    window_s = max(0, int(settings.IMPLICIT_FB_ATTRIBUTION_WINDOW_S))
    # @MX:NOTE: P0 user-feedback scores — capture the trace id HERE, in the
    # webhook context that produced the cards. click / no_click arrive on a
    # LATER webhook = a DIFFERENT trace, so the original trace must be bound
    # to the impression row now and read back at attribution time.
    try:
        impression_trace = current_langfuse_trace_id()
    except Exception:  # noqa: BLE001 — trace capture is best-effort
        impression_trace = None
    if impression_trace is None:
        logger.debug("[IMPLICIT_FB][impression] no active langfuse trace; batch unscored")
    for c in products:
        pid = _product_id_of(c)
        if not pid:
            logger.debug("[IMPLICIT_FB][skip] missing product_id")
            continue
        brand = _brand_of(c)
        keywords = _keywords_for_product(c)
        rows.append((chat_id, from_user_id, pid, brand, Jsonb(keywords), window_s, impression_trace))

    if not rows:
        update_current_span(
            metadata={
                "chat_id_hash": hash_id(chat_id),
                "count": 0,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "backend": "postgres",
            }
        )
        return 0

    try:
        from app.providers.db_pool import get_pool, run_in_pool_loop

        async def _insert() -> None:
            async with get_pool().connection() as conn, conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO ai.card_impression
                        (chat_id, from_user_id, product_id, brand, keywords,
                         attribution_window_s, langfuse_trace)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
                await conn.commit()

        run_in_pool_loop(_insert())
    except Exception as exc:  # noqa: BLE001
        _warn_once(chat_id, "impression_logged", f"[IMPLICIT_FB][impression] insert failed: {exc!r}")
        update_current_span(
            metadata={
                "chat_id_hash": hash_id(chat_id),
                "count": 0,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "backend": "postgres_error",
            }
        )
        return 0

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    update_current_span(
        metadata={
            "chat_id_hash": hash_id(chat_id),
            "count": len(rows),
            "elapsed_ms": elapsed_ms,
            "backend": "postgres",
        }
    )
    return len(rows)


@observe(name="implicit_feedback.click", as_type="span")
async def record_click(
    chat_id: int,
    from_user_id: int | None,
    product_id: str,
    brand: str,
    keywords: list[str],
    *,
    stale: bool = False,
) -> int:
    """Mark impression as clicked and reinforce liked_brand/liked_keywords.

    Returns rows_affected on the UPDATE (0 if already attributed / clicked).
    Reinforcement runs regardless of UPDATE result (real UI tap = real signal).
    """
    t0 = time.perf_counter()
    weight = float(settings.IMPLICIT_FB_CLICK_WEIGHT)

    if stale or not product_id:
        update_current_span(
            metadata={
                "chat_id_hash": hash_id(chat_id),
                "product_id": product_id or "",
                "brand": brand,
                "weight": weight,
                "stale": True,
                "db_rows_affected": 0,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            }
        )
        return 0

    rows_affected = 0
    original_trace: str | None = None
    if _BACKEND_IS_POSTGRES:
        try:
            from app.providers.db_pool import get_pool, run_in_pool_loop

            # @MX:NOTE: P0 user-feedback scores — RETURNING langfuse_trace
            # recovers the ORIGINAL recommendation trace bound at impression
            # time (this click webhook is a different trace). rows_affected
            # uses len(fetched): cur.rowcount is unreliable after fetchall()
            # on psycopg3 (can be -1/0).
            async def _update() -> tuple[int, str | None]:
                async with get_pool().connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE ai.card_impression
                           SET click_status = 'clicked', click_at = now()
                         WHERE chat_id = %s AND product_id = %s AND click_status IS NULL
                        RETURNING langfuse_trace
                        """,
                        (chat_id, product_id),
                    )
                    fetched = await cur.fetchall()
                    await conn.commit()
                trace = fetched[0][0] if fetched else None
                return len(fetched), trace

            rows_affected, original_trace = run_in_pool_loop(_update())
        except Exception as exc:  # noqa: BLE001
            _warn_once(chat_id, "click", f"[IMPLICIT_FB][click] update failed: {type(exc).__name__}")
            rows_affected = 0
            original_trace = None

    # Score the ORIGINAL trace OUTSIDE the DB try/except — a scoring failure
    # must never set rows_affected=0 or skip taste reinforcement.
    if original_trace:
        emit_feedback_score(
            original_trace,
            signal="click",
            product_id=hash_id(product_id),
            brand=brand,
        )

    # Reinforce taste profile regardless of DB state
    if settings.TASTE_PROFILE_ENABLED:
        try:
            taste_store = get_taste_store()
            profile = taste_store.get_or_create(user_key_for(from_user_id, chat_id))
            if brand:
                profile.reinforce_liked_brand(brand, weight=weight)
            if keywords:
                profile.reinforce_liked_keywords(keywords, weight=weight)
            taste_store.update(profile)
        except Exception:  # noqa: BLE001
            logger.exception("[IMPLICIT_FB][click] taste reinforcement failed")

    update_current_span(
        metadata={
            "chat_id_hash": hash_id(chat_id),
            "product_id": product_id,
            "brand": brand,
            "weight": weight,
            "stale": False,
            "db_rows_affected": rows_affected,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "backend": "postgres" if _BACKEND_IS_POSTGRES else "in_memory_skipped",
        }
    )
    return rows_affected


@observe(name="implicit_feedback.no_click", as_type="span")
async def attribute_expired_impressions(chat_id: int, from_user_id: int | None) -> int:
    """Single-CTE round-trip — select+update expired pending rows, reinforce
    disliked_* with NOCLICK_WEIGHT. Returns count attributed."""
    t0 = time.perf_counter()
    weight = float(settings.IMPLICIT_FB_NOCLICK_WEIGHT)

    if not _BACKEND_IS_POSTGRES:
        update_current_span(
            metadata={
                "chat_id_hash": hash_id(chat_id),
                "attributed_count": 0,
                "weight": weight,
                "elapsed_ms": 0,
                "backend": "in_memory_skipped",
            }
        )
        return 0

    try:
        from app.providers.db_pool import get_pool, run_in_pool_loop

        # @MX:NOTE: P0 user-feedback scores — RETURNING langfuse_trace gives
        # the ORIGINAL recommendation trace per expired (no-click) impression.
        async def _attribute() -> list[tuple[str, list[str], str | None]]:
            async with get_pool().connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    """
                    WITH expired AS (
                        SELECT id, brand, keywords FROM ai.card_impression
                         WHERE chat_id = %s
                           AND click_status IS NULL
                           AND shown_at + (attribution_window_s * interval '1 second') < now()
                    )
                    UPDATE ai.card_impression
                       SET click_status = 'attributed_no_click'
                     WHERE id IN (SELECT id FROM expired)
                    RETURNING brand, keywords, langfuse_trace
                    """,
                    (chat_id,),
                )
                rows = await cur.fetchall()
                await conn.commit()
            return [(r[0], list(r[1] or []), r[2]) for r in rows]

        attributed = run_in_pool_loop(_attribute())
    except Exception as exc:  # noqa: BLE001
        _warn_once(chat_id, "no_click", f"[IMPLICIT_FB][no_click] attribute failed: {type(exc).__name__}")
        update_current_span(
            metadata={
                "chat_id_hash": hash_id(chat_id),
                "attributed_count": 0,
                "weight": weight,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "backend": "postgres_error",
            }
        )
        return 0

    # Score each expired (no-click) impression's ORIGINAL trace OUTSIDE the
    # DB try/except — a scoring failure must never skip taste reinforcement.
    for _brand, _kw, _trace in attributed:
        if _trace:
            emit_feedback_score(_trace, signal="no_click", brand=_brand or None)

    if attributed and settings.TASTE_PROFILE_ENABLED:
        try:
            taste_store = get_taste_store()
            profile = taste_store.get_or_create(user_key_for(from_user_id, chat_id))
            for brand, keywords, _ in attributed:
                if brand:
                    profile.reinforce_disliked_brand(brand, weight=weight)
                if keywords:
                    profile.reinforce_disliked_keywords(keywords, weight=weight)
            taste_store.update(profile)
        except Exception:  # noqa: BLE001
            logger.exception("[IMPLICIT_FB][no_click] taste reinforcement failed")

    # LOG-T22 — emit `taste_update(source="no_click")` per attribution batch.
    # thread_id is unknown at this caller; we pass a fresh uuid4 (no callback
    # correlation needed for background attribution).
    # @MX:SPEC: SPEC-CONVERSATION-LOG-001
    if attributed:
        try:
            from uuid import uuid4

            agg_brands: list[str] = []
            agg_keywords: list[str] = []
            for brand, keywords, _ in attributed:
                if brand:
                    agg_brands.append(brand)
                if keywords:
                    agg_keywords.extend(keywords)
            _conv_emit(
                event_type="taste_update",
                user_key=user_key_for(from_user_id, chat_id),
                chat_id=chat_id,
                thread_id=uuid4(),
                turn_no=0,
                payload={
                    "source": "no_click",
                    "keywords_delta": {"disliked_added": agg_keywords},
                    "brands_delta": {"disliked_added": agg_brands},
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("[IMPLICIT_FB][no_click] conversation_log emit best-effort")

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    update_current_span(
        metadata={
            "chat_id_hash": hash_id(chat_id),
            "attributed_count": len(attributed),
            "weight": weight,
            "elapsed_ms": elapsed_ms,
            "backend": "postgres",
        }
    )
    return len(attributed)


async def _fetch_clicked_product_ids(chat_id: int) -> set[str]:
    """Return product_ids that were already clicked in this chat.

    Used by re-query path to exclude explicitly liked products from the
    soft-negative reinforcement (preserves REQ-FB-CLICK-001 signal).
    Best-effort: returns empty set on any failure or in_memory backend.
    """
    if not _BACKEND_IS_POSTGRES:
        return set()
    try:
        from app.providers.db_pool import get_pool

        async with get_pool().connection() as conn, conn.cursor() as cur:
            # LIMIT to avoid unbounded memory growth on long-lived chats.
            await cur.execute(
                "SELECT DISTINCT product_id FROM ai.card_impression "
                "WHERE chat_id = %s AND click_status = 'clicked' LIMIT 1000",
                (chat_id,),
            )
            rows = await cur.fetchall()
        return {row[0] for row in rows if row and row[0]}
    except Exception:
        logger.debug("[IMPLICIT_FB][re-query] clicked_pids fetch failed (best-effort)", exc_info=True)
        return set()


async def _fetch_original_traces(chat_id: int, product_ids: list[str]) -> set[str]:
    """Return the distinct ORIGINAL recommendation trace ids bound at
    impression time for the given products in this chat.

    Used by the re-query path (a session-level signal with no impression-row
    link) to recover the trace(s) that produced the re-queried cards.
    Best-effort: returns empty set on any failure / in_memory backend.
    """
    if not _BACKEND_IS_POSTGRES or not product_ids:
        return set()
    try:
        from app.providers.db_pool import get_pool

        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT langfuse_trace FROM ai.card_impression "
                "WHERE chat_id = %s AND product_id = ANY(%s) "
                "AND langfuse_trace IS NOT NULL LIMIT 1000",
                (chat_id, list(product_ids)),
            )
            rows = await cur.fetchall()
        return {row[0] for row in rows if row and row[0]}
    except Exception as exc:  # noqa: BLE001
        # warn-once so a missing langfuse_trace column (pre-migration deploy)
        # is visible instead of silently swallowed. Still fail-open.
        _warn_once(
            chat_id,
            "rq_trace_fetch",
            f"[IMPLICIT_FB][re-query] original trace fetch failed ({type(exc).__name__}) — "
            f"check migration 0003 applied (best-effort, returning empty)",
        )
        return set()


@observe(name="implicit_feedback.re_query", as_type="span")
async def detect_and_apply_re_query(session: Any, inbound_is_fresh_query: bool) -> bool:
    """If session matches re-query predicate, soft-negatively reinforce
    last_results' brands+keywords. Returns whether the re-query triggered."""
    t0 = time.perf_counter()
    weight = float(settings.IMPLICIT_FB_REQUERY_WEIGHT)
    window_s = max(0, int(settings.IMPLICIT_FB_REQUERY_WINDOW_S))

    try:
        from app.infrastructure.memory.session import SessionState

        triggered = (
            inbound_is_fresh_query
            and getattr(session, "state", None) == SessionState.RESULTS_SENT
            and bool(getattr(session, "last_results", None))
            and window_s > 0
            and (time.time() - float(getattr(session, "last_active", 0.0))) < window_s
        )
    except Exception:  # noqa: BLE001
        triggered = False

    if not triggered:
        update_current_span(
            metadata={
                "chat_id_hash": hash_id(getattr(session, "chat_id", None)),
                "triggered": False,
                "products_count": 0,
                "weight": weight,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            }
        )
        return False

    products = list(getattr(session, "last_results", []) or [])
    elapsed_since = time.time() - float(getattr(session, "last_active", 0.0))
    # Guard: chat_id may be missing on degenerate sessions. Skip safely.
    _raw_chat_id = getattr(session, "chat_id", None)
    if _raw_chat_id is None:
        return False
    try:
        chat_id_int = int(_raw_chat_id)
    except (TypeError, ValueError):
        return False
    clicked_pids = await _fetch_clicked_product_ids(chat_id_int)
    # Exclude already-clicked products from re-query soft-negative to preserve
    # explicit positive signals (REQ-FB-CLICK-001 must not be cancelled by a
    # subsequent re-query in the same session).
    filtered_products = [c for c in products if (_product_id_of(c) or "") not in clicked_pids]
    excluded_n = len(products) - len(filtered_products)

    # @MX:NOTE: P0 user-feedback scores — re_query is a SESSION-level signal
    # with no impression-row link, so the ORIGINAL recommendation trace is
    # recovered by looking up the impression rows for the re-queried products.
    # One distinct trace → one (user_feedback=0.0 + re_query=1.0) pair.
    _rq_pids = [pid for c in filtered_products if (pid := _product_id_of(c))]
    for _rq_trace in await _fetch_original_traces(chat_id_int, _rq_pids):
        emit_feedback_score(
            _rq_trace,
            signal="re_query",
            attribution_window_s=window_s,
        )

    re_query_brands: list[str] = []
    re_query_keywords: list[str] = []
    if settings.TASTE_PROFILE_ENABLED:
        try:
            taste_store = get_taste_store()
            from_user_id = getattr(session, "from_user_id", None)
            profile = taste_store.get_or_create(user_key_for(from_user_id, chat_id_int))
            for c in filtered_products:
                brand = _brand_of(c)
                keywords = _keywords_for_product(c)
                if brand:
                    profile.reinforce_disliked_brand(brand, weight=weight)
                    re_query_brands.append(brand)
                if keywords:
                    profile.reinforce_disliked_keywords(keywords, weight=weight)
                    re_query_keywords.extend(keywords)
            taste_store.update(profile)
            logger.info(
                "[IMPLICIT_FB][re-query] chat_id_hash=%s products=%d excluded_clicked=%d elapsed_since_results_s=%.1f",
                hash_id(chat_id_int),
                len(filtered_products),
                excluded_n,
                elapsed_since,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[IMPLICIT_FB][re-query] reinforcement failed")

    # LOG-T22 — emit `taste_update(source="re_query")` once per detected
    # re-query, capturing the aggregated soft-negative signal.
    # @MX:SPEC: SPEC-CONVERSATION-LOG-001
    try:
        from uuid import uuid4

        _conv_emit(
            event_type="taste_update",
            user_key=user_key_for(getattr(session, "from_user_id", None), chat_id_int),
            chat_id=chat_id_int,
            thread_id=uuid4(),
            turn_no=0,
            payload={
                "source": "re_query",
                "keywords_delta": {"disliked_added": re_query_keywords},
                "brands_delta": {"disliked_added": re_query_brands},
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[IMPLICIT_FB][re-query] conversation_log emit best-effort")

    update_current_span(
        metadata={
            "chat_id_hash": hash_id(getattr(session, "chat_id", None)),
            "triggered": True,
            "products_count": len(products),
            "elapsed_since_results_s": round(elapsed_since, 2),
            "weight": weight,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }
    )
    return True


async def cleanup_card_impressions() -> tuple[int, int]:
    """Hard-delete attributed/clicked rows > 7d AND pending rows > 60d.

    Returns (attributed_deleted, pending_deleted). REQ-FB-CLEANUP-001.
    Called by PostgresSessionStore's cleanup loop. Safe to call in non-postgres
    mode (returns (0, 0)).
    """
    if not _BACKEND_IS_POSTGRES:
        return (0, 0)
    try:
        from app.providers.db_pool import get_pool

        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM ai.card_impression
                 WHERE click_status IS NOT NULL
                   AND COALESCE(click_at, shown_at + (attribution_window_s * interval '1 second'))
                       < now() - interval '7 days'
                """
            )
            n_attr = cur.rowcount or 0
            await cur.execute(
                """
                DELETE FROM ai.card_impression
                 WHERE click_status IS NULL
                   AND shown_at < now() - interval '60 days'
                """
            )
            n_pending = cur.rowcount or 0
            await conn.commit()
        if n_attr or n_pending:
            logger.info("[IMPLICIT_FB][cleanup] deleted_attr=%d deleted_pending=%d", n_attr, n_pending)
        return (int(n_attr), int(n_pending))
    except Exception:  # noqa: BLE001
        logger.exception("[IMPLICIT_FB][cleanup] error")
        return (0, 0)


# Length budget for crit:click:{suffix} — Telegram 64B limit minus "crit:click:" prefix (11)
_CLICK_SUFFIX_BUDGET = 53


def click_callback_for(product_id: str) -> str:
    """Build callback_data 'crit:click:{suffix}' truncating long product_ids
    to the last _CLICK_SUFFIX_BUDGET chars. REQ-FB-UX-001."""
    pid = str(product_id or "")
    if len(pid) > _CLICK_SUFFIX_BUDGET:
        pid = pid[-_CLICK_SUFFIX_BUDGET:]
    return f"crit:click:{pid}"


# @MX:ANCHOR: [AUTO] suffix-match resolver — fan_in from critique_apply on every click webhook
# @MX:REASON: matches truncated callback_data back to a Candidate in Session.last_results
def resolve_click_target(suffix: str, last_results: list[Any]) -> Any | None:
    """Resolve a (possibly truncated) product_id suffix to a Candidate in
    last_results. Prefers exact match; falls back to endswith match. Returns
    None if no match found."""
    if not suffix:
        return None
    # exact match first
    for c in last_results:
        pid = _product_id_of(c)
        if pid is not None and pid == suffix:
            return c
    # suffix match (handles truncation)
    for c in last_results:
        pid = _product_id_of(c)
        if pid is not None and pid.endswith(suffix):
            return c
    return None
