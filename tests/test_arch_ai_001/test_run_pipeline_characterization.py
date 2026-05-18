"""Net (1) -- run_pipeline end-to-end response snapshot
(v6-migrated by SPEC-SEARCH-V6-001).

Full path: run_pipeline -> embed (fixed vector) -> enhance_query (disabled)
-> v6 search RPC (fixed hand-authored rows) -> diversify -> RecommendResponse.

v6 rows carry `distance` (cosine, ASC=better) + `degraded`; the runner maps
score = 1.0 - distance and sets dense_rank/sparse_rank = None. The rows below
set distance = 1.0 - score so the locked `score` golden values are preserved
verbatim (the v5 score-equivalence subject is retired with SPEC basis; the
SAME end-to-end snapshot guard is preserved against v6). diversify is
order-preserving and never reads score/distance, so the brand/platform cap
survivor logic is byte-identical — only the rank fields change (-> None).

Locks resp.item_id, resp.results (ordered (id,brand,score,dense_rank,
sparse_rank) tuples), and the full resp.counts dict. resp.latency_ms VALUES
are wall-clock (non-deterministic) -> only its key SET is asserted.

Also locks PIPELINE_PARALLEL_ENABLED True vs False equivalence.
"""

from __future__ import annotations

import pytest

from app.models.request import AnalyzedItem, RecommendRequest
from app.pipeline.runner import run_pipeline


# 14 rows: 4 brands (uniqlo/cos/zara/musinsa) + blank, 3 platforms
# (shop/market/web). One row (r7) omits `distance` -> runner score
# = 1.0 - default(1.0) = 0.0. One row (r9) omits `brand` -> "".
# v6 shape: distance (= 1.0 - target_score) + degraded; no score/ranks.
def _rows() -> list[dict]:
    def mk(i, brand, platform, score, dense, sparse, *, no_score=False, no_brand=False):
        d: dict = {
            "id": f"p{i}",
            "name": f"Item {i}",
            "price": 10000 + i * 1000,
            "image_url": f"https://img/{i}.jpg",
            "product_url": f"https://shop/{i}",
            "platform": platform,
            "subcategory": "knit",
            "degraded": False,
        }
        if not no_brand:
            d["brand"] = brand
        if not no_score:
            # distance = 1.0 - score so runner's score = 1.0 - distance
            # reproduces the locked golden score verbatim.
            d["distance"] = 1.0 - score
        return d

    return [
        mk(0, "uniqlo", "shop", 0.95, 1, 1),
        mk(1, "uniqlo", "shop", 0.94, 2, 2),
        mk(2, "uniqlo", "shop", 0.93, 3, 3),  # 3rd uniqlo -> brand cap 2 drops
        mk(3, "cos", "shop", 0.92, 4, None),
        mk(4, "cos", "market", 0.91, None, 4),
        mk(5, "zara", "market", 0.90, 5, 5),
        mk(6, "zara", "web", 0.89, 6, 6),
        mk(7, "musinsa", "web", 0.0, 7, 7, no_score=True),  # score omitted
        mk(8, "musinsa", "web", 0.87, 8, 8),
        mk(9, "", "shop", 0.86, 9, 9, no_brand=True),  # brand key omitted
        mk(10, "cos", "market", 0.85, 10, 10),
        mk(11, "zara", "web", 0.84, 11, 11),
        mk(12, "uniqlo", "market", 0.83, 12, 12),
        mk(13, "musinsa", "shop", 0.82, 13, 13),
    ]


def _req(
    *,
    tolerance: float = 0.5,
    final_limit: int | None = None,
    brand_filter: list[str] | None = None,
) -> RecommendRequest:
    item = AnalyzedItem(
        id="item-42",
        category="top",
        subcategory="knit",
        searchQuery="beige knit sweater",
        searchQueryKo="베이지 니트 스웨터",
    )
    return RecommendRequest(
        item=item,
        imageUrl="https://example.com/x.jpg",
        brandFilter=brand_filter,
        tolerance=tolerance,
        finalLimit=final_limit,
    )


def _tuples(resp):
    return [(c.id, c.brand, c.score, c.dense_rank, c.sparse_rank) for c in resp.results]


# Expected diversify trace (brand_cap=2, platform_cap=3, target by tolerance):
#  p0 uniqlo/shop  keep (u=1, shop=1)
#  p1 uniqlo/shop  keep (u=2, shop=2)
#  p2 uniqlo/shop  DROP brand cap (uniqlo>=2)
#  p3 cos/shop     keep (cos=1, shop=3)
#  p4 cos/market   keep (cos=2, market=1)
#  p5 zara/market  keep (zara=1, market=2)
#  p6 zara/web     keep (zara=2, web=1)
#  p7 musinsa/web  keep (musinsa=1, web=2)  score->0.0
#  p8 musinsa/web  keep (musinsa=2, web=3)
#  p9 ""/shop      DROP platform cap (shop>=3)
#  p10 cos/market  DROP brand cap (cos>=2)
#  p11 zara/web    DROP brand cap (zara>=2) AND web>=3
#  p12 uniqlo/mkt  DROP brand cap (uniqlo>=2)
#  p13 musinsa/shp DROP brand cap (musinsa>=2)
#  -> survivors: p0,p1,p3,p4,p5,p6,p7,p8  (8 rows; target 15 not reached)
# v6: dense_rank/sparse_rank are always None (runner sets them None); score
# is preserved (distance = 1.0 - score round-trips exactly).
_EXPECTED_BASE = [
    ("p0", "uniqlo", 0.95, None, None),
    ("p1", "uniqlo", 0.94, None, None),
    ("p3", "cos", 0.92, None, None),
    ("p4", "cos", 0.91, None, None),
    ("p5", "zara", 0.90, None, None),
    ("p6", "zara", 0.89, None, None),
    ("p7", "musinsa", 0.0, None, None),
    ("p8", "musinsa", 0.87, None, None),
]
_EXPECTED_COUNTS_BASE = {"raw": 14, "after_diversify": 8, "final": 8}


