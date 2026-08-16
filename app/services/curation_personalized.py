"""Request-time product selection for conditional personalized curation slots."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.services.curation_refresh import PRODUCT_FEATURES_JOIN, _quality_sql, _query_params
from app.services.curation_taste import feature_pairs


async def personalized_product_ids(
    cur: Any,
    *,
    section_id: str,
    user_id: UUID,
    gender: str,
    excluded_ids: set[int],
    taste_scores: dict[int, float],
    feature_scores: dict[tuple[str, str], float],
) -> list[int]:
    """Return every eligible ID, or an empty list when the slot condition is unmet."""
    params = {
        **_query_params(gender),
        "user_id": user_id,
        "excluded_ids": list(excluded_ids),
    }

    if section_id == "recently-viewed":
        await cur.execute(
            f"""
            WITH recent AS (
                SELECT product_id, max(viewed_at) AS last_viewed_at
                FROM ai.product_views
                WHERE user_id = %(user_id)s
                GROUP BY product_id
            )
            SELECT p.id
            FROM recent r
            JOIN public.products p ON p.id = r.product_id
            {PRODUCT_FEATURES_JOIN}
            WHERE NOT (p.id = ANY(%(excluded_ids)s))
              AND {_quality_sql()}
            ORDER BY r.last_viewed_at DESC, p.id DESC
            """,  # noqa: S608 -- interpolated SQL is composed only from module-owned fragments
            params,
        )
        return [int(row[0]) for row in await cur.fetchall()]

    if section_id == "saved-on-sale":
        await cur.execute(
            f"""
            SELECT p.id
            FROM ai.saves s
            JOIN public.products p
              ON s.product_id ~ '^[0-9]+$'
             AND p.id = s.product_id::bigint
            {PRODUCT_FEATURES_JOIN}
            WHERE s.user_id = %(user_id)s
              AND p.original_price IS NOT NULL
              AND p.sale_price IS NOT NULL
              AND p.sale_price < p.original_price
              AND NOT (p.id = ANY(%(excluded_ids)s))
              AND {_quality_sql()}
            ORDER BY ((p.original_price - p.sale_price) / NULLIF(p.original_price, 0)) DESC,
                     s.created_at DESC, p.id DESC
            """,  # noqa: S608 -- interpolated SQL is composed only from module-owned fragments
            params,
        )
        return [int(row[0]) for row in await cur.fetchall()]

    if section_id == "favorite-brands":
        await cur.execute(
            f"""
            WITH ranked AS (
                SELECT p.id,
                       p.brand_node_id,
                       row_number() OVER (
                           PARTITION BY p.brand_node_id
                           ORDER BY COALESCE(p.review_count, 0) DESC, p.id DESC
                       ) AS brand_rank
                FROM ai.user_brand_picks ubp
                JOIN public.products p ON p.brand_node_id = ubp.brand_id
                {PRODUCT_FEATURES_JOIN}
                WHERE ubp.user_id = %(user_id)s
                  AND NOT (p.id = ANY(%(excluded_ids)s))
                  AND {_quality_sql()}
            )
            SELECT id
            FROM ranked
            ORDER BY brand_rank ASC, brand_node_id ASC, id DESC
            """,  # noqa: S608 -- interpolated SQL is composed only from module-owned fragments
            params,
        )
        return [int(row[0]) for row in await cur.fetchall()]

    if section_id != "taste-picks":
        return []

    positive_style_ids = [style_id for style_id, score in taste_scores.items() if score > 0]
    positive_features: dict[str, list[str]] = {
        axis: [
            value for (candidate_axis, value), score in feature_scores.items() if candidate_axis == axis and score > 0
        ]
        for axis in ("color", "fit", "pattern", "neckline", "material")
    }
    if not positive_style_ids and not any(positive_features.values()):
        return []

    params.update(
        {
            "positive_style_ids": positive_style_ids,
            "positive_colors": positive_features["color"],
            "positive_fits": positive_features["fit"],
            "positive_patterns": positive_features["pattern"],
            "positive_necklines": positive_features["neckline"],
            "positive_materials": positive_features["material"],
        }
    )
    await cur.execute(
        f"""
        SELECT p.id, bn.primary_style_node_id, pf.feature_metadata
        FROM public.products p
        LEFT JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
        {PRODUCT_FEATURES_JOIN}
        WHERE NOT (p.id = ANY(%(excluded_ids)s))
          AND {_quality_sql()}
          AND (
              bn.primary_style_node_id = ANY(CAST(%(positive_style_ids)s AS bigint[]))
              OR pf.feature_metadata->>'primary_color' = ANY(CAST(%(positive_colors)s AS text[]))
              OR pf.feature_metadata->>'fit' = ANY(CAST(%(positive_fits)s AS text[]))
              OR pf.feature_metadata->>'pattern' = ANY(CAST(%(positive_patterns)s AS text[]))
              OR pf.feature_metadata->>'neckline' = ANY(CAST(%(positive_necklines)s AS text[]))
              OR EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(
                      CASE
                          WHEN jsonb_typeof(pf.feature_metadata->'material') = 'array'
                          THEN pf.feature_metadata->'material'
                          ELSE '[]'::jsonb
                      END
                  ) AS material(value)
                  WHERE material.value = ANY(CAST(%(positive_materials)s AS text[]))
              )
          )
        ORDER BY COALESCE(p.review_count, 0) DESC, p.id DESC
        """,  # noqa: S608 -- interpolated SQL is composed only from module-owned fragments
        params,
    )
    selected: list[int] = []
    for product_id, style_node_id, metadata in await cur.fetchall():
        score = taste_scores.get(int(style_node_id), 0.0) if style_node_id is not None else 0.0
        score += sum(feature_scores.get(pair, 0.0) for pair in feature_pairs(metadata))
        if score > 0:
            selected.append(int(product_id))
    return selected
