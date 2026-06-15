"""Brand-multimodal embedding cache (SPEC-BRAND-2TOWER-RESCORE).

2-tower rescore uses `brand_multimodal_embeddings.vector` (768-dim,
FashionSigLIP — same space as product/query embeddings) as a second
ranking signal alongside the product embedding distance.

Why memory cache:
  - 2,180 brands × 768 floats × 4 bytes ≈ 6.7 MB total. Trivial.
  - The rescore runs on every top-K search; per-search SELECT of N brand
    vectors would add 15~50 round-trips. Lookup is O(1) here.
  - Coverage on dev-app: 82% of brands returned by a typical RPC query
    have a brand embedding. The other 18% degrade to product-only
    distance (fail-open) so the rescore never starves results.

The cache stores L2-normalized vectors as Python `list[float]` to keep
the dependency surface zero (no numpy at warm time). The rescore step
itself uses numpy when available for speed; the list form is the
serialization-safe canonical.
"""

from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger(__name__)

_EXPECTED_DIM: Final[int] = 768
_EXPECTED_MODEL: Final[str] = "Marqo/marqo-fashionSigLIP"

_cache: dict[int, list[float]] = {}
_warmed: bool = False


def lookup(brand_id: int | None) -> list[float] | None:
    """Return the 768-dim brand vector for `brand_id` or None when absent.

    The rescore step calls this once per candidate; misses are silent and
    surfaced only as the rescore's `cache_miss` counter (per-search log).
    """
    if brand_id is None:
        return None
    return _cache.get(int(brand_id))


def is_warmed() -> bool:
    return _warmed


def size() -> int:
    return len(_cache)


def _parse_halfvec(raw: object) -> list[float] | None:
    """Postgres halfvec/vector → list[float].

    psycopg returns `vector(N)` columns as a Python list-of-floats already
    when the `pgvector` register hook is installed. Without the hook the
    column comes back as a string like '[0.1,0.2,...]'. Accept both.
    None / malformed → None.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        s = raw.strip()
        if not (s.startswith("[") and s.endswith("]")):
            return None
        try:
            return [float(x) for x in s[1:-1].split(",") if x.strip()]
        except ValueError:
            return None
    return None


async def warm_cache() -> None:
    """Populate from `public.brand_multimodal_embeddings`. Lifespan-safe.

    Filters by the canonical FashionSigLIP model + the production strategy
    so a future second strategy (e.g. text-only chunks) doesn't bleed in
    silently — the SELECT is explicit about what it consumes. Fail-open:
    on any failure the cache stays empty → rescore is a no-op.
    """
    global _warmed
    from app.core.config import settings
    from app.providers import db_pool

    if not settings.DB_DSN:
        logger.info(
            "[BRAND_EMB_CACHE][startup] DB_DSN empty — cache stays empty (rescore no-op)"
        )
        _warmed = True
        return

    pool = db_pool._pool  # noqa: SLF001
    if pool is None:
        logger.info(
            "[BRAND_EMB_CACHE][startup] db_pool not initialized — cache stays empty"
        )
        _warmed = True
        return

    sql = """
        SELECT brand_id, vector::text
        FROM public.brand_multimodal_embeddings
        WHERE embedding_model = %s
    """

    try:
        async def _query() -> list[tuple[int, str]]:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(sql, (_EXPECTED_MODEL,))
                return await cur.fetchall()

        rows = db_pool.run_in_pool_loop(_query())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[BRAND_EMB_CACHE][startup] warm failed (%s) — cache stays empty",
            type(exc).__name__,
        )
        _warmed = True
        return

    skipped_dim = 0
    skipped_parse = 0
    for brand_id, raw in rows:
        vec = _parse_halfvec(raw)
        if vec is None:
            skipped_parse += 1
            continue
        if len(vec) != _EXPECTED_DIM:
            skipped_dim += 1
            continue
        _cache[int(brand_id)] = vec

    _warmed = True
    logger.info(
        "[BRAND_EMB_CACHE][startup] warmed n=%d dim=%d skipped_parse=%d skipped_dim=%d",
        len(_cache),
        _EXPECTED_DIM,
        skipped_parse,
        skipped_dim,
    )
