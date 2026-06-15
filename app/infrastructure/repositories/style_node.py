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
from typing import Any, Final

logger = logging.getLogger(__name__)

# A=1, B=2, ... U=21 — matches current production seed (dev-app inspected
# 2026-06-14). Replaced in-place by `warm_cache` when DB is reachable.
_FALLBACK: Final[dict[str, int]] = {letter: idx + 1 for idx, letter in enumerate(string.ascii_uppercase[:21])}

_cache: dict[str, int] = dict(_FALLBACK)
# SPEC-SEARCH-V6-STYLE-WIRING follow-up: short prompt-ready digest of the
# 21 active style nodes — fed to the search_products / refine_search tool
# descriptions so the LLM has enough context to pick a letter on
# text-only turns (where Vision didn't run). Built once at warm_cache()
# from `name_en` + first three `keywords_en`. Empty list → tool description
# falls back to its base text (the digest just isn't appended).
_digest: list[str] = []
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

        async def _query() -> list[tuple[Any, ...]]:
            async with pool.connection() as conn, conn.cursor() as cur:
                # Also pull name_en + keywords_en so the prompt digest is built
                # from the SAME row set as the code↔id cache (no drift).
                await cur.execute(
                    "SELECT code, id, name_en, keywords_en FROM public.style_nodes WHERE is_active = true ORDER BY id"
                )
                return await cur.fetchall()

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
    _cache.update({str(r[0]).strip().upper(): int(r[1]) for r in rows})

    _digest.clear()
    for r in rows:
        letter = str(r[0]).strip().upper()
        name_en = str(r[2] or "").strip()
        kw = r[3] or []
        # First 3 keywords keep the prompt tight (~12 tokens per line × 21 = ~250
        # tokens added to the tool description). Skip rows with no name to avoid
        # noise lines.
        kw_head = ", ".join(str(k).strip() for k in kw[:3] if k)
        if not name_en:
            continue
        line = f"  - {letter}: {name_en}" + (f" — {kw_head}" if kw_head else "")
        _digest.append(line)

    _warmed = True
    logger.info(
        "[STYLE_NODE][startup] cache warmed n=%d digest_lines=%d",
        len(_cache),
        len(_digest),
    )

    # SPEC-SEARCH-V6-STYLE-WIRING text-only follow-up: append the 21-letter
    # digest to the search_products + refine_search tool descriptions so the
    # ReAct LLM has enough context to pick a letter on text-only turns. The
    # base description (single source) still owns the canonical-form rules;
    # the digest is appended once and idempotently. Failing to mutate the
    # REGISTRY is fail-open (logged warn, search keeps working).
    try:
        from app.agents import tool_registry as _reg

        header = "\n\n[STYLE NODES — pick `style_node_primary` letter when text alone implies one]\n"
        digest_block = header + "\n".join(_digest)
        for tool_name in ("search_products", "refine_search"):
            meta = _reg.REGISTRY.get(tool_name)
            if meta is None:
                continue
            base = meta["description"]
            # Idempotent: only append on first warm; subsequent warms (e.g. in
            # tests) skip if the marker is already present.
            if "[STYLE NODES —" not in base:
                meta["description"] = base + digest_block
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STYLE_NODE][startup] tool description mutate skipped (%s)",
            type(exc).__name__,
        )


def is_warmed() -> bool:
    """True once `warm_cache()` has run (success OR fallback)."""
    return _warmed


def snapshot() -> dict[str, int]:
    """Return a copy of the current cache — for tests/observability only."""
    return dict(_cache)


def digest_lines() -> list[str]:
    """Return the prompt-ready 21-letter digest lines. Empty until warm_cache runs."""
    return list(_digest)
