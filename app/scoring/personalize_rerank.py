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
import math
from dataclasses import dataclass
from typing import Any

from app.infrastructure.memory.taste_profile import TasteProfile
from app.infrastructure.repositories.brand_node_cache import lookup as _lookup_brand
from app.infrastructure.repositories.brand_node_cache import normalize_brand
from app.scoring.feature_taste import mean_feature_pref

logger = logging.getLogger(__name__)


# ── 무드 태그 IDF 다운가중 ──────────────────────────────────────────────────
# product_features_v26.final_tags 는 recall 지향 게이트(style_gate.py)의 산물이라
# 정밀 임계가 안 걸려 흔한 태그가 비대하다(미니멀룩 52% = 카탈로그 절반 = 무드
# 신호로선 무정보). rerank 무드 보너스를 태그 희귀도(IDF)로 스케일해 흔한 태그의
# 부스트를 죽이고 희귀·변별 태그만 온전히 살린다.
# 스냅샷: 2026-09-13 dev product_features_v26 (has_final 239,481), df=태그빈도/has_final.
# 재-enrichment 로 분포가 바뀌면 아래 쿼리로 갱신:
#   WITH t AS (SELECT unnest(final_tags) tag FROM product_features_v26 WHERE final_tags IS NOT NULL)
#   SELECT tag, count(*)::float/(SELECT count(*) FROM product_features_v26 WHERE final_tags IS NOT NULL)
#   FROM t GROUP BY tag;
_MOOD_DF_SNAPSHOT: dict[str, float] = {
    "미니멀룩": 0.5205,
    "해체주의": 0.1761,
    "다크웨어": 0.1502,
    "그런지": 0.1403,
    "프레피룩": 0.1321,
    "스트릿": 0.1089,
    "핫걸": 0.0960,
    "y2k": 0.0955,
    "슬래커코어": 0.0920,
    "아메카지": 0.0916,
    "나이트클러빙": 0.0879,
    "프렌치시크": 0.0841,
    "시티보이": 0.0821,
    "고프코어": 0.0787,
    "올드머니룩": 0.0614,
    "러닝코어": 0.0485,
    "코티지코어": 0.0420,
    "리조트": 0.0410,
    "모리걸": 0.0401,
    "워크웨어": 0.0380,
    "그래놀라코어": 0.0352,
    "포엣코어": 0.0341,
    "코케트": 0.0335,
    "발레코어": 0.0255,
    "블록코어": 0.0236,
    "란제리코어": 0.0211,
    "애슬레저/요가": 0.0197,
}
# df→가중 정규화 앵커: df≥60%(무정보)→0, df≤5%(변별)→1.0. 사이는 log 스케일 선형.
_MOOD_IDF_LO = math.log(1.0 / 0.60)
_MOOD_IDF_HI = math.log(1.0 / 0.05)


def _mood_idf_weight(tag: str) -> float:
    """무드 태그 → [0,1] 희귀도 가중(흔할수록 0). 미지 태그는 억제 안 함(1.0)."""
    df = _MOOD_DF_SNAPSHOT.get(tag.strip().lower())
    if df is None or df <= 0.0:
        return 1.0
    idf = math.log(1.0 / df)
    return max(0.0, min(1.0, (idf - _MOOD_IDF_LO) / (_MOOD_IDF_HI - _MOOD_IDF_LO)))


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
    feature: float = 0.15
    # 속성정렬 ("우와 비슷하다") — 쿼리 target fit/material/color/pattern ↔ 후보 feature_metadata.
    attr_fit: float = 0.0
    attr_material: float = 0.0
    attr_color: float = 0.0
    attr_pattern: float = 0.0
    attr_neckline: float = 0.0
    # v2.6 스타일 무드축 (product_features_v26.final_tags, 27 폐쇄값) — 하드필터 대체.
    attr_mood: float = 0.0
    # v2.6 enrichment 축 (product_features_v26.attr) — length/sleeve/leg_shape.
    attr_length: float = 0.0
    attr_sleeve_length: float = 0.0
    attr_leg_shape: float = 0.0
    # v2.6 스타일 디테일축 — surface(스칼라)/texture(배열)/design_details(배열).
    attr_surface: float = 0.0
    attr_texture: float = 0.0
    attr_design_details: float = 0.0
    # v2.6 비어패럴 조건부축(신발/가방/안경/주얼리) — 전부 스칼라, 공유 가중치.
    attr_nonapparel: float = 0.0
    # v2.6 wash(데님 워싱)/graphics(로고·프린트) — 스칼라.
    attr_wash: float = 0.0
    attr_graphics: float = 0.0


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


