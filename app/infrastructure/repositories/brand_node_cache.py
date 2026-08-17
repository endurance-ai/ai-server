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

# Hangul-syllable key. brand_name is frequently BILINGUAL — 'MONDAY EDITION
# (먼데이에디션)', '오베르 (AUBER)' — or purely Korean ('킨더살몬'). The Latin
# normalizer above deletes every Hangul codepoint, so a Korean-language brand
# query ('먼데이에디션만 보여줘') collapsed to '' and never matched. This keeps
# only Hangul syllables (drops spaces / punctuation / Latin), giving Korean
# surface forms their own stable key alongside the Latin one.
_HANGUL_RE: Final[re.Pattern[str]] = re.compile(r"[^가-힣]+")

# Parenthetical splitter — 'MONDAY EDITION (먼데이에디션)' → outer 'MONDAY
# EDITION' + inner '먼데이에디션'. Both half-width and full-width parens appear
# in the curated data.
_PAREN_RE: Final[re.Pattern[str]] = re.compile(r"[（(][^）)]*[）)]")
_PAREN_INNER_RE: Final[re.Pattern[str]] = re.compile(r"[（(]\s*([^）)]*?)\s*[）)]")

# Latin word tokens (for the initials-acronym fallback: 'Post Archive Faction'
# → 'paf').
_LATIN_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")


def normalize_brand(brand: str | None) -> str:
    """Latin canonical key — '1017 ALYX 9SM' / '1017 Alyx 9SM' → '1017alyx9sm'.

    Hangul-destroying by design (kept for the rerank / diversity lookup path
    and the /brands/search prefix match). Korean surfaces are handled by
    ``normalize_brand_ko`` and the multi-key alias index below.
    """
    if not brand:
        return ""
    return _NORM_RE.sub("", brand.lower())


def normalize_brand_ko(brand: str | None) -> str:
    """Hangul-only key — 'MONDAY EDITION (먼데이에디션)' / '먼데이 에디션' →
    '먼데이에디션'. Empty when the input carries no Hangul."""
    if not brand:
        return ""
    return _HANGUL_RE.sub("", brand)


def _strip_parens(brand: str) -> str:
    """Drop parenthetical spans — the 'outer' surface of a bilingual name."""
    return _PAREN_RE.sub(" ", brand).strip()


def _acronym(latin_source: str) -> str:
    """Initials acronym of a multi-word Latin surface — 'Post Archive Faction'
    → 'paf'. Empty for single-word names (acronym == the word, useless)."""
    words = _LATIN_WORD_RE.findall(latin_source.lower())
    return "".join(w[0] for w in words) if len(words) >= 2 else ""


def _surface_keys(brand_name: str) -> set[str]:
    """All exact-match alias keys derivable from one raw `brand_name`.

    Covers Latin + Hangul forms of the whole string, the paren-stripped outer
    surface, and each parenthetical inner surface — so any single language the
    curator kept in `brand_name` (and the one they dropped from
    `brand_name_normalized`) is reachable from either language of query.
    """
    surfaces = [brand_name, _strip_parens(brand_name)]
    surfaces.extend(m.strip() for m in _PAREN_INNER_RE.findall(brand_name))
    keys: set[str] = set()
    for s in surfaces:
        lk = normalize_brand(s)
        if lk:
            keys.add(lk)
        kk = normalize_brand_ko(s)
        if kk:
            keys.add(kk)
    return keys


@dataclass(frozen=True)
class BrandAttributes:
    """Subset of brand_nodes columns needed by rerank scoring + diversity caps.

    All fields default to safe empties so missing JSONB keys never raise.
    `vibe` and `silhouette` are list-shaped (JSONB array of lowercase
    tokens); the others are single tokens. Coverage on dev-app (2026-06):
    vibe 78%, silhouette 91%, price_tier 75%, formality 75%,
    gender_lean 75%, era_reference 75%.
    """

    brand_name: str = ""
    brand_id: int | None = None
    primary_style_node_id: int | None = None
    vibe: tuple[str, ...] = ()
    silhouette: tuple[str, ...] = ()
    price_tier: str = ""
    formality: str = ""
    gender_lean: str = ""
    era_reference: str = ""
    price_min_usd: float | None = None
    price_max_usd: float | None = None


_cache: dict[str, BrandAttributes] = {}

# Brand-FILTER resolution index (distinct from `_cache`, which is single-attrs
# per key for the rerank/diversity lookup path). Maps every alias key — Latin
# or Hangul surface, paren inner, unambiguous acronym — to the list of
# canonical `brand_name`s that share a base identity. Duplicate nodes for the
# same real brand ('Post Archive Faction' #3922 + 'Post Archive Faction (PAF)'
# #95) group together so ONE alias ('paf' / '포스트아카이브팩션' / 'post archive
# faction') resolves to BOTH names — the v6 RPC's `bn.brand_name = ANY(...)`
# then matches products hung off either node (products split across the twins).
_filter_index: dict[str, list[str]] = {}
# Initials-acronym fallback, consulted only after an exact-alias miss and only
# when the acronym is unambiguous (maps to a single brand group).
_acronym_index: dict[str, list[str]] = {}
_warmed: bool = False


