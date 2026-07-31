"""Shared visual-feature taste scoring (Phase 5).

Maps a user's decayed ``ai.user_feature_scores`` against a product's
``product_features.feature_metadata``, reusing ``feature_pairs`` — the same
extractor that recorded the signals — so the scoring keys match the stored keys
exactly. Shared by curation (``curation_refresh.select_candidate_ids``) and
search re-rank (``personalize_rerank``).
"""

from __future__ import annotations

from typing import Any

from app.services.curation_taste import feature_pairs


def norm_taste(score: float) -> float:
    """Map a decayed taste score in [-20, 20] onto [0, 1]. 0 → 0.5 (neutral)."""
    return (max(-20.0, min(20.0, score)) + 20.0) / 40.0


def mean_feature_pref(metadata: Any, feature_scores: dict[tuple[str, str], float]) -> float:
    """Mean normalized preference over a product's (axis, value) feature pairs.

    Returns the neutral 0.5 when the product carries no enriched features or the
    user has no matching signal — so it neither helps nor hurts the ranking.
    """
    pairs = feature_pairs(metadata if isinstance(metadata, dict) else None)
    if not pairs or not feature_scores:
        return 0.5
    return sum(norm_taste(feature_scores.get(p, 0.0)) for p in pairs) / len(pairs)
