"""Notion-backed curation metadata sync and daily auto-candidate refresh.

The app never reads Notion. This service copies the operating database into
Supabase/Postgres, then refreshes auto products once per KST day.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings
from app.services.curation_taste import feature_pairs

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_GENDERS = ("women", "men")
_AUTO_IDS = ("popular", "trending-search", "under-100")
_SECTION_SIZE = 12
_CANDIDATES_PER_POOL = 60
_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

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


# 성별 3단 다리 — search_products_v6.sql 과 같은 계단을 쓴다.
#   ① product_features (VLM 스칼라 'men'|'women'|'unisex')
#   ② products.gender  (크롤러 레거시 배열)
#   ③ 둘 다 없으면 제외
# 검색은 ③에서 fail-open 하지만 큐레이션은 fail-closed 다. 메인 피드에 성별이
# 어긋난 상품이 노출되는 비용이 신규 상품 등장이 VLM 배치 한 주기 늦는 비용보다
# 크고, 후보 풀(60)이 슬롯(12)보다 넉넉해 굶지 않는다.
# 두 계단 모두 unisex 를 제외한다 — 명시 라벨만 올린다(기존 동작 보존).
# 🧹 VLM gender 커버리지가 충분해지면 ②를 지우고 products.gender 를 DROP 한다.
PRODUCT_FEATURES_JOIN = "LEFT JOIN public.product_features pf ON pf.product_id = p.id"

GENDER_MATCH_SQL = """
        CASE
            WHEN pf.feature_metadata->>'gender' IS NOT NULL
                THEN pf.feature_metadata->>'gender' = %(gender)s
            WHEN p.gender IS NOT NULL AND cardinality(p.gender) > 0
                THEN %(gender)s = ANY(p.gender) AND NOT ('unisex' = ANY(p.gender))
            ELSE false
        END
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
        AND COALESCE(bn.wiki->>'status', '') NOT IN ('제외', 'excluded', 'discontinued')
        AND NOT (COALESCE(p.category, '') = ANY(%(excluded_categories)s))
        AND NOT (COALESCE(p.subcategory, '') = ANY(%(excluded_subcategories)s))
        {winter_name_gate}
    """


def _candidate_sql(section_id: str) -> str:
    if section_id == "popular":
        signal_ctes = """
            views AS (
                SELECT product_id, count(*)::float AS n
                FROM ai.product_views
                WHERE viewed_at > now() - interval '7 days'
                GROUP BY product_id
            ),
            saves AS (
                SELECT product_id::bigint AS product_id, count(*)::float AS n
                FROM ai.saves
                WHERE product_id ~ '^[0-9]+$'
                  AND created_at > now() - interval '7 days'
                GROUP BY product_id::bigint
            ),
            outbound AS (
                SELECT (metadata->>'product_id')::bigint AS product_id, count(*)::float AS n
                FROM ai.taste_signal_events
                WHERE signal_type = 'outbound'
                  AND occurred_at > now() - interval '7 days'
                  AND metadata->>'product_id' ~ '^[0-9]+$'
                GROUP BY (metadata->>'product_id')::bigint
            )
        """
        score = """
            COALESCE(v.n, 0) + 3 * COALESCE(sv.n, 0) + 4 * COALESCE(o.n, 0)
                + ln(1 + COALESCE(p.review_count, 0))
        """
        joins = """
            LEFT JOIN views v ON v.product_id = p.id
            LEFT JOIN saves sv ON sv.product_id = p.id
            LEFT JOIN outbound o ON o.product_id = p.id
        """
        extra = ""
    elif section_id == "trending-search":
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
            )
        """
        score = "greatest(0, COALESCE(rs.n, 0) - COALESCE(ps.n, 0))"
        joins = """
            LEFT JOIN recent_search rs ON rs.product_id = p.id
            LEFT JOIN prior_search ps ON ps.product_id = p.id
        """
        extra = ""
    else:
        signal_ctes = ""
        score = "0::float"
        joins = ""
        extra = "AND p.price <= 150000"

    signal_prefix = f"{signal_ctes}," if signal_ctes else ""
    return f"""
        WITH
        {signal_prefix}
        eligible AS (
            SELECT
                p.id AS product_id,
                COALESCE(NULLIF(lower(btrim(p.brand)), ''), p.id::text) AS brand_key,
                p.brand_node_id,
                bn.primary_style_node_id AS style_node_id,
                COALESCE(bn.wiki->>'status', '') IN ('완료', '진행중') AS is_hot,
                {score} AS base_score
            FROM public.products p
            LEFT JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
            {PRODUCT_FEATURES_JOIN}
            {joins}
            WHERE {_quality_sql()}
              {extra}
        ),
        brand_limited AS (
            SELECT *,
                row_number() OVER (
                    PARTITION BY is_hot, brand_key
                    ORDER BY base_score DESC,
                             md5(product_id::text || current_date::text)
                ) AS brand_rank
            FROM eligible
        ),
        pool_ranked AS (
            SELECT *,
                row_number() OVER (
                    PARTITION BY is_hot
                    ORDER BY base_score DESC,
                             md5(product_id::text || current_date::text)
                ) AS pool_rank
            FROM brand_limited
            WHERE brand_rank <= 6
        )
        SELECT product_id, is_hot, base_score, pool_rank, brand_key,
               brand_node_id, style_node_id
        FROM pool_ranked
        WHERE pool_rank <= {_CANDIDATES_PER_POOL}
        ORDER BY is_hot DESC, pool_rank
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
    """Select hot 8 + overall 4 with per-section brand cap and stable ties.

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
    selected: list[int] = []
    brands: Counter[str] = Counter()

    def take(source: list[dict[str, Any]], target: int) -> None:
        for row in source:
            if len(selected) >= target:
                return
            pid = int(row["product_id"])
            brand = str(row["brand_key"])
            if pid in selected or brands[brand] >= 2:
                continue
            selected.append(pid)
            brands[brand] += 1

    take([r for r in ranked if r["is_hot"]], 8)
    # Do not weaken the quota when hot inventory is short.
    if len(selected) < 8:
        return [] if require_full else selected
    take([r for r in ranked if not r["is_hot"]], _SECTION_SIZE)
    if require_full and len(selected) != _SECTION_SIZE:
        return []
    return selected


