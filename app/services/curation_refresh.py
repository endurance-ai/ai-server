"""auto 구좌 일일 후보 계산.

구좌 메타데이터(존재·제목·순서·활성·display_type과 editorial 상품 목록)는
어드민 페이지가 소유한다. 이 모듈은 auto 구좌(trending-search /
under-100)의 product_ids 를 KST 하루 한 번 다시 계산해 UPDATE 할 뿐,
ai.curation_sections 에 행을 만들거나 지우지 않는다.

이력: 2026-08 이전에는 노션 "큐레이션 구좌 (어드민)" DB 를 폴링해 구좌
메타데이터를 동기화했다. 어드민 페이지로 이관하면서 그 경로는 제거했다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from app.core.config import settings
from app.services.curation_sections import CURATION_SECTION_PRODUCT_LIMIT, DAILY_AUTO_SECTION_IDS
from app.services.curation_taste import feature_pairs

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_GENDERS = ("women", "men")
_AUTO_IDS = DAILY_AUTO_SECTION_IDS
_SECTION_SIZE = CURATION_SECTION_PRODUCT_LIMIT
# The second auto slot must still have 100 candidates after the first slot's
# products are excluded. Keep extra headroom for differing quality filters.
_CANDIDATES_PER_SECTION = CURATION_SECTION_PRODUCT_LIMIT * 3

_WINTER_NAME_RE = "패딩|기모|플리스|무스탕|puffer|fleece"
_BASE_EXCLUDED_CATEGORIES = ("other",)
_SUMMER_EXCLUDED_CATEGORIES = ("outerwear", "knitwear")
_SUMMER_EXCLUDED_SUBCATEGORIES = ("hoodie", "sweater", "cardigan")
_MEN_ONLY_EXCLUDED_SUBCATEGORIES = (
    "skirt",
    "heels",
    "bikini",
    "bra",
    "blouse",
    "crop-top",
    "hairbands",
)


# 성별 매칭 — products.gender 단일 출처 (search_products_v6.sql 과 동일).
#
# 이력: VLM(product_features) 우선 3단 다리 → 크롤러 정본 우선 → 단일 출처.
# gender 소유권이 크롤러로 돌아왔고 migration 104 가
# `chk_products_gender_required` 를 VALIDATE 했으므로 p.gender 는
# non-NULL · non-empty · canonical 이 보장된다 — 폴백이 방어할 대상이 없다.
#
# unisex 는 포함한다 — 검색(search_products_v6.sql:138 `p.gender &&
# ARRAY[p_gender, 'unisex']`)과 같은 술어다. 이전에는 큐레이션만 unisex 를
# 잘라내는 비대칭이었는데, 카탈로그 상당수가 unisex 라벨이라 브랜드 구좌가
# 통째로 비었다(예: 999HUMANITY 727개 전량, NWT 48개 전량, Scuffers 1221개 중 974개).
# 이제 성별 정의는 검색·큐레이션 한 곳뿐이다.
#
# 빈 배열과 NULL 은 `&&` 가 각각 false / NULL 이라 그대로 걸러진다.
# PRODUCT_FEATURES_JOIN 은 유지한다: primary_color 와 품질 게이트가 쓴다.
PRODUCT_FEATURES_JOIN = "LEFT JOIN public.product_features pf ON pf.product_id = p.id"

GENDER_MATCH_SQL = """
        p.gender && ARRAY[%(gender)s, 'unisex']::text[]
