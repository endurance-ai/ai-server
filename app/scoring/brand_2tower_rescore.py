"""Brand-anchored 2-tower rescore (SPEC-BRAND-2TOWER-RESCORE).

The v6 RPC returns rows ordered by `cosine(product_embedding,
query_embedding)`. This step adds a second tower — `cosine(brand_vector,
query_embedding)` — and blends:

    new_distance = α · product_distance + (1 − α) · brand_distance

where `brand_distance = 1 − cosine(brand_vector, query_embedding)`.

Effect: a candidate from a brand whose AVERAGE catalog visually matches
the query (think "Arc'teryx-like vibe") gets a small boost even when the
individual product photo is a slightly weaker match. Conversely, a
visually-close product from a brand that doesn't fit the query is pushed
down. This rescues "best-match brand, mid-match product" outcomes that
pure product-distance ranking misses.

Fail-open contract:
  - Brand miss (no brand_id in brand_node_cache, or no vector in
    brand_embedding_cache) → that candidate keeps its original distance.
  - `query_embedding` not 768-dim or norm zero → return rows unchanged.
  - Anything raises → caller catches and falls back to RPC order.

Pure function. The caller (search_service) decides α, performs the
sort, and updates `state.raw_candidates`.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.infrastructure.repositories.brand_embedding_cache import lookup as _brand_emb
from app.infrastructure.repositories.brand_node_cache import lookup as _brand_attr

logger = logging.getLogger(__name__)


def _cosine_distance(a: list[float], b: list[float]) -> float | None:
    """Return `1 − cos(a, b)` in [0, 2], or None when inputs are unusable.

    Matches Postgres' `<=>` semantics on `vector`/`halfvec` columns.
    """
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return None
    return 1.0 - dot / (math.sqrt(na) * math.sqrt(nb))


def rescore(
    candidates: list[dict[str, Any]],
    query_embedding: list[float] | None,
    *,
    alpha: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Recompute `distance` per candidate using the 2-tower formula and
    return rows sorted by the new distance ASC. Always returns a copy
    of each candidate dict (no in-place mutation of caller's rows).

    Stats dict shape: `{"hit": N, "miss": M, "kept": K}` for observability.
    `kept` is the count of rows whose distance was unchanged (brand miss).
    """
    stats = {"hit": 0, "miss": 0, "kept": 0}
    if not candidates:
        return candidates, stats
    if not query_embedding or len(query_embedding) == 0:
        stats["kept"] = len(candidates)
        return candidates, stats
    if not 0.0 < alpha <= 1.0:
        # Out-of-range alpha is a misconfiguration — be loud (warn) but
        # don't break search (return original order).
        logger.warning("[2tower] α=%.3f out of (0, 1] — skipping rescore", alpha)
        stats["kept"] = len(candidates)
        return candidates, stats

    one_minus_alpha = 1.0 - alpha
    out: list[dict[str, Any]] = []
    for c in candidates:
        original_distance = float(c.get("distance", 1.0))
        attrs = _brand_attr(c.get("brand"))
        brand_id = attrs.brand_id if attrs else None
        brand_vec = _brand_emb(brand_id) if brand_id is not None else None
        if brand_vec is None:
            stats["miss"] += 1
            stats["kept"] += 1
            out.append(c)
            continue
        bd = _cosine_distance(brand_vec, query_embedding)
        if bd is None:
            stats["miss"] += 1
            stats["kept"] += 1
            out.append(c)
            continue
        new_distance = alpha * original_distance + one_minus_alpha * bd
        # Shallow-copy + override distance so caller comparisons (e.g.
        # personalize_rerank's `1 - distance`) read the new value.
        out.append({**c, "distance": new_distance})
        stats["hit"] += 1

    out.sort(key=lambda r: float(r.get("distance", 1.0)))
    return out, stats