# ── Notion operating database ────────────────────────────────────────────────


def _notion_plain_text(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    fragments = prop.get("title") or prop.get("rich_text") or []
    return "".join(f.get("plain_text", "") for f in fragments).strip()


def _parse_product_ids(raw: str) -> list[int]:
    return list(dict.fromkeys(int(tok) for tok in re.findall(r"\d+", raw)))[:_SECTION_SIZE]


def _parse_notion_page(page: dict[str, Any]) -> dict[str, Any] | None:
    props = page.get("properties", {})

    def get(name: str) -> dict[str, Any]:
        return props.get(name) or {}

    title = _notion_plain_text(get("구좌명"))
    section_id = _notion_plain_text(get("구좌 ID")).strip().lower()
    slot_type = ((get("slot_type").get("select") or {}).get("name") or "").strip().lower()
    if not title or slot_type not in ("auto", "editorial"):
        return None
    if slot_type == "auto" and section_id not in _AUTO_IDS:
        return None
    if slot_type == "editorial" and not re.fullmatch(r"editorial-[a-z0-9]+(?:-[a-z0-9]+)*", section_id):
        return None

    gender_options = get("gender_scope").get("multi_select") or []
    genders = [str(o.get("name", "")).lower() for o in gender_options]
    genders = [g for g in _GENDERS if g in genders]
    # The connected operating DB is a legacy Notion database whose schema API
    # cannot add columns. Its three fixed auto rows are intentionally shared by
    # both genders; keep those rows syncable until gender_scope is added in UI.
    if not genders and slot_type == "auto":
        genders = list(_GENDERS)
    if not genders:
        return None

    return {
        "source_page_id": page["id"],
        "section_id": section_id,
        "slot_type": slot_type,
        "title": title,
        "subtitle": _notion_plain_text(get("서브타이틀")) or None,
        "sort_order": int(get("순서").get("number") or 100),
        "is_active": bool(get("활성").get("checkbox", False)),
        "product_ids": _parse_product_ids(_notion_plain_text(get("상품"))),
        "genders": genders,
    }


def _validate_snapshot(sections: list[dict[str, Any]]) -> None:
    ids = [s["section_id"] for s in sections]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate curation section id")
    for gender in _GENDERS:
        active = [s for s in sections if s["is_active"] and gender in s["genders"]]
        if not 3 <= len(active) <= 6:
            raise ValueError(f"{gender}: active sections must be 3..6")
        ordered = sorted(active, key=lambda s: (s["sort_order"], s["section_id"]))
        seen_editorial = False
        for section in ordered:
            seen_editorial = seen_editorial or section["slot_type"] == "editorial"
            if seen_editorial and section["slot_type"] == "auto":
                raise ValueError(f"{gender}: auto section after editorial")
            if section["slot_type"] == "editorial" and len(section["product_ids"]) != _SECTION_SIZE:
                raise ValueError(f"{section['section_id']}: active editorial requires 12 unique products")


async def _fetch_notion_pages() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            body: dict[str, Any] = {"page_size": 50}
            if cursor:
                body["start_cursor"] = cursor
            response = await client.post(
                f"{_NOTION_API}/databases/{settings.NOTION_CURATION_DB_ID}/query",
                headers={
                    "Authorization": f"Bearer {settings.NOTION_TOKEN}",
                    "Notion-Version": _NOTION_VERSION,
                },
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")


async def sync_notion_sections(pool: AsyncConnectionPool) -> int:
    if not (settings.NOTION_TOKEN and settings.NOTION_CURATION_DB_ID):
        return 0
    parsed = [s for page in await _fetch_notion_pages() if (s := _parse_notion_page(page)) is not None]
    _validate_snapshot(parsed)
    seen: list[str] = []
    written = 0
    async with pool.connection() as conn, conn.cursor() as cur:
        for section in parsed:
            for gender in section["genders"]:
                seen.append(f"{section['section_id']}:{gender}")
                product_ids = section["product_ids"] if section["slot_type"] == "editorial" else []
                await cur.execute(
                    """
                    INSERT INTO ai.curation_sections
                        (section_id, gender, slot_type, title, subtitle, product_ids,
                         sort_order, is_active, source_page_id, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (section_id, gender) DO UPDATE SET
                        slot_type = EXCLUDED.slot_type,
                        title = EXCLUDED.title,
                        subtitle = EXCLUDED.subtitle,
                        product_ids = CASE
                            WHEN EXCLUDED.slot_type = 'editorial' THEN EXCLUDED.product_ids
                            ELSE ai.curation_sections.product_ids
                        END,
                        sort_order = EXCLUDED.sort_order,
                        is_active = EXCLUDED.is_active,
                        source_page_id = EXCLUDED.source_page_id,
                        updated_at = now()
                    """,
                    (
                        section["section_id"],
                        gender,
                        section["slot_type"],
                        section["title"],
                        section["subtitle"],
                        product_ids,
                        section["sort_order"],
                        section["is_active"],
                        section["source_page_id"],
                    ),
                )
                written += 1
        await cur.execute(
            """
            UPDATE ai.curation_sections
            SET is_active = false, updated_at = now()
            WHERE NOT ((section_id || ':' || gender) = ANY(%s)) AND is_active
            """,
            (seen or [""],),
        )
        await conn.commit()
    return written


# Backward-compatible name used by existing tests/callers.
sync_editorial_sections = sync_notion_sections


# ── Daily auto candidates ────────────────────────────────────────────────────


async def refresh_auto_sections(
    pool: AsyncConnectionPool,
    section_ids: tuple[str, ...] = _AUTO_IDS,
    *,
    strict_quota: bool = True,
) -> int:
    generated_for = datetime.now(tz=_KST).date()
    written = 0
    for gender in _GENDERS:
        excluded_ids: set[int] = set()
        for section_id in section_ids:
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
                written += 1
            except Exception as exc:  # noqa: BLE001
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
    notion_n = 0
    try:
        notion_n = await sync_notion_sections(pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("🗂 [curation] notion snapshot rejected: %r", exc)
    auto_n = await refresh_auto_sections(pool) if await _auto_refresh_due(pool) else 0
    negative_n = 0
    try:
        from app.services.curation_taste import attribute_negative_impressions

        negative_n = await attribute_negative_impressions(pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("🗂 [curation] negative attribution failed: %r", exc)
    logger.info(
        "🗂 [curation] refresh done · notion=%d auto=%d negative=%d",
        notion_n,
        auto_n,
        negative_n,
    )


async def curation_refresh_loop() -> None:
    interval = max(900, settings.CURATION_REFRESH_INTERVAL_S)
    logger.info("🗂 [curation] sync loop started · notion interval=%ds", interval)
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
