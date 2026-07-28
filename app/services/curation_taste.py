"""Style-node preference signals used by personalized curation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

HALF_LIFE_SECONDS = 30 * 24 * 60 * 60

WEIGHTS = {
    "outbound": 4.0,
    "conversation_like": 4.0,
    "conversation_dislike": -4.0,
    "onboarding": 3.0,
    "image": 3.0,
    "save": 2.0,
    "unsave": -2.0,
    "search": 1.5,
    "chip": 1.5,
    "view": 1.0,
    "no_interaction": -1.0,
}


async def record_style_signal(
    pool: AsyncConnectionPool,
    *,
    user_id: UUID,
    style_node_id: int | None,
    signal_type: str,
    dedupe_key: str,
    metadata: dict[str, Any] | None = None,
    weight: float | None = None,
) -> bool:
    """Append one idempotent signal and atomically update the decayed score."""
    if style_node_id is None:
        return False
    signal_weight = WEIGHTS[signal_type] if weight is None else weight
    now = datetime.now(tz=UTC)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.taste_signal_events
                (dedupe_key, user_id, style_node_id, signal_type, weight, metadata, occurred_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING event_id
            """,
            (dedupe_key, user_id, style_node_id, signal_type, signal_weight, Jsonb(metadata or {}), now),
        )
        if await cur.fetchone() is None:
            return False
        await cur.execute(
            """
            INSERT INTO ai.user_style_scores
                (user_id, style_node_id, score, signal_count, last_event_at, updated_at)
            VALUES (%s, %s, %s, 1, %s, %s)
            ON CONFLICT (user_id, style_node_id) DO UPDATE SET
                score = greatest(
                    -20.0,
                    least(
                        20.0,
                        ai.user_style_scores.score
                            * power(
                                0.5,
                                greatest(
                                    0,
                                    extract(epoch FROM (
                                        EXCLUDED.last_event_at - ai.user_style_scores.last_event_at
                                    ))
                                ) / %s
                            )
                            + EXCLUDED.score
                    )
                ),
                signal_count = ai.user_style_scores.signal_count + 1,
                last_event_at = greatest(ai.user_style_scores.last_event_at, EXCLUDED.last_event_at),
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, style_node_id, signal_weight, now, now, HALF_LIFE_SECONDS),
        )
        await conn.commit()
    return True


async def record_product_signal(
    pool: AsyncConnectionPool,
    *,
    user_id: UUID,
    product_id: int,
    signal_type: str,
    dedupe_key: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Resolve the product's primary style node, mark impressions, and score it."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT bn.primary_style_node_id
            FROM public.products p
            LEFT JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
            WHERE p.id = %s
            """,
            (product_id,),
        )
        row = await cur.fetchone()
        await cur.execute(
            """
            UPDATE ai.curation_impressions
            SET interacted_at = COALESCE(interacted_at, now())
            WHERE user_id = %s AND product_id = %s
              AND shown_at > now() - interval '7 days'
            """,
            (user_id, product_id),
        )
        await conn.commit()
    payload = {"product_id": product_id, **(metadata or {})}
    return await record_style_signal(
        pool,
        user_id=user_id,
        style_node_id=int(row[0]) if row and row[0] is not None else None,
        signal_type=signal_type,
        dedupe_key=dedupe_key,
        metadata=payload,
    )


async def record_search_result_signals(
    pool: AsyncConnectionPool,
    *,
    user_id: UUID,
    search_id: UUID | str,
    signal_type: str,
) -> int:
    """Apply one signal to each distinct style represented in a result set."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT DISTINCT bn.primary_style_node_id
            FROM ai.searches s
            CROSS JOIN LATERAL unnest(s.product_ids[1:12]) pid
            JOIN public.products p ON p.id = pid
            JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
            WHERE s.search_id = %s
              AND s.user_id = %s
              AND bn.primary_style_node_id IS NOT NULL
            """,
            (search_id, user_id),
        )
        style_ids = [int(r[0]) for r in await cur.fetchall()]
    written = 0
    for style_id in style_ids:
        if await record_style_signal(
            pool,
            user_id=user_id,
            style_node_id=style_id,
            signal_type=signal_type,
            dedupe_key=f"{signal_type}:{search_id}:{style_id}",
            metadata={"search_id": str(search_id)},
        ):
            written += 1
    return written


async def record_impressions(
    pool: AsyncConnectionPool,
    *,
    user_id: UUID,
    items: list[tuple[str, int, int | None]],
) -> int:
    """Store deduplicated, server-resolved curation impressions."""
    written = 0
    async with pool.connection() as conn, conn.cursor() as cur:
        for section_id, product_id, position in items:
            await cur.execute(
                """
                INSERT INTO ai.curation_impressions
                    (user_id, section_id, product_id, style_node_id, position)
                SELECT %s, %s, p.id, bn.primary_style_node_id, %s
                FROM public.products p
                LEFT JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
                WHERE p.id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM ai.curation_sections s
                      WHERE s.section_id = %s
                        AND s.is_active
                        AND (
                            p.id = ANY(s.product_ids)
                            OR EXISTS (
                                SELECT 1
                                FROM ai.curation_candidates c
                                WHERE c.section_id = s.section_id
                                  AND c.gender = s.gender
                                  AND c.product_id = p.id
                            )
                        )
                  )
                ON CONFLICT (user_id, section_id, product_id, shown_on) DO NOTHING
                RETURNING impression_id
                """,
                (user_id, section_id, position, product_id, section_id),
            )
            if await cur.fetchone() is not None:
                written += 1
        await conn.commit()
    return written


async def attribute_negative_impressions(pool: AsyncConnectionPool) -> int:
    """Apply at most one -1 per user/style/7d after three ignored impressions."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user_id, style_node_id, (array_agg(impression_id ORDER BY shown_at))[1:3]
            FROM ai.curation_impressions i
            WHERE style_node_id IS NOT NULL
              AND interacted_at IS NULL
              AND negative_attributed_at IS NULL
              AND shown_at < now() - interval '24 hours'
              AND shown_at > now() - interval '7 days'
              AND NOT EXISTS (
                  SELECT 1
                  FROM ai.taste_signal_events e
                  WHERE e.user_id = i.user_id
                    AND e.style_node_id = i.style_node_id
                    AND e.signal_type = 'no_interaction'
                    AND e.occurred_at > now() - interval '7 days'
              )
            GROUP BY user_id, style_node_id
            HAVING count(*) >= 3
            """
        )
        groups = await cur.fetchall()
    written = 0
    for user_id, style_node_id, impression_ids in groups:
        recorded = await record_style_signal(
            pool,
            user_id=user_id,
            style_node_id=int(style_node_id),
            signal_type="no_interaction",
            dedupe_key=f"no-interaction:{user_id}:{style_node_id}:{datetime.now(tz=UTC).date()}",
            metadata={"impression_ids": [str(i) for i in impression_ids]},
        )
        if not recorded:
            continue
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai.curation_impressions
                SET negative_attributed_at = now()
                WHERE impression_id = ANY(%s)
                """,
                (impression_ids,),
            )
            await conn.commit()
        written += 1
    return written
