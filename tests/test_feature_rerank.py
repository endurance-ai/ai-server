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


def test_attr_align_fit_lifts_matching_candidate_without_profile():
    # "우와 비슷하다" — 쿼리 target fit 과 맞는 후보를, 임베딩 거리가 더 먼데도 위로.
    cands = [
        {"id": "1", "brand": "A", "distance": 0.10, "feature_metadata": {"fit": "slim"}},
        {"id": "2", "brand": "B", "distance": 0.16, "feature_metadata": {"fit": "oversized"}},
    ]
    out = rerank(
        cands,
        None,
        weights=RerankWeights(attr_fit=0.20),
        target_attrs={"fit": {"oversized"}},
    )
    assert [c["id"] for c in out] == ["2", "1"]


def test_attr_align_alone_triggers_reorder_for_anonymous():
    # target_attrs 만으로 (profile/feature_scores 없이) reorder 가 켜져야 한다.
    cands = [
        {"id": "1", "brand": "A", "distance": 0.12, "feature_metadata": {"fit": "regular"}},
        {"id": "2", "brand": "B", "distance": 0.14, "feature_metadata": {"fit": "relaxed"}},
    ]
    out = rerank(cands, None, weights=RerankWeights(attr_fit=0.20), target_attrs={"fit": {"relaxed"}})
    assert [c["id"] for c in out] == ["2", "1"]


def test_attr_align_no_target_is_noop():
    cands = [{"id": "1", "brand": "A", "distance": 0.1, "feature_metadata": {"fit": "slim"}}]
    out = rerank(cands, None, weights=RerankWeights(attr_fit=0.20), target_attrs={})
    assert out is cands


def test_attr_align_pattern_lifts_matching_candidate():
    # 쿼리 target pattern(striped) 과 맞는 후보를, 거리가 더 먼데도 위로.
    cands = [
        {"id": "1", "brand": "A", "distance": 0.08, "feature_metadata": {"pattern": "solid"}},
        {"id": "2", "brand": "B", "distance": 0.18, "feature_metadata": {"pattern": "striped"}},
    ]
    out = rerank(cands, None, weights=RerankWeights(attr_pattern=0.15), target_attrs={"pattern": {"striped"}})
    assert [c["id"] for c in out] == ["2", "1"]


def test_attr_align_color_keeps_exact_color_on_top_when_relaxed():
    # 재고 부족으로 색 게이트 relax 됐을 때: 요청 색(BLACK) 이 거리가 더 먼데도 위로.
    cands = [
        {"id": "1", "brand": "A", "distance": 0.05, "feature_metadata": {"primary_color": "BLUE"}},
        {"id": "2", "brand": "B", "distance": 0.15, "feature_metadata": {"primary_color": "BLACK"}},
    ]
    out = rerank(cands, None, weights=RerankWeights(attr_color=0.25), target_attrs={"color": {"BLACK"}})
    assert [c["id"] for c in out] == ["2", "1"]


def test_mean_feature_pref_excludes_pinned_axes():
    # Phase 6-② adaptive α — "blanks = taste": a query-pinned axis is dropped
    # from the match so taste only speaks for the axes the user left open.
    meta = {"primary_color": "black", "fit": "slim"}
    scores = {("color", "black"): 20.0, ("fit", "slim"): -20.0}
    # Both axes → loved color + disliked fit average to neutral.
    assert mean_feature_pref(meta, scores) == 0.5
    # Query pinned color → only the (disliked) fit remains.
    assert mean_feature_pref(meta, scores, {"color"}) == 0.0
    # Query pinned fit → only the (loved) color remains.
    assert mean_feature_pref(meta, scores, {"fit"}) == 1.0
    # Every axis pinned → nothing left for taste → neutral (taste stays silent).
    assert mean_feature_pref(meta, scores, {"color", "fit"}) == 0.5


def test_rerank_excludes_query_pinned_axis():
    # Same setup as the feature-only reorder, but the user explicitly searched a
    # colour — the pinned color axis must not let taste flip the RPC order.
    cands = [
        {"id": "1", "brand": "A", "distance": 0.10, "feature_metadata": {"primary_color": "beige"}},
        {"id": "2", "brand": "B", "distance": 0.12, "feature_metadata": {"primary_color": "black"}},
    ]
    scores = {("color", "black"): 20.0, ("color", "beige"): -20.0}
    # Without masking the loved black wins (#2 first) — see the 5a test above.
    unmasked = rerank(cands, None, weights=RerankWeights(feature=0.15), feature_scores=scores)
    assert [c["id"] for c in unmasked] == ["2", "1"]
    # With color pinned, taste is silent on color → distance order preserved.
    masked = rerank(
        cands, None, weights=RerankWeights(feature=0.15), feature_scores=scores, exclude_axes=frozenset({"color"})
    )
    assert [c["id"] for c in masked] == ["1", "2"]


def test_query_pinned_axes_maps_slots_to_feature_axes():
    from types import SimpleNamespace

    from app.services.search_service import _query_pinned_axes

    # color_family / fit / fabric → the three query-expressible feature axes.
    item = SimpleNamespace(color_family="BLACK", fit="oversized", fabric="cotton")
    assert _query_pinned_axes(item) == frozenset({"color", "fit", "material"})
    # Blank / whitespace slots are not pinned — taste fills them.
    assert _query_pinned_axes(SimpleNamespace(color_family="BLACK", fit=None, fabric="  ")) == frozenset({"color"})
    assert _query_pinned_axes(SimpleNamespace(color_family=None, fit=None, fabric=None)) == frozenset()


def test_feature_scores_cache_roundtrip():
    feature_scores_cache.clear()
    assert feature_scores_cache.get("c:1") is None
    feature_scores_cache.put("c:1", {("color", "black"): 5.0})
    assert feature_scores_cache.get("c:1") == {("color", "black"): 5.0}
    feature_scores_cache.clear()
    assert feature_scores_cache.get("c:1") is None