"""


def _quality_sql() -> str:
    winter_name_gate = f"AND p.name !~* '{_WINTER_NAME_RE}'" if settings.CURATION_SEASON.lower() == "summer" else ""
    return f"""
        p.in_stock
        AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
        AND p.price >= 5000
        AND {GENDER_MATCH_SQL}
        AND length(btrim(p.brand)) BETWEEN 1 AND 40
        AND p.brand NOT ILIKE '%%상품명%%'
        AND p.brand <> p.name
        AND NOT (COALESCE(p.category, '') = ANY(%(excluded_categories)s))
        AND NOT (COALESCE(p.subcategory, '') = ANY(%(excluded_subcategories)s))
        {winter_name_gate}
    """


def _candidate_sql(section_id: str) -> str:
    if section_id == "trending-search":
        signal_ctes = """
            recent_search AS (
                SELECT x.pid AS product_id, count(*)::float / 2 AS n
                FROM ai.searches s
                CROSS JOIN LATERAL unnest(s.product_ids[1:20]) x(pid)
                WHERE s.created_at > now() - interval '2 days'
                GROUP BY x.pid
            ),
            prior_search AS (
                SELECT x.pid AS product_id, count(*)::float / 7 AS n
                FROM ai.searches s
                CROSS JOIN LATERAL unnest(s.product_ids[1:20]) x(pid)
                WHERE s.created_at <= now() - interval '2 days'
                  AND s.created_at > now() - interval '9 days'
                GROUP BY x.pid
            ),
            views AS (
                SELECT product_id, count(DISTINCT user_id)::float AS n
                FROM ai.product_views
                WHERE viewed_at > now() - interval '7 days'
                GROUP BY product_id
            ),
            saves AS (
                SELECT product_id::bigint AS product_id, count(DISTINCT user_id)::float AS n
                FROM ai.saves
                WHERE product_id ~ '^[0-9]+$'
                  AND created_at > now() - interval '7 days'
                GROUP BY product_id::bigint
            ),
            outbound AS (
                SELECT (metadata->>'product_id')::bigint AS product_id,
                       count(DISTINCT user_id)::float AS n
                FROM ai.taste_signal_events
                WHERE signal_type = 'outbound'
                  AND occurred_at > now() - interval '7 days'
                  AND metadata->>'product_id' ~ '^[0-9]+$'
                GROUP BY (metadata->>'product_id')::bigint
            )
        """
        product_score = """
            greatest(0, COALESCE(rs.n, 0) - COALESCE(ps.n, 0))
                + COALESCE(v.n, 0) + 3 * COALESCE(sv.n, 0) + 4 * COALESCE(o.n, 0)
                + ln(1 + COALESCE(p.review_count, 0))
        """
        joins = """
            LEFT JOIN recent_search rs ON rs.product_id = p.id
            LEFT JOIN prior_search ps ON ps.product_id = p.id
            LEFT JOIN views v ON v.product_id = p.id
            LEFT JOIN saves sv ON sv.product_id = p.id
            LEFT JOIN outbound o ON o.product_id = p.id
        """
        signal_prefix = f"{signal_ctes},"
        return f"""
            WITH
            {signal_prefix}
            eligible AS (
                SELECT
                    p.id AS product_id,
                    COALESCE(NULLIF(lower(btrim(p.brand)), ''), p.id::text) AS brand_key,
                    p.brand_node_id,
                    bn.primary_style_node_id AS style_node_id,
                    {product_score} AS product_score
                FROM public.products p
                LEFT JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
                {PRODUCT_FEATURES_JOIN}
                {joins}
                WHERE {_quality_sql()}
            ),
            brand_products AS (
                SELECT *,
                    row_number() OVER (
                        PARTITION BY brand_key
                        ORDER BY product_score DESC,
                                 md5(product_id::text || current_date::text)
                    ) AS product_rank
                FROM eligible
            ),
            brand_scores AS (
                SELECT brand_key, sum(product_score) AS brand_score
                FROM brand_products
                WHERE product_rank <= 3
                GROUP BY brand_key
            ),
            ranked AS (
                SELECT bp.*, bs.brand_score,
                    row_number() OVER (
                        ORDER BY bs.brand_score DESC, bp.product_score DESC,
                                 md5(bp.product_id::text || current_date::text)
                    ) AS base_rank
                FROM brand_products bp
                JOIN brand_scores bs USING (brand_key)
            )
            SELECT product_id, false AS is_hot, brand_score AS base_score, base_rank,
                   brand_key, brand_node_id, style_node_id
            FROM ranked
            WHERE base_rank <= {_CANDIDATES_PER_SECTION}
            ORDER BY base_rank
        """  # noqa: S608 -- all interpolation is module-owned SQL
    score = "0::float"
    return f"""
        WITH
        eligible AS (
            SELECT
                p.id AS product_id,
                COALESCE(NULLIF(lower(btrim(p.brand)), ''), p.id::text) AS brand_key,
                p.brand_node_id,
                bn.primary_style_node_id AS style_node_id,
                {score} AS base_score
            FROM public.products p
            LEFT JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
            {PRODUCT_FEATURES_JOIN}
            WHERE {_quality_sql()}
              AND p.price <= 150000
        ),
        ranked AS (
            SELECT eligible.*,
                row_number() OVER (
                    ORDER BY base_score DESC,
                             md5(product_id::text || current_date::text)
                ) AS base_rank
            FROM eligible
        )
        SELECT product_id, false AS is_hot, base_score, base_rank, brand_key,
               brand_node_id, style_node_id
        FROM ranked
        WHERE base_rank <= {_CANDIDATES_PER_SECTION}
        ORDER BY base_rank
    """  # noqa: S608 -- all interpolation is module-owned SQL


def _query_params(gender: str) -> dict[str, Any]:
    categories = list(_BASE_EXCLUDED_CATEGORIES)
    subcategories: list[str] = []
    if settings.CURATION_SEASON.lower() == "summer":
        categories.extend(_SUMMER_EXCLUDED_CATEGORIES)
        subcategories.extend(_SUMMER_EXCLUDED_SUBCATEGORIES)
    if gender == "men":
        categories.append("dresses")
        subcategories.extend(_MEN_ONLY_EXCLUDED_SUBCATEGORIES)
    return {
        "gender": gender,
        "excluded_categories": categories,
        "excluded_subcategories": subcategories,
    }


def _stable_tie(seed: str, product_id: int) -> int:
    return int(hashlib.sha256(f"{seed}:{product_id}".encode()).hexdigest()[:16], 16)


def _norm_taste(score: float) -> float:
    """Map a decayed taste score in [-20, 20] onto [0, 1]. 0 → 0.5 (neutral)."""
    return (max(-20.0, min(20.0, score)) + 20.0) / 40.0


def _feature_taste(metadata: Any, feature_scores: dict[tuple[str, str], float]) -> float:
    """Mean normalized preference over a product's (axis, value) feature pairs.

    Reuses `feature_pairs` (the same extractor that recorded the signals) so the
    scoring axis/value keys match the stored scores exactly. A product with no
    enriched features — or a cold user with no matching signal — contributes the
    neutral 0.5, so it neither helps nor hurts the ranking.
    """
    pairs = feature_pairs(metadata if isinstance(metadata, dict) else None)
    if not pairs:
        return 0.5
    return sum(_norm_taste(feature_scores.get(p, 0.0)) for p in pairs) / len(pairs)


def select_candidate_ids(
    rows: list[dict[str, Any]],
    *,
    section_id: str,
    excluded_ids: set[int],
    taste_scores: dict[int, float] | None = None,
    feature_scores: dict[tuple[str, str], float] | None = None,
    seed: str,
    require_full: bool = True,
) -> list[int]:
    """Select up to 100 products, distributed round-robin across brands.

    Personalization blends taste onto the base rank. The style-node axis
    (`taste_scores`) is joined by the visual-feature axes (`feature_scores`,
    matched against each row's `feature_metadata`). When the user has feature
    signals the taste budget is split style/feature (0.15/0.15, or 0.35/0.35 for
    under-100); style-only users keep the original style weight (0.3 / 0.7) so
    the shipped behavior does not regress. With neither axis, ordering falls back
    to base rank.
    """
    usable = [r for r in rows if int(r["product_id"]) not in excluded_ids]
    if not usable:
        return []
    count = len(usable)
    taste_scores = taste_scores or {}
    feature_scores = feature_scores or {}
    personalized = bool(taste_scores or feature_scores)
    under_100 = section_id == "under-100"

    def rank_value(row: dict[str, Any]) -> tuple[float, int]:
        base = 1.0 - (max(1, int(row["base_rank"])) - 1) / max(1, count - 1)
        if not personalized:
            combined = base
        else:
            style = _norm_taste(taste_scores.get(row.get("style_node_id"), 0.0))
            if feature_scores:
                feature = _feature_taste(row.get("feature_metadata"), feature_scores)
                combined = (
                    0.3 * base + 0.35 * style + 0.35 * feature
                    if under_100
                    else 0.7 * base + 0.15 * style + 0.15 * feature
                )
            else:
                combined = 0.3 * base + 0.7 * style if under_100 else 0.7 * base + 0.3 * style
        return combined, -_stable_tie(seed, int(row["product_id"]))

    ranked = sorted(usable, key=rank_value, reverse=True)
    brand_order: list[str] = []
    brand_rows: dict[str, list[int]] = defaultdict(list)
    seen_ids: set[int] = set()
    for row in ranked:
        pid = int(row["product_id"])
        brand = str(row["brand_key"])
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        if brand not in brand_rows:
            brand_order.append(brand)
        brand_rows[brand].append(pid)

    # Rank decides the order within each brand and the order in which brands
    # enter the rotation. Repeated rounds keep the mix even, while allowing a
    # single/few brands to fill the full section instead of stopping at a cap.
    selected: list[int] = []
    depth = 0
    while len(selected) < _SECTION_SIZE:
        added = False
        for brand in brand_order:
            products = brand_rows[brand]
            if depth >= len(products):
                continue
            selected.append(products[depth])
            added = True
            if len(selected) >= _SECTION_SIZE:
                break
        if not added:
            break
        depth += 1

    if require_full and len(selected) != _SECTION_SIZE:
        return []
    return selected


# ── Daily auto candidates ────────────────────────────────────────────────────


async def refresh_auto_sections(
    pool: AsyncConnectionPool,
    section_ids: tuple[str, ...] = _AUTO_IDS,
    *,
    strict_quota: bool = True,
) -> int:
    generated_for = datetime.now(tz=_KST).date()
    written = 0
    requested = set(section_ids)
    ordered_section_ids = tuple(section_id for section_id in _AUTO_IDS if section_id in requested)
    for gender in _GENDERS:
        excluded_ids: set[int] = set()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT section_id, product_ids
                FROM ai.curation_sections
                WHERE gender = %s AND slot_type = 'auto' AND is_active
                  AND section_id = ANY(%s)
                """,
                (gender, list(_AUTO_IDS)),
            )
            cached_ids = {str(row[0]): {int(pid) for pid in (row[1] or [])} for row in await cur.fetchall()}

        processed: set[str] = set()
        for section_id in ordered_section_ids:
            section_index = _AUTO_IDS.index(section_id)
            for prior_section_id in _AUTO_IDS[:section_index]:
                if prior_section_id not in processed:
                    excluded_ids.update(cached_ids.get(prior_section_id, set()))
            try:
                async with pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(_candidate_sql(section_id), _query_params(gender))
                    raw = await cur.fetchall()
                    rows = [
                        {
                            "product_id": int(r[0]),
                            "is_hot": bool(r[1]),
                            "base_score": float(r[2]),
                            "base_rank": int(r[3]),
                            "brand_key": r[4],
                            "brand_node_id": r[5],
                            "style_node_id": r[6],
                        }
                        for r in raw
                    ]
                    selected = select_candidate_ids(
                        rows,
                        section_id=section_id,
                        excluded_ids=excluded_ids,
                        seed=f"guest:{gender}:{section_id}:{generated_for}",
                        require_full=strict_quota,
                    )
                    if not selected:
                        logger.warning("🗂 [curation] no valid candidates %s/%s", section_id, gender)
                        excluded_ids.update(cached_ids.get(section_id, set()))
                        processed.add(section_id)
                        continue
                    await cur.execute(
                        "DELETE FROM ai.curation_candidates WHERE section_id = %s AND gender = %s",
                        (section_id, gender),
                    )
                    for row in rows:
                        await cur.execute(
                            """
                            INSERT INTO ai.curation_candidates
                                (section_id, gender, product_id, is_hot, base_score, base_rank,
                                 brand_key, brand_node_id, style_node_id, generated_for)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                section_id,
                                gender,
                                row["product_id"],
                                row["is_hot"],
                                row["base_score"],
                                row["base_rank"],
                                row["brand_key"],
                                row["brand_node_id"],
                                row["style_node_id"],
                                generated_for,
                            ),
                        )
                    await cur.execute(
                        """
                        UPDATE ai.curation_sections
                        SET product_ids = %s, products_updated_at = now()
                        WHERE section_id = %s AND gender = %s AND slot_type = 'auto'
                        """,
                        (selected, section_id, gender),
                    )
                    await conn.commit()
                excluded_ids.update(selected)
                cached_ids[section_id] = set(selected)
                processed.add(section_id)
                written += 1
            except Exception as exc:  # noqa: BLE001
                excluded_ids.update(cached_ids.get(section_id, set()))
                processed.add(section_id)
                logger.warning("🗂 [curation] auto %s/%s failed: %r", section_id, gender, exc)
    return written


async def _auto_refresh_due(pool: AsyncConnectionPool) -> bool:
    today = datetime.now(tz=_KST).date()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT count(*)
            FROM ai.curation_sections s
            WHERE s.slot_type = 'auto'
              AND s.section_id = ANY(%s)
              AND cardinality(s.product_ids) = %s
              AND (s.products_updated_at AT TIME ZONE 'Asia/Seoul')::date = %s
              AND EXISTS (
                  SELECT 1
                  FROM ai.curation_candidates c
                  WHERE c.section_id = s.section_id
                    AND c.gender = s.gender
                    AND c.generated_for = %s
              )
            """,
            (list(_AUTO_IDS), _SECTION_SIZE, today, today),
        )
        row = await cur.fetchone()
    return not row or int(row[0]) != len(_AUTO_IDS) * len(_GENDERS)


