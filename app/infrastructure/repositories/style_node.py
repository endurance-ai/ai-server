"""Style-node code → id resolver (SPEC-SEARCH-V6-STYLE-WIRING).

Single source for converting a Vision-supplied style_node letter
(`A`..`U` in current data) into the `style_nodes.id` bigint that
`search_products_v6(p_style_node_id ...)` accepts.

Why a cache:
  - The `style_nodes` table holds 21 active rows and is essentially static
    (curated taxonomy). A per-search SELECT is wasteful.
  - DB is the source of truth so a re-seed/insertion is picked up on the
    next service restart without code change.

Fail-open contract:
  - Cache warming runs at FastAPI lifespan startup. If DB_DSN is empty
    (dev / DEMO_MODE) or warming fails, a hardcoded 21-letter fallback
    (A→1 .. U→21, mirroring current production ordering) is used. Search
    keeps working even with the fallback wrong: an unknown id makes the
    RPC's rung-1 row count zero, which gracefully drops to rung-2
    (degraded=true) — never worse than today's "always-None" baseline.
"""

from __future__ import annotations

import logging
import string
from typing import Final

logger = logging.getLogger(__name__)

# A=1, B=2, ... U=21 — matches current production seed (dev-app inspected
# 2026-06-14). Replaced in-place by `warm_cache` when DB is reachable.
_FALLBACK: Final[dict[str, int]] = {
    letter: idx + 1 for idx, letter in enumerate(string.ascii_uppercase[:21])
}

_cache: dict[str, int] = dict(_FALLBACK)
_warmed: bool = False


def code_to_id(code: str | None) -> int | None:
    """Resolve a style-node letter to its bigint id.

    Returns None when the input is empty, unknown, or not a single
    ASCII letter. Callers should pass the result straight to
    `SearchRepository.build_params(style_node_id=...)`.
    """
    if not code:
        return None
    key = code.strip().upper()
    if len(key) != 1 or not key.isascii() or not key.isalpha():
        return None
    return _cache.get(key)


async def warm_cache() -> None:
    """Populate the in-memory map from `public.style_nodes`. Lifespan-safe.

    Fail-open: on any failure (no pool, no DSN, query error) the existing
    fallback map is kept. Logs the outcome at INFO so a misconfiguration is
    visible without breaking the app.
    """
    global _warmed
    from app.core.config import settings
    from app.providers import db_pool

    if not settings.DB_DSN:
        logger.info("[STYLE_NODE][startup] DB_DSN empty — using fallback A..U (n=%d)", len(_cache))
        _warmed = True
        return

    pool = db_pool._pool  # noqa: SLF001 — read-only check
    if pool is None:
        logger.info("[STYLE_NODE][startup] db_pool not initialized — using fallback A..U")
        _warmed = True
        return

    try:
        async def _query() -> list[tuple[str, int]]:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT code, id FROM public.style_nodes WHERE is_active = true"
                )
                rows = await cur.fetchall()
                return [(str(r[0]).strip().upper(), int(r[1])) for r in rows]

        rows = db_pool.run_in_pool_loop(_query())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STYLE_NODE][startup] cache warm failed (%s) — keeping fallback",
            type(exc).__name__,
        )
        _warmed = True
        return

    if not rows:
        logger.warning("[STYLE_NODE][startup] style_nodes returned 0 active rows — keeping fallback")
        _warmed = True
        return

    _cache.clear()
    _cache.update(dict(rows))
    _warmed = True
    logger.info("[STYLE_NODE][startup] cache warmed n=%d sample=%s", len(_cache), sorted(_cache.items())[:3])


def is_warmed() -> bool:
    """True once `warm_cache()` has run (success OR fallback)."""
    return _warmed


def snapshot() -> dict[str, int]:
    """Return a copy of the current cache — for tests/observability only."""
    return dict(_cache)
