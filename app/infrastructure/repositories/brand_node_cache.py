"""Brand-node attribute cache (SPEC-PERSONALIZE-RERANK).

Personalization rerank needs cheap lookup of `brand_nodes.attributes`
(vibe, price_tier, formality, gender_lean, era_reference,
price_min_usd, price_max_usd, primary_style_node_id) keyed by the
normalized brand string that the v6 RPC returns as `brand`.

Why memory cache:
  - `brand_nodes` is 2,899 rows curated by the data team and changes
    on the order of weeks. A per-search SELECT is wasteful and adds
    latency to the hot path.
  - Total payload is small (~few hundred KB) and the working set is
    a single dict — well within process memory.

Fail-open contract:
  - Warming runs at FastAPI lifespan startup. If `DB_DSN` is empty or
    warming fails, the cache stays empty and `lookup()` returns None
    for every brand → rerank degrades to a no-op (returns RPC order
    unchanged). Search keeps working.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

# Same normalization the brand_nodes.brand_name_normalized column uses
# (visible in dev-app rows): lowercase, strip, collapse internal whitespace
# and punctuation noise. Centralized here so candidate-side lookup and
# cache-key generation never drift.
_NORM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def normalize_brand(brand: str | None) -> str:
    """Canonical brand key — '1017 ALYX 9SM' / '1017 Alyx 9SM' → '1017alyx9sm'."""
    if not brand:
        return ""
    return _NORM_RE.sub("", brand.lower())


@dataclass(frozen=True)
class BrandAttributes:
    """Subset of brand_nodes columns needed by rerank scoring.

    All fields default to safe empties so missing JSONB keys never raise.
    `vibe` is the most populous signal (75~91% coverage on dev-app),
    `price_tier` / `gender_lean` are next.
    """

    brand_name: str = ""
    primary_style_node_id: int | None = None
    vibe: tuple[str, ...] = ()
    price_tier: str = ""
    formality: str = ""
    gender_lean: str = ""
    era_reference: str = ""
    price_min_usd: float | None = None
    price_max_usd: float | None = None


_cache: dict[str, BrandAttributes] = {}
_warmed: bool = False


def lookup(brand: str | None) -> BrandAttributes | None:
    """Return cached attributes for `brand` or None when unknown.

    `brand` is the verbatim string from v6 RPC rows (`brand` column).
    Normalization is applied here so callers stay simple.
    """
    if not brand:
        return None
    return _cache.get(normalize_brand(brand))


def is_warmed() -> bool:
    return _warmed


def size() -> int:
    return len(_cache)


def _coerce_vibe(raw: Any) -> tuple[str, ...]:
    """`attributes->'vibe'` is stored as a JSON-encoded list-of-strings.

    On dev-app the values are e.g. `'["minimalist-architectural", "old-money"]'`
    after `->>`, or already a Python list when fetched as jsonb. Accept both
    shapes and return a lowercase tuple. Anything unparseable → empty tuple
    (the rerank degrades to "no vibe signal" for that brand, never errors).
    """
    if raw is None:
        return ()
    if isinstance(raw, list):
        return tuple(str(v).strip().lower() for v in raw if v)
    if isinstance(raw, str):
        s = raw.strip()
        if not s or s == "[]":
            return ()
        # Strip JSON brackets and quotes; split on commas. Tolerant parser —
        # the JSONB shape on dev-app is well-formed, but we never want a stray
        # whitespace / quoting glitch to abort the warm.
        s = s.strip("[]")
        parts = [p.strip().strip('"').strip("'").lower() for p in s.split(",")]
        return tuple(p for p in parts if p)
    return ()


async def warm_cache() -> None:
    """Populate from `public.brand_nodes`. Lifespan-safe.

    Reads only the rows the rerank actually needs. Skips brands without a
    `brand_name_normalized` (cannot match candidates anyway).
    """
    global _warmed
    from app.core.config import settings
    from app.providers import db_pool

    if not settings.DB_DSN:
        logger.info("[BRAND_NODE_CACHE][startup] DB_DSN empty — cache stays empty (rerank no-op)")
        _warmed = True
        return

    pool = db_pool._pool  # noqa: SLF001
    if pool is None:
        logger.info("[BRAND_NODE_CACHE][startup] db_pool not initialized — cache stays empty")
        _warmed = True
        return

    sql = """
        SELECT brand_name, brand_name_normalized, primary_style_node_id,
               attributes->'vibe' AS vibe,
               attributes->>'price_tier' AS price_tier,
               attributes->>'formality' AS formality,
               attributes->>'gender_lean' AS gender_lean,
               attributes->>'era_reference' AS era_reference,
               price_min_usd, price_max_usd
        FROM public.brand_nodes
        WHERE brand_name_normalized IS NOT NULL AND brand_name_normalized <> ''
    """

    try:
        async def _query() -> list[tuple[Any, ...]]:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(sql)
                return await cur.fetchall()

        rows = db_pool.run_in_pool_loop(_query())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[BRAND_NODE_CACHE][startup] warm failed (%s) — cache stays empty (rerank no-op)",
            type(exc).__name__,
        )
        _warmed = True
        return

    built: dict[str, BrandAttributes] = {}
    for r in rows:
        # The `brand_name_normalized` column in the DB matches our regex
        # most of the time but not always (curated edits). Use it verbatim
        # if present AND use the regex on `brand_name` as a secondary key
        # so candidates matched via either path resolve.
        brand_name = str(r[0] or "")
        db_norm = str(r[1] or "").strip().lower()
        regex_norm = normalize_brand(brand_name)
        attrs = BrandAttributes(
            brand_name=brand_name,
            primary_style_node_id=int(r[2]) if r[2] is not None else None,
            vibe=_coerce_vibe(r[3]),
            price_tier=str(r[4] or "").strip().lower(),
            formality=str(r[5] or "").strip().lower(),
            gender_lean=str(r[6] or "").strip().lower(),
            era_reference=str(r[7] or "").strip().lower(),
            price_min_usd=float(r[8]) if r[8] is not None else None,
            price_max_usd=float(r[9]) if r[9] is not None else None,
        )
        if db_norm:
            built[db_norm] = attrs
        if regex_norm and regex_norm not in built:
            built[regex_norm] = attrs

    _cache.clear()
    _cache.update(built)
    _warmed = True
    logger.info(
        "[BRAND_NODE_CACHE][startup] warmed brands=%d keys=%d sample_keys=%s",
        len({a.brand_name for a in built.values()}),
        len(built),
        list(built.keys())[:3],
    )