# v2.6 비어패럴 조건부 스칼라축 — 신발/가방/안경/주얼리. search_service 가 target 세팅 +
# _attach_feature_metadata 가 v26.attr 에서 머지, 여기서 공유 가중치로 정렬 가산.
NONAPPAREL_SCALAR_AXES: tuple[str, ...] = (
    "heel_type",
    "heel_height",
    "shaft",
    "shoe_toe",
    "bag_size",
    "bag_structure",
    "frame_shape",
    "metal_tone",
)


def _attr_align_bonus(c: dict[str, Any], w: RerankWeights, target_attrs: dict[str, set[str]]) -> float:
    """쿼리 target 속성(fit/material) ↔ 후보 feature_metadata 정렬 가산.
    color/subcategory 는 RPC 하드게이트라 제외. 개인화가 아니라 쿼리 의도이므로
    프로필 유무와 무관하게 적용된다 ("우와 비슷하다")."""
    if not target_attrs:
        return 0.0
    fmeta = c.get("feature_metadata")
    if not isinstance(fmeta, dict):
        return 0.0
    bonus = 0.0
    tfit = target_attrs.get("fit")
    if tfit and str(fmeta.get("fit") or "").strip().lower() in tfit:
        bonus += w.attr_fit
    # color 는 보통 RPC 하드게이트라 제외되지만, 재고 부족으로 relax 되면
    # target_attrs["color"] 로 넘어와 exact-color 를 상단에 유지한다.
    tcol = target_attrs.get("color")
    if tcol and str(fmeta.get("primary_color") or "").strip().upper() in tcol:
        bonus += w.attr_color
    tmat = target_attrs.get("material")
    if tmat:
        raw = fmeta.get("material")
        cand = {str(m).strip().lower() for m in raw} if isinstance(raw, list) else {str(raw).strip().lower()}
        if tmat & cand:
            bonus += w.attr_material
    tpat = target_attrs.get("pattern")
    if tpat and str(fmeta.get("pattern") or "").strip().lower() in tpat:
        bonus += w.attr_pattern
    tneck = target_attrs.get("neckline")
    if tneck and str(fmeta.get("neckline") or "").strip().lower() in tneck:
        bonus += w.attr_neckline
    # v2.6 축 — _attach_feature_metadata 가 product_features_v26.attr 에서 머지해 둔 값.
    tlen = target_attrs.get("length")
    if tlen and str(fmeta.get("length") or "").strip().lower() in tlen:
        bonus += w.attr_length
    tslv = target_attrs.get("sleeve_length")
    if tslv and str(fmeta.get("sleeve_length") or "").strip().lower() in tslv:
        bonus += w.attr_sleeve_length
    tleg = target_attrs.get("leg_shape")
    if tleg and str(fmeta.get("leg_shape") or "").strip().lower() in tleg:
        bonus += w.attr_leg_shape
    # v2.6 스타일 디테일 — surface(스칼라), texture/design_details(배열, material 과 동일 교집합).
    tsurf = target_attrs.get("surface")
    if tsurf and str(fmeta.get("surface") or "").strip().lower() in tsurf:
        bonus += w.attr_surface
    ttex = target_attrs.get("texture")
    if ttex:
        raw = fmeta.get("texture")
        cand = {str(m).strip().lower() for m in raw} if isinstance(raw, list) else {str(raw).strip().lower()}
        if ttex & cand:
            bonus += w.attr_texture
    tdd = target_attrs.get("design_details")
    if tdd:
        raw = fmeta.get("design_details")
        cand = {str(m).strip().lower() for m in raw} if isinstance(raw, list) else {str(raw).strip().lower()}
        if tdd & cand:
            bonus += w.attr_design_details
    # v2.6 비어패럴 스칼라축 — 공유 가중치(매칭 축마다 가산).
    if w.attr_nonapparel:
        for ax in NONAPPAREL_SCALAR_AXES:
            tv = target_attrs.get(ax)
            if tv and str(fmeta.get(ax) or "").strip().lower() in tv:
                bonus += w.attr_nonapparel
    # v2.6 wash/graphics(스칼라).
    twash = target_attrs.get("wash")
    if twash and str(fmeta.get("wash") or "").strip().lower() in twash:
        bonus += w.attr_wash
    tgfx = target_attrs.get("graphics")
    if tgfx and str(fmeta.get("graphics") or "").strip().lower() in tgfx:
        bonus += w.attr_graphics
    # v2.6 무드/스타일(final_tags 배열) — 쿼리 무드 ∩ 후보 무드 태그.
    # 보너스를 매칭 태그의 IDF 희귀도로 스케일(미니멀룩 등 흔한 태그≈0, 희귀 태그=풀).
    tmood = target_attrs.get("mood")
    if tmood:
        raw = fmeta.get("mood_tags")
        cand = {str(m).strip().lower() for m in raw} if isinstance(raw, list) else {str(raw).strip().lower()}
        matched = tmood & cand
        if matched:
            bonus += w.attr_mood * max(_mood_idf_weight(t) for t in matched)
    return bonus


