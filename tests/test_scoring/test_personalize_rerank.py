"""Characterization tests for personalization rerank.

SPEC-PERSONALIZE-RERANK. Captures the pure `rerank()` reordering driven by a
user's `TasteProfile` × `brand_nodes.attributes`. The brand-node cache lookup
is patched on the rerank module (imported as `_lookup_brand` alias at import
time). `normalize_brand` is the real pure function.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.memory.taste_profile import TasteProfile
from app.scoring import personalize_rerank as pr
from app.scoring.personalize_rerank import RerankWeights, rerank

_W = RerankWeights()


@pytest.fixture
def patch_lookup(monkeypatch):
    """Install a brand-attrs stub from a {brand_lower: attrs} dict."""

    def _install(by_brand_lower: dict):
        def _lookup(brand):
            return by_brand_lower.get((brand or "").strip().lower())

        monkeypatch.setattr(pr, "_lookup_brand", _lookup)

    return _install


def _attrs(*, vibe=(), gender_lean=""):
    return SimpleNamespace(vibe=tuple(vibe), gender_lean=gender_lean)


def _profile(**kw) -> TasteProfile:
    return TasteProfile(user_key="u:test", **kw)


# ── guard clauses (fail-open) ───────────────────────────────────────────────


def test_empty_candidates_returns_unchanged():
    assert rerank([], _profile(liked_brands={"x": 1.0}), weights=_W) == []


def test_none_profile_returns_unchanged():
    cands = [{"brand": "A", "distance": 0.1}]
    assert rerank(cands, None, weights=_W) is cands


def test_no_signal_profile_returns_unchanged(patch_lookup):
    patch_lookup({})
    cands = [{"brand": "A", "distance": 0.1}, {"brand": "B", "distance": 0.2}]
    # Brand-new profile: every signal dict empty, no gender → skip entirely.
    assert rerank(cands, _profile(), weights=_W) is cands


# ── brand like / dislike ────────────────────────────────────────────────────


def test_liked_brand_moves_up(patch_lookup):
    patch_lookup({})  # no brand attrs needed
    cands = [
        {"brand": "Acme", "distance": 0.5, "id": "a"},
        {"brand": "Zenith", "distance": 0.5, "id": "z"},
    ]
    profile = _profile(liked_brands={"zenith": 1.0})
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["z", "a"]


def test_disliked_brand_moves_down(patch_lookup):
    patch_lookup({})
    cands = [
        {"brand": "Acme", "distance": 0.5, "id": "a"},
        {"brand": "Zenith", "distance": 0.5, "id": "z"},
    ]
    profile = _profile(disliked_brands={"acme": 1.0})
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["z", "a"]


# ── gender mismatch penalty ─────────────────────────────────────────────────


def test_gender_mismatch_penalizes_conflicting_brand(patch_lookup):
    # men profile + womens-leaning brand → penalty; neutral brand wins.
    patch_lookup(
        {
            "wbrand": _attrs(gender_lean="womens"),
            "nbrand": _attrs(gender_lean="unisex"),
        }
    )
    cands = [
        {"brand": "WBrand", "distance": 0.5, "id": "w"},
        {"brand": "NBrand", "distance": 0.5, "id": "n"},
    ]
    profile = _profile(gender="men")
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["n", "w"]


def test_gender_only_signal_still_reranks(patch_lookup):
    # gender alone counts as a signal (no early return).
    patch_lookup({"wbrand": _attrs(gender_lean="womens")})
    cands = [{"brand": "WBrand", "distance": 0.5, "id": "w"}]
    # Should not raise / drop the candidate.
    out = rerank(cands, _profile(gender="men"), weights=_W)
    assert [c["id"] for c in out] == ["w"]


# ── keyword vibe contribution ───────────────────────────────────────────────


def test_liked_keyword_vibe_boosts_brand(patch_lookup):
    patch_lookup(
        {
            "boho": _attrs(vibe=("bohemian",)),
            "plain": _attrs(vibe=("minimal",)),
        }
    )
    cands = [
        {"brand": "Plain", "distance": 0.5, "id": "p"},
        {"brand": "Boho", "distance": 0.5, "id": "b"},
    ]
    # Strong keyword weight relative to the default keyword weight (0.02).
    profile = _profile(liked_keywords={"bohemian": 5.0})
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["b", "p"]


def test_disliked_keyword_vibe_demotes_brand(patch_lookup):
    patch_lookup(
        {
            "boho": _attrs(vibe=("bohemian",)),
            "plain": _attrs(vibe=("minimal",)),
        }
    )
    cands = [
        {"brand": "Boho", "distance": 0.5, "id": "b"},
        {"brand": "Plain", "distance": 0.5, "id": "p"},
    ]
    profile = _profile(disliked_keywords={"bohemian": 5.0})
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["p", "b"]


# ── brand cache miss is fail-open (not dropped) ─────────────────────────────


def test_brand_cache_miss_keeps_candidate(patch_lookup):
    patch_lookup({})  # lookup always None
    cands = [
        {"brand": "Known", "distance": 0.4, "id": "k"},
        {"brand": "Unknown", "distance": 0.6, "id": "u"},
    ]
    # A brand-level like still applies via the profile dict even with no attrs.
    profile = _profile(liked_brands={"known": 1.0})
    out = rerank(cands, profile, weights=_W)
    # Both candidates survive; none dropped.
    assert {c["id"] for c in out} == {"k", "u"}
    assert [c["id"] for c in out] == ["k", "u"]


# ── price fit ───────────────────────────────────────────────────────────────


def test_price_in_range_gets_bonus(patch_lookup):
    patch_lookup({})
    cands = [
        {"brand": "X", "distance": 0.5, "price": 50, "id": "cheap"},
        {"brand": "Y", "distance": 0.5, "price": 150, "id": "fit"},
    ]
    profile = _profile(price_min_observed=100, price_max_observed=200)
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["fit", "cheap"]


def test_price_out_of_range_no_bonus_keeps_order(patch_lookup):
    patch_lookup({})
    cands = [
        {"brand": "X", "distance": 0.5, "price": 50, "id": "a"},
        {"brand": "Y", "distance": 0.5, "price": 60, "id": "b"},
    ]
    profile = _profile(price_min_observed=100, price_max_observed=200)
    out = rerank(cands, profile, weights=_W)
    # Neither in range → equal scores → stable original order.
    assert [c["id"] for c in out] == ["a", "b"]


def test_non_numeric_price_contributes_zero(patch_lookup):
    patch_lookup({})
    cands = [{"brand": "X", "distance": 0.5, "price": "n/a", "id": "a"}]
    profile = _profile(price_min_observed=100, price_max_observed=200)
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["a"]


# ── stable sort on equal scores ─────────────────────────────────────────────


def test_equal_scores_preserve_rpc_order(patch_lookup):
    patch_lookup({})
    cands = [
        {"brand": "First", "distance": 0.5, "id": "1"},
        {"brand": "Second", "distance": 0.5, "id": "2"},
        {"brand": "Third", "distance": 0.5, "id": "3"},
    ]
    # Signal present (so we don't early-return) but irrelevant to these brands.
    profile = _profile(liked_brands={"other": 1.0})
    out = rerank(cands, profile, weights=_W)
    assert [c["id"] for c in out] == ["1", "2", "3"]


# ── helper unit coverage ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "price,p_min,p_max,expected",
    [
        (150, 100, 200, _W.price_fit),  # in range
        (50, 100, 200, 0.0),  # below min
        (250, 100, 200, 0.0),  # above max
        (150, None, None, 0.0),  # no bounds
        (150, 100, None, _W.price_fit),  # one-sided, satisfied
        (50, 100, None, 0.0),  # one-sided, below
        ("oops", 100, 200, 0.0),  # non-numeric
    ],
)
def test_price_fit_contribution_table(price, p_min, p_max, expected):
    assert pr._price_fit_contribution(price, p_min, p_max, _W.price_fit) == expected


def test_price_fit_zero_weight_short_circuits():
    assert pr._price_fit_contribution(150, 100, 200, 0.0) == 0.0


@pytest.mark.parametrize(
    "pg,bg,expected",
    [
        ("men", "womens", _W.gender_mismatch),
        ("women", "mens", _W.gender_mismatch),
        ("men", "mens", 0.0),  # agreement, no penalty
        ("men", "unisex", 0.0),
        ("", "womens", 0.0),  # no profile gender
        ("men", "", 0.0),  # no brand lean
    ],
)
def test_gender_penalty_table(pg, bg, expected):
    assert pr._gender_penalty(pg, bg, _W.gender_mismatch) == expected