@pytest.mark.parametrize("tolerance", [0.0, 0.5, 1.0])
async def test_characterize_run_pipeline_tolerance(fixed_embed, patch_rpc, tolerance):
    """Caps bite before any tolerance target (8 < 10) -> output invariant."""
    patch_rpc(_rows())
    resp = await run_pipeline(_req(tolerance=tolerance))
    assert resp.item_id == "item-42"
    assert _tuples(resp) == _EXPECTED_BASE
    assert resp.counts == _EXPECTED_COUNTS_BASE
    # latency_ms: keys present, values non-deterministic -> assert key set only.
    assert set(resp.latency_ms.keys()) == {"embed", "enhance_query", "search", "diversify"}


@pytest.mark.parametrize("final_limit", [None, 5])
async def test_characterize_run_pipeline_final_limit(fixed_embed, patch_rpc, final_limit):
    patch_rpc(_rows())
    resp = await run_pipeline(_req(tolerance=1.0, final_limit=final_limit))
    if final_limit is None:
        assert _tuples(resp) == _EXPECTED_BASE
        assert resp.counts == _EXPECTED_COUNTS_BASE
    else:
        # final_limit=5 truncates diversify before caps exhaust the list.
        assert _tuples(resp) == _EXPECTED_BASE[:5]
        assert resp.counts == {"raw": 14, "after_diversify": 5, "final": 5}


@pytest.mark.parametrize("brand_filter", [None, ["uniqlo"]])
async def test_characterize_run_pipeline_brand_filter(fixed_embed, patch_rpc, brand_filter):
    patch_rpc(_rows())
    resp = await run_pipeline(_req(tolerance=0.5, brand_filter=brand_filter))
    if brand_filter is None:
        assert _tuples(resp) == _EXPECTED_BASE
        assert resp.counts == _EXPECTED_COUNTS_BASE
    else:
        # brand_filter active -> brand_cap 2*3=6, so uniqlo is NOT brand-capped
        # (p0,p1,p2 all survive). Platform cap 3 then drives all drops:
        #   shop:   p0,p1,p2 fill it -> p3,p9,p13 dropped (shop>=3)
        #   market: p4,p5,p10 fill it -> p12 dropped (market>=3)
        #   web:    p6,p7,p8 fill it -> p11 dropped (web>=3)
        # Observed-and-locked ACTUAL survivor order (p10 NOT p12 -- the market
        # cap fills at p10 before p12 is reached; this ordering asymmetry is
        # exactly the arithmetic the IMPROVE extraction must keep byte-identical).
        tuples = _tuples(resp)
        ids = [t[0] for t in tuples]
        assert ids == ["p0", "p1", "p2", "p4", "p5", "p6", "p7", "p8", "p10"]
        assert tuples == [
            ("p0", "uniqlo", 0.95, None, None),
            ("p1", "uniqlo", 0.94, None, None),
            ("p2", "uniqlo", 0.93, None, None),
            ("p4", "cos", 0.91, None, None),
            ("p5", "zara", 0.90, None, None),
            ("p6", "zara", 0.89, None, None),
            ("p7", "musinsa", 0.0, None, None),
            ("p8", "musinsa", 0.87, None, None),
            ("p10", "cos", 0.85, None, None),
        ]
        assert resp.counts == {"raw": 14, "after_diversify": 9, "final": 9}


async def test_characterize_run_pipeline_parallel_equivalence(fixed_embed, patch_rpc, monkeypatch):
    """PIPELINE_PARALLEL_ENABLED True vs False -> byte-identical response."""
    from app.core.config import settings as app_settings

    patch_rpc(_rows())
    monkeypatch.setattr(app_settings, "PIPELINE_PARALLEL_ENABLED", True)
    resp_par = await run_pipeline(_req(tolerance=0.5))

    patch_rpc(_rows())
    monkeypatch.setattr(app_settings, "PIPELINE_PARALLEL_ENABLED", False)
    resp_seq = await run_pipeline(_req(tolerance=0.5))

    assert resp_par.item_id == resp_seq.item_id
    assert _tuples(resp_par) == _tuples(resp_seq)
    assert resp_par.counts == resp_seq.counts
    # And both equal the locked golden.
    assert _tuples(resp_par) == _EXPECTED_BASE
    assert resp_par.counts == _EXPECTED_COUNTS_BASE
