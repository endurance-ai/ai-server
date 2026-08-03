"""Characterization tests for the brand-anchored 2-tower rescore.

SPEC-BRAND-2TOWER-RESCORE. Captures the pure `rescore()` blend behaviour:

    new_distance = α · product_distance + (1 − α) · brand_distance

Fail-open contract (brand miss / bad α / bad embedding) keeps the original
distance and original RPC order. The two cache lookups are patched on the
rescore module (it imports them under `_brand_attr` / `_brand_emb` aliases at
import time, so patching the source module would not rebind the names).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.scoring import brand_2tower_rescore as r


@pytest.fixture
def patch_caches(monkeypatch):
    """Install brand-attr / brand-emb stubs from per-test dicts.

    `attrs_by_brand`: brand string -> brand_id (or None for "no attrs").
    `vec_by_id`: brand_id -> vector (or absent for "no embedding").
    """

    def _install(attrs_by_brand: dict, vec_by_id: dict):
        def _attr(brand):
            if brand in attrs_by_brand:
                bid = attrs_by_brand[brand]
                return None if bid is None else SimpleNamespace(brand_id=bid)
            return None

        def _emb(brand_id):
            return vec_by_id.get(brand_id)

        monkeypatch.setattr(r, "_brand_attr", _attr)
        monkeypatch.setattr(r, "_brand_emb", _emb)

    return _install


# ── guard clauses ───────────────────────────────────────────────────────────


def test_empty_candidates_returns_unchanged():
    out, stats = r.rescore([], [1.0, 0.0], alpha=0.5)
    assert out == []
    assert stats == {"hit": 0, "miss": 0, "kept": 0}


def test_query_embedding_none_keeps_all():
    cands = [{"brand": "A", "distance": 0.3}]
    out, stats = r.rescore(cands, None, alpha=0.5)
    assert out is cands
    assert stats["kept"] == 1
    assert stats["hit"] == 0


def test_query_embedding_empty_list_keeps_all():
    cands = [{"brand": "A", "distance": 0.3}, {"brand": "B", "distance": 0.4}]
    out, stats = r.rescore(cands, [], alpha=0.5)
    assert out is cands
    assert stats["kept"] == 2


@pytest.mark.parametrize("bad_alpha", [0.0, -0.1, 1.5, 2.0])
def test_alpha_out_of_range_keeps_original(bad_alpha):
    cands = [{"brand": "A", "distance": 0.3}]
    out, stats = r.rescore(cands, [1.0, 0.0], alpha=bad_alpha)
    # Out-of-range α → skip rescore (loud warn), original order/distance kept.
    assert out is cands
    assert stats == {"hit": 0, "miss": 0, "kept": 1}


# ── brand miss paths (fail-open) ────────────────────────────────────────────


def test_brand_attr_miss_keeps_original_distance(patch_caches):
    patch_caches(attrs_by_brand={}, vec_by_id={})  # no attrs at all
    cands = [{"brand": "Unknown", "distance": 0.42}]
    out, stats = r.rescore(cands, [1.0, 0.0], alpha=0.5)
    # hit==0 → returns the ORIGINAL list object, unchanged.
    assert out is cands
    assert out[0]["distance"] == 0.42
    assert stats == {"hit": 0, "miss": 1, "kept": 1}


def test_brand_vector_missing_keeps_original(patch_caches):
    # attrs resolve a brand_id, but no embedding for that id.
    patch_caches(attrs_by_brand={"A": 1}, vec_by_id={})
    cands = [{"brand": "A", "distance": 0.42}]
    out, stats = r.rescore(cands, [1.0, 0.0], alpha=0.5)
    assert out is cands
    assert stats == {"hit": 0, "miss": 1, "kept": 1}


def test_bad_brand_vector_dim_keeps_original(patch_caches):
    # brand vector length differs from query → _cosine_distance returns None.
    patch_caches(attrs_by_brand={"A": 1}, vec_by_id={1: [1.0, 0.0, 0.0]})
    cands = [{"brand": "A", "distance": 0.42}]
    out, stats = r.rescore(cands, [1.0, 0.0], alpha=0.5)
    assert out is cands
    assert stats == {"hit": 0, "miss": 1, "kept": 1}


def test_zero_norm_brand_vector_keeps_original(patch_caches):
    patch_caches(attrs_by_brand={"A": 1}, vec_by_id={1: [0.0, 0.0]})
    cands = [{"brand": "A", "distance": 0.42}]
    out, stats = r.rescore(cands, [1.0, 0.0], alpha=0.5)
    assert out is cands
    assert stats == {"hit": 0, "miss": 1, "kept": 1}


# ── successful blend + re-sort ──────────────────────────────────────────────


def test_blend_recomputes_distance_and_sorts_asc(patch_caches):
    # query points along x. BrandA vec aligns (bd=0); BrandB vec is orthogonal
    # (bd=1). With α=0.5:
    #   A: 0.5*0.5 + 0.5*0.0 = 0.25
    #   B: 0.5*0.1 + 0.5*1.0 = 0.55
    # Input order is [B(0.1), A(0.5)]; after rescore A sorts ahead of B.
    patch_caches(
        attrs_by_brand={"A": 1, "B": 2},
        vec_by_id={1: [1.0, 0.0], 2: [0.0, 1.0]},
    )
    cands = [
        {"brand": "B", "distance": 0.1, "id": "b"},
        {"brand": "A", "distance": 0.5, "id": "a"},
    ]
    out, stats = r.rescore(cands, [1.0, 0.0], alpha=0.5)
    assert [c["id"] for c in out] == ["a", "b"]
    assert out[0]["distance"] == pytest.approx(0.25)
    assert out[1]["distance"] == pytest.approx(0.55)
    assert stats == {"hit": 2, "miss": 0, "kept": 0}


def test_blend_does_not_mutate_caller_rows(patch_caches):
    patch_caches(attrs_by_brand={"A": 1}, vec_by_id={1: [1.0, 0.0]})
    original = {"brand": "A", "distance": 0.5}
    cands = [original]
    out, _ = r.rescore(cands, [1.0, 0.0], alpha=0.5)
    # caller's dict is untouched; the output carries a fresh copy.
    assert original["distance"] == 0.5
    assert out[0] is not original
    assert out[0]["distance"] == pytest.approx(0.25)


def test_partial_hit_sorts_when_at_least_one_hit(patch_caches):
    # A hits (bd=0 → new 0.25), B misses (kept original 0.9).
    patch_caches(attrs_by_brand={"A": 1, "B": None}, vec_by_id={1: [1.0, 0.0]})
    cands = [
        {"brand": "B", "distance": 0.9, "id": "b"},
        {"brand": "A", "distance": 0.5, "id": "a"},
    ]
    out, stats = r.rescore(cands, [1.0, 0.0], alpha=0.5)
    assert stats == {"hit": 1, "miss": 1, "kept": 1}
    # hit>0 → list is sorted ASC; A(0.25) before B(0.9).
    assert [c["id"] for c in out] == ["a", "b"]


# ── _cosine_distance unit coverage ──────────────────────────────────────────


def test_cosine_distance_identical_vectors_is_zero():
    assert r._cosine_distance([1.0, 2.0], [1.0, 2.0]) == pytest.approx(0.0)


def test_cosine_distance_orthogonal_is_one():
    assert r._cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "a,b",
    [
        ([], [1.0]),
        ([1.0], []),
        ([1.0, 2.0], [1.0]),  # length mismatch
        ([0.0, 0.0], [1.0, 1.0]),  # zero norm a
        ([1.0, 1.0], [0.0, 0.0]),  # zero norm b
    ],
)
def test_cosine_distance_unusable_returns_none(a, b):
    assert r._cosine_distance(a, b) is None
