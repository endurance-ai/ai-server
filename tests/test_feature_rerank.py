"""Phase 5a — visual-feature taste in the search re-rank."""

from __future__ import annotations

from app.scoring import feature_scores_cache
from app.scoring.feature_taste import mean_feature_pref
from app.scoring.personalize_rerank import RerankWeights, rerank


def test_mean_feature_pref_neutral_and_signal():
    # No metadata or no scores → neutral 0.5 (never shifts the baseline).
    assert mean_feature_pref(None, {}) == 0.5
    assert mean_feature_pref({"primary_color": "black"}, {}) == 0.5
    # A single liked / disliked axis maps to the [0, 1] extremes.
    assert mean_feature_pref({"primary_color": "black"}, {("color", "black"): 20.0}) == 1.0
    assert mean_feature_pref({"primary_color": "black"}, {("color", "black"): -20.0}) == 0.0
    # Averages across the product's axes (black loved, cotton disliked → 0.5).
    mixed = mean_feature_pref(
        {"primary_color": "black", "material": ["cotton"]},
        {("color", "black"): 20.0, ("material", "cotton"): -20.0},
    )
    assert mixed == 0.5


def test_rerank_feature_only_reorders_without_profile():
    cands = [
        {"id": "1", "brand": "A", "distance": 0.10, "feature_metadata": {"primary_color": "beige"}},
        {"id": "2", "brand": "B", "distance": 0.12, "feature_metadata": {"primary_color": "black"}},
    ]
    out = rerank(
        cands,
        None,
        weights=RerankWeights(feature=0.15),
        feature_scores={("color", "black"): 20.0, ("color", "beige"): -20.0},
    )
    # #2 is slightly farther but its loved color outweighs the distance gap.
    assert [c["id"] for c in out] == ["2", "1"]


def test_rerank_no_signal_no_feature_is_noop():
    cands = [{"id": "1", "brand": "A", "distance": 0.1}, {"id": "2", "brand": "B", "distance": 0.2}]
    out = rerank(cands, None, weights=RerankWeights(), feature_scores=None)
    assert out is cands


def test_feature_scores_cache_roundtrip():
    feature_scores_cache.clear()
    assert feature_scores_cache.get("c:1") is None
    feature_scores_cache.put("c:1", {("color", "black"): 5.0})
    assert feature_scores_cache.get("c:1") == {("color", "black"): 5.0}
    feature_scores_cache.clear()
    assert feature_scores_cache.get("c:1") is None