def _score_candidate(
    c: dict[str, Any],
    profile: TasteProfile | None,
    w: RerankWeights,
    feature_scores: dict[tuple[str, str], float] | None = None,
    exclude_axes: frozenset[str] | None = None,
    target_attrs: dict[str, set[str]] | None = None,
) -> float:
    """Compute personalized score for one row. Higher = better.

    `feature_scores` (from ai.user_feature_scores, matched against the row's
    attached `feature_metadata`) contributes a centered term in [-w.feature,
    +w.feature] — 0 when the product has no enriched features or the user has no
    matching signal, so it never shifts the baseline for cold users.

    `exclude_axes` are the feature axes the query already pinned (adaptive α):
    they are dropped from the feature match so taste fills only the open axes.
    """
    distance = float(c.get("distance", 1.0))
    score = 1.0 - distance

    # 쿼리 의도 속성정렬 — 프로필/개인화 유무와 무관하게 항상 적용.
    if target_attrs:
        score += _attr_align_bonus(c, w, target_attrs)

    if feature_scores:
        pref = mean_feature_pref(c.get("feature_metadata"), feature_scores, exclude_axes)
        score += w.feature * (pref - 0.5) * 2.0

    if profile is None:
        return score

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
            score += w.keyword * sum(profile.liked_keywords.get(v, 0.0) for v in vibe_set)
            score -= w.keyword * sum(profile.disliked_keywords.get(v, 0.0) for v in vibe_set)
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
    feature_scores: dict[tuple[str, str], float] | None = None,
    exclude_axes: frozenset[str] | None = None,
    target_attrs: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return `candidates` re-ordered by score (desc).

    Three axes may apply: the brand/keyword/price/gender `TasteProfile`, the
    visual-feature `feature_scores` (personalization), and `target_attrs` — the
    query's own fit/material intent matched against each row's `feature_metadata`
    ("우와 비슷하다", applies to anonymous/cold users too). Any axis triggers the
    reorder.

    Stable sort: ties (e.g., score equal because no signal applied) keep
    their RPC order. Empty list / no axis with signal → return the
    input unchanged.

    The function never drops candidates — diversify handles cap and dedup
    after this step.
    """
    has_profile_signal = profile is not None and _has_any_signal(profile)
    has_attr = bool(target_attrs) and any(target_attrs.values())
    if not candidates or (not has_profile_signal and not feature_scores and not has_attr):
        return candidates
    profile = profile if has_profile_signal else None

    # Score once, then stable sort. Python's `sorted` is stable, so equal
    # scores fall back to original insertion order — preserves "RPC
    # distance ASC" as the deterministic tie-breaker.
    scored: list[tuple[float, dict[str, Any]]] = [
        (_score_candidate(c, profile, weights, feature_scores, exclude_axes, target_attrs), c) for c in candidates
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
