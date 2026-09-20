"""Daily, cursor-stable rankings for edit-shop catalog pages."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

EDIT_SHOP_PLATFORMS = (
    "slowsteadyclub",
    "8division",
    "etcseoul",
    "fr8ight",
    "kith",
)
EDIT_SHOP_GENDERS = ("women", "men")
ALL_CATEGORY = "all"
POPULAR_SIGNAL_THRESHOLD = 500
WHAT100_MAX_AGE = timedelta(hours=48)
_KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class RankingProduct:
    product_id: int
    product_no: int | None
    brand: str


def what100_is_fresh(captured_at: datetime, now: datetime) -> bool:
    return timedelta(0) <= now - captured_at <= WHAT100_MAX_AGE


def daily_shuffle_key(generated_for: date, platform: str, gender: str, category: str, product_id: int) -> str:
    seed = f"{generated_for.isoformat()}:{platform}:{gender}:{category}:{product_id}"
    return hashlib.sha256(seed.encode()).hexdigest()


def enforce_brand_run_limit(products: Sequence[RankingProduct]) -> list[RankingProduct]:
    """Keep source order, moving only a row that would create a third brand in a row."""
    remaining = list(products)
    result: list[RankingProduct] = []
    while remaining:
        pick = 0
        if len(result) >= 2 and result[-1].brand.casefold() == result[-2].brand.casefold():
            repeated = result[-1].brand.casefold()
            alternative = next(
                (index for index, item in enumerate(remaining) if item.brand.casefold() != repeated),
                None,
            )
            if alternative is not None:
                pick = alternative
        result.append(remaining.pop(pick))
    return result


def build_ranking(
    products: Sequence[RankingProduct],
    *,
    generated_for: date,
    platform: str,
    gender: str,
    category: str,
    what100_ranks: dict[int, int] | None = None,
    signal_scores: dict[int, float] | None = None,
    signal_count: int = 0,
) -> tuple[list[RankingProduct], str, dict[int, float]]:
    shuffle_key = lambda item: daily_shuffle_key(  # noqa: E731
        generated_for, platform, gender, category, item.product_id
    )
    scores = signal_scores or {}

    if platform == "slowsteadyclub" and what100_ranks:
        what100_candidates = sorted(
            (item for item in products if item.product_no in what100_ranks),
            key=lambda item: (what100_ranks[item.product_no or -1], shuffle_key(item)),
        )
        ranked = enforce_brand_run_limit(what100_candidates)[:20]
        if ranked:
            ranked_ids = {item.product_id for item in ranked}
            tail = sorted((item for item in products if item.product_id not in ranked_ids), key=shuffle_key)
            ordered = ranked + tail
            source = "what100_plus_shuffle"
        else:
            ordered = sorted(products, key=shuffle_key)
            source = "daily_shuffle"
    elif platform != "slowsteadyclub" and signal_count >= POPULAR_SIGNAL_THRESHOLD:
        ordered = sorted(products, key=lambda item: (-scores.get(item.product_id, 0.0), shuffle_key(item)))
        source = "popular"
    else:
        ordered = sorted(products, key=shuffle_key)
        source = "daily_shuffle"

    return enforce_brand_run_limit(ordered), source, scores


async def _eligible_products(cur, platform: str, gender: str, category: str) -> list[RankingProduct]:
    category_clause = "" if category == ALL_CATEGORY else "AND p.category = %(category)s"
    await cur.execute(
        f"""
        SELECT p.id, p.product_no, p.brand
        FROM public.products p
        WHERE p.platform = %(platform)s
          AND p.in_stock
          AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
          AND p.price >= 5000
          AND p.gender && ARRAY[%(gender)s, 'unisex']::text[]
          {category_clause}
        """,  # noqa: S608 -- the optional clause is module-owned SQL
        {"platform": platform, "gender": gender, "category": category},
    )
    return [
        RankingProduct(int(row[0]), int(row[1]) if row[1] is not None else None, str(row[2]))
        for row in await cur.fetchall()
    ]


async def _categories(cur, platform: str, gender: str) -> list[str]:
    await cur.execute(
        """
        SELECT DISTINCT p.category
        FROM public.products p
        WHERE p.platform = %s
          AND p.in_stock
          AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
          AND p.price >= 5000
          AND p.gender && ARRAY[%s, 'unisex']::text[]
          AND p.category IS NOT NULL AND btrim(p.category) <> ''
        ORDER BY p.category
        """,
        (platform, gender),
    )
    return [ALL_CATEGORY, *(str(row[0]) for row in await cur.fetchall())]


async def _what100_ranks(cur, now: datetime) -> dict[int, int] | None:
    await cur.execute(
        """
        SELECT s.id, s.captured_at
        FROM public.edit_shop_listing_snapshots s
        WHERE s.platform = 'slowsteadyclub'
          AND s.list_type = 'what100'
          AND s.list_key = 'what100'
          AND s.item_count = 100
        ORDER BY s.captured_at DESC
        LIMIT 1
        """
    )
    snapshot = await cur.fetchone()
    if snapshot is None or not what100_is_fresh(snapshot[1], now):
        return None
    await cur.execute(
        """
        SELECT product_no, source_rank
        FROM public.edit_shop_listing_items
        WHERE snapshot_id = %s
        ORDER BY source_rank
        """,
        (snapshot[0],),
    )
    rows = await cur.fetchall()
    if len(rows) != 100 or [int(row[1]) for row in rows] != list(range(1, 101)):
        return None
    return {int(product_no): int(rank) for product_no, rank in rows}


async def _platform_signals(cur, platform: str) -> tuple[dict[int, float], int]:
    await cur.execute(
        """
        WITH eligible AS (
            SELECT id
            FROM public.products
            WHERE platform = %(platform)s
              AND in_stock
              AND image_url IS NOT NULL AND btrim(image_url) <> ''
              AND price >= 5000
        ), signals AS (
            SELECT 'view' AS kind, v.user_id, v.product_id, 1.0 AS weight
            FROM ai.product_views v
            JOIN eligible e ON e.id = v.product_id
            WHERE v.viewed_at >= now() - interval '30 days'
            GROUP BY v.user_id, v.product_id
            UNION ALL
            SELECT 'save', s.user_id, s.product_id, 3.0
            FROM (
                SELECT user_id,
                       CASE WHEN product_id ~ '^[0-9]+$' THEN product_id::bigint END AS product_id
                FROM ai.saves
                WHERE created_at >= now() - interval '30 days'
            ) s
            JOIN eligible e ON e.id = s.product_id
            GROUP BY s.user_id, s.product_id
            UNION ALL
            SELECT 'outbound', t.user_id, t.product_id, 4.0
            FROM (
                SELECT user_id,
                       CASE WHEN metadata->>'product_id' ~ '^[0-9]+$'
                            THEN (metadata->>'product_id')::bigint END AS product_id
                FROM ai.taste_signal_events
                WHERE signal_type = 'outbound'
                  AND occurred_at >= now() - interval '30 days'
            ) t
            JOIN eligible e ON e.id = t.product_id
            GROUP BY t.user_id, t.product_id
        )
        SELECT product_id, sum(weight)::float, sum(count(*)) OVER ()::int
        FROM signals
        GROUP BY product_id
        ORDER BY product_id
        """,
        {"platform": platform},
    )
    rows = await cur.fetchall()
    if not rows:
        return {}, 0
    return {int(row[0]): float(row[1]) for row in rows}, int(rows[0][2])


async def edit_shop_refresh_due(pool: AsyncConnectionPool, generated_for: date | None = None) -> bool:
    target = generated_for or datetime.now(tz=_KST).date()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM ai.edit_shop_rankings WHERE generated_for = %s)",
            (target,),
        )
        row = await cur.fetchone()
    # The refresh is one transaction, so any row proves the whole non-empty
    # catalog set committed; an interrupted refresh leaves no partial day.
    return not row or not bool(row[0])


async def refresh_edit_shop_rankings(
    pool: AsyncConnectionPool,
    *,
    generated_for: date | None = None,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(tz=_KST)
    target = generated_for or now.date()
    written = 0
    async with pool.connection() as conn, conn.cursor() as cur:
        what100 = await _what100_ranks(cur, now)
        for platform in EDIT_SHOP_PLATFORMS:
            scores, signal_count = await _platform_signals(cur, platform)
            for gender in EDIT_SHOP_GENDERS:
                for category in await _categories(cur, platform, gender):
                    products = await _eligible_products(cur, platform, gender, category)
                    ordered, source, base_scores = build_ranking(
                        products,
                        generated_for=target,
                        platform=platform,
                        gender=gender,
                        category=category,
                        what100_ranks=what100,
                        signal_scores=scores,
                        signal_count=signal_count,
                    )
                    await cur.execute(
                        """
                        DELETE FROM ai.edit_shop_rankings
                        WHERE generated_for = %s AND platform = %s AND gender = %s AND category = %s
                        """,
                        (target, platform, gender, category),
                    )
                    rows: Iterable[tuple[object, ...]] = (
                        (
                            platform,
                            gender,
                            category,
                            item.product_id,
                            base_scores.get(item.product_id, 0.0),
                            rank,
                            source,
                            target,
                        )
                        for rank, item in enumerate(ordered, 1)
                    )
                    await cur.executemany(
                        """
                        INSERT INTO ai.edit_shop_rankings
                            (platform, gender, category, product_id, base_score,
                             final_rank, ranking_source, generated_for)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        rows,
                    )
                    written += len(ordered)
        await cur.execute("DELETE FROM ai.edit_shop_rankings WHERE generated_for < %s", (target - timedelta(days=1),))
        await conn.commit()
    logger.info("[edit-shops] rankings refreshed generated_for=%s rows=%d", target, written)
    return written