async def refresh_once(pool: AsyncConnectionPool) -> None:
    """auto 구좌 재계산 + 부정 시그널 귀속. 구좌 메타데이터는 손대지 않는다.

    구좌의 존재·제목·순서·활성 여부와 editorial 상품 목록은 어드민이 소유한다
    (이전에는 노션 동기화가 소유했다). 이 루프는 auto 구좌의 product_ids 만
    UPDATE 한다 — 행을 만들지도 지우지도 않는다.
    """
    auto_n = await refresh_auto_sections(pool) if await _auto_refresh_due(pool) else 0
    negative_n = 0
    try:
        from app.services.curation_taste import attribute_negative_impressions

        negative_n = await attribute_negative_impressions(pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("🗂 [curation] negative attribution failed: %r", exc)
    logger.info("🗂 [curation] refresh done · auto=%d negative=%d", auto_n, negative_n)


async def curation_refresh_loop() -> None:
    interval = max(900, settings.CURATION_REFRESH_INTERVAL_S)
    logger.info("🗂 [curation] refresh loop started · interval=%ds", interval)
    while True:
        try:
            from app.providers import db_pool

            pool = db_pool._pool  # noqa: SLF001
            if pool is not None:
                await refresh_once(pool)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("🗂 [curation] refresh cycle raised: %r", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