def lookup(brand: str | None) -> BrandAttributes | None:
    """Return cached attributes for `brand` or None when unknown.

    `brand` is the verbatim string from v6 RPC rows (`brand` column) — Latin,
    Korean, or bilingual. Tries the Latin key first, then the Hangul key, so a
    Korean-language product brand resolves for rerank/diversity too.
    """
    if not brand:
        return None
    hit = _cache.get(normalize_brand(brand))
    if hit is not None:
        return hit
    ko = normalize_brand_ko(brand)
    return _cache.get(ko) if ko else None


def resolve_brand_names(query: str | None) -> list[str] | None:
    """Resolve a free-form brand mention → canonical `brand_name`s for filtering.

    Accepts Korean ('포스트아카이브팩션'), English ('post archive faction'),
    parenthetical acronyms carried in the data ('paf' from '… (PAF)'), or an
    initials acronym as a last resort. Returns every canonical name in the
    matched brand group (dedup, stable order), or None when unrecognized
    (caller fails open — no filter). Never raises."""
    if not query or not isinstance(query, str) or not query.strip():
        return None
    for key in (normalize_brand(query), normalize_brand_ko(query)):
        if key and key in _filter_index:
            return list(_filter_index[key])
    # Acronym fallback (Latin only, unambiguous groups only).
    lk = normalize_brand(query)
    if lk and lk in _acronym_index and lk not in _filter_index:
        return list(_acronym_index[lk])
    return None


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
               attributes->'silhouette' AS silhouette,
               attributes->>'price_tier' AS price_tier,
               attributes->>'formality' AS formality,
               attributes->>'gender_lean' AS gender_lean,
               attributes->>'era_reference' AS era_reference,
               price_min_usd, price_max_usd, id
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
    # Filter-resolution scaffolding: group duplicate/bilingual nodes by a base
    # identity so one alias → every canonical name in the group. Aliases /
    # acronyms mapping to >1 distinct group are ambiguous and dropped (the
    # caller fails open rather than filter on the wrong brand).
    groups: dict[str, list[str]] = {}
    alias_to_groups: dict[str, set[str]] = {}
    acro_to_groups: dict[str, set[str]] = {}
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
            brand_id=int(r[11]) if r[11] is not None else None,
            primary_style_node_id=int(r[2]) if r[2] is not None else None,
            vibe=_coerce_vibe(r[3]),
            silhouette=_coerce_vibe(r[4]),
            price_tier=str(r[5] or "").strip().lower(),
            formality=str(r[6] or "").strip().lower(),
            gender_lean=str(r[7] or "").strip().lower(),
            era_reference=str(r[8] or "").strip().lower(),
            price_min_usd=float(r[9]) if r[9] is not None else None,
            price_max_usd=float(r[10]) if r[10] is not None else None,
        )
        # `_cache` (rerank/diversity single-attrs lookup): DB-norm + regex-norm
        # plus every Latin/Hangul surface key. First-writer-wins so a later
        # duplicate node never clobbers an earlier attrs entry.
        for key in {db_norm, regex_norm, *_surface_keys(brand_name)}:
            if key and key not in built:
                built[key] = attrs

        # Filter grouping — base identity is the paren-stripped surface
        # ('Post Archive Faction (PAF)' & 'Post Archive Faction' → same group).
        outer = _strip_parens(brand_name)
        group_key = (
            normalize_brand(outer)
            or normalize_brand_ko(outer)
            or normalize_brand(brand_name)
            or normalize_brand_ko(brand_name)
        )
        if not group_key:
            continue
        groups.setdefault(group_key, []).append(brand_name)
        for key in _surface_keys(brand_name):
            alias_to_groups.setdefault(key, set()).add(group_key)
        acro = _acronym(outer) or _acronym(brand_name)
        if acro:
            acro_to_groups.setdefault(acro, set()).add(group_key)

    # Dedup each group's canonical names (stable order).
    for gk in list(groups):
        groups[gk] = list(dict.fromkeys(groups[gk]))
    # Exact-alias index — keep unambiguous aliases only.
    filt: dict[str, list[str]] = {key: groups[next(iter(gks))] for key, gks in alias_to_groups.items() if len(gks) == 1}
    # Acronym fallback — unambiguous AND not already an exact alias.
    acro_idx: dict[str, list[str]] = {
        key: groups[next(iter(gks))] for key, gks in acro_to_groups.items() if len(gks) == 1 and key not in filt
    }

    _cache.clear()
    _cache.update(built)
    _filter_index.clear()
    _filter_index.update(filt)
    _acronym_index.clear()
    _acronym_index.update(acro_idx)
    _warmed = True
    logger.info(
        "[BRAND_NODE_CACHE][startup] warmed brands=%d keys=%d filter_aliases=%d acronyms=%d sample_keys=%s",
        len({a.brand_name for a in built.values()}),
        len(built),
        len(filt),
        len(acro_idx),
        list(built.keys())[:3],
    )
