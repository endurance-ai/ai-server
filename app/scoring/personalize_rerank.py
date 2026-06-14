"""Personalization rerank (SPEC-PERSONALIZE-RERANK).

Pure-function re-ordering of v6 RPC raw rows using the user's
`TasteProfile` × `brand_nodes.attributes`. Runs between `search_step`
and `diversify_step` so the brand/platform cap is applied to the new
order (no algorithm change in diversify).

Scoring:

    base = 1.0 - distance                                # RPC's natural fit
    + LIKED_BRAND_W   · liked_brands[normalized_brand]
    - DISLIKED_BRAND_W · disliked_brands[normalized_brand]
    + KEYWORD_W       · |liked_keywords ∩ brand.vibe|
    - KEYWORD_W       · |disliked_keywords ∩ brand.vibe|
    + PRICE_FIT_W     · (1 if candidate.price ∈ [profile.observed_min, max] else 0)
    - GENDER_MISMATCH_W · (1 if profile.gender disagrees with brand.gender_lean)

Fail-open:
  - No profile / no signal in the profile → return candidates unchanged.
  - Brand miss in cache → that candidate gets `base` only (no personalization
    contribution), but is NOT dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.infrastructure.memory.taste_profile import TasteProfile
from app.infrastructure.repositories.brand_node_cache import lookup as _lookup_brand
from app.infrastructure.repositories.brand_node_cache import normalize_brand

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankWeights:
    """Tuning surface for the score. All weights are additive to `1 - distance`.

    Defaults are set in `app.core.config.Settings.PERSONALIZE_*` and supplied
    by the caller (search_service); this dataclass keeps the rerank function
    independent of settings import so tests can pin any combo.
    """

    liked_brand: float = 0.10
    disliked_brand: float = 0.20
    keyword: float = 0.02
    price_fit: float = 0.05
    gender_mismatch: float = 0.10


# Gender-lean tokens used by brand_nodes.attributes.gender_lean
# (observed values on dev-app: mens, womens, unisex, womens-leaning).
# We treat 'unisex' and 'womens-leaning' as "no strong mismatch with anyone"
# so the penalty is only applied on the unambiguous mens↔womens conflict.
_GENDER_CONFLICT: dict[tuple[str, str], bool] = {
    ("men", "womens"): True,
    ("women", "mens"): True,
}


def _has_any_signal(p: TasteProfile) -> bool:
    """Skip rerank entirely when the profile has nothing to contribute.

    A pinned-but-empty profile (e.g., gender alone) is enough — gender
    mismatch on its own can still re-order. Cheaper than running the full
    scoring loop on every search of a brand-new user.
    """
    return bool(
        p.liked_brands
        or p.disliked_brands
        or p.liked_keywords
        or p.disliked_keywords
        or p.price_min_observed is not None
        or p.price_max_observed is not None
        or (p.gender or "").strip()
    )


def _price_fit_contribution(
    price: Any,
    p_min: int | None,
    p_max: int | None,
    weight: float,
) -> float:
    """+weight when price ∈ [p_min, p_max], 0 otherwise.

    Either bound being None is treated as +∞ on that side so a one-sided
    observation (only min seen) still steers toward "at least this price".
    A non-numeric or absent price contributes 0 (graceful — never raise).
    """
    if weight == 0.0:
        return 0.0
    if p_min is None and p_max is None:
        return 0.0
    try:
        v = int(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if p_min is not None and v < p_min:
        return 0.0
    if p_max is not None and v > p_max:
        return 0.0
    return weight


def _gender_penalty(profile_gender: str, brand_gender_lean: str, weight: float) -> float:
    if weight == 0.0 or not profile_gender or not brand_gender_lean:
        return 0.0
    key = (profile_gender.strip().lower(), brand_gender_lean.strip().lower())
    return weight if _GENDER_CONFLICT.get(key, False) else 0.0


def _score_candidate(
    c: dict[str, Any],
    profile: TasteProfile,
    w: RerankWeights,
) -> float:
    """Compute personalized score for one row. Higher = better."""
    distance = float(c.get("distance", 1.0))
    score = 1.0 - distance

    brand_text = str(c.get("brand") or "")
    norm = normalize_brand(brand_text)

    if norm:
        # Brand-level like/dislike — uses TasteProfile's existing
        # lower-cased normalized brand key dictionary. Keys in the profile
        # are produced by `reinforce_*_brand` which lowercases + strips,
        # so we match against the SAME lowercased brand_text (not the
        # punctuation-stripped norm) for the profile dicts. The cache key
        # is the punctuation-stripped form.
        brand_lc = brand_text.strip().lower()
        score += w.liked_brand * profile.liked_brands.get(brand_lc, 0.0)
        score -= w.disliked_brand * profile.disliked_brands.get(brand_lc, 0.0)

    attrs = _lookup_brand(brand_text)
    if attrs is not None:
        if attrs.vibe and (profile.liked_keywords or profile.disliked_keywords):
            vibe_set = set(attrs.vibe)
            score += w.keyword * sum(
                profile.liked_keywords.get(v, 0.0) for v in vibe_set
            )
            score -= w.keyword * sum(
                profile.disliked_keywords.get(v, 0.0) for v in vibe_set
            )
        score -= _gender_penalty(profile.gender or "", attrs.gender_lean, w.gender_mismatch)

    score += _price_fit_contribution(
        c.get("price"),
        profile.price_min_observed,
        profile.price_max_observed,
        w.price_fit,
    )
    return score


def rerank(
    candidates: list[dict[str, Any]],
    profile: TasteProfile | None,
    *,
    weights: RerankWeights,
) -> list[dict[str, Any]]:
    """Return `candidates` re-ordered by personalized score (desc).

    Stable sort: ties (e.g., score equal because no signal applied) keep
    their RPC order. Empty list / no profile / no signal → return the
    input unchanged.

    The function never drops candidates — diversify handles cap and dedup
    after this step.
    """
    if not candidates or profile is None or not _has_any_signal(profile):
        return candidates

    # Score once, then stable sort. Python's `sorted` is stable, so equal
    # scores fall back to original insertion order — preserves "RPC
    # distance ASC" as the deterministic tie-breaker.
    scored: list[tuple[float, dict[str, Any]]] = [
        (_score_candidate(c, profile, weights), c) for c in candidates
    ]
    reranked = [c for _, c in sorted(scored, key=lambda kv: -kv[0])]

    # Cheap observability — peek at the top-3 shuffle. Per-row scores are
    # debug-level to keep info logs scannable.
    if logger.isEnabledFor(logging.DEBUG):
        for i, (sc, c) in enumerate(sorted(scored, key=lambda kv: -kv[0])[:5]):
            logger.debug(
                "[rerank] #%d brand=%r price=%s score=%.4f distance=%.4f",
                i + 1,
                (c.get("brand") or "")[:40],
                c.get("price"),
                sc,
                float(c.get("distance", 1.0)),
            )

    return reranked
