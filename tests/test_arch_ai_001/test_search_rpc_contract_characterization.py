"""Net (3) -- SupabaseProvider.rpc("search_products_v5", ...) param mapping.

Locks the FULL params dict that app.pipeline.search.search_step passes to the
RPC, byte-for-byte, including the `:.7f` pgvector string. This is the anchor
for SPEC-ARCH-AI-001 REQ-AI-002 (the hardcoded RPC name + param mapping moves
into SearchRepository; this net proves the move is byte-identical).
"""

from __future__ import annotations

from app.models.request import AnalyzedItem, PriceFilter, RecommendRequest
from app.pipeline.runner import run_pipeline

from .conftest import FIXED_EMBED_DIM, FIXED_EMBED_VALUE

# Expected pgvector string: 768 copies of "0.1234567" (f"{0.1234567:.7f}")
EXPECTED_QUERY_EMBEDDING = "[" + ",".join([f"{FIXED_EMBED_VALUE:.7f}"] * FIXED_EMBED_DIM) + "]"


def _req(
    *,
    search_query: str = "beige knit sweater",
    search_query_ko: str | None = "베이지 니트 스웨터",
    brand_filter: list[str] | None = None,
    price_filter: PriceFilter | None = None,
) -> RecommendRequest:
    item = AnalyzedItem(
        id="item-1",
        category="top",
        subcategory="knit",
        searchQuery=search_query,
        searchQueryKo=search_query_ko,
    )
    return RecommendRequest(
        item=item,
        imageUrl="https://example.com/x.jpg",
        brandFilter=brand_filter,
        priceFilter=price_filter,
    )


async def _capture_params(req: RecommendRequest, fixed_embed, rpc_capture) -> tuple[str, dict]:
    await run_pipeline(req)
    assert len(rpc_capture.calls) == 1
    return rpc_capture.calls[0]


# ── (a) raw query path (enhance disabled) — search_query_ko wins ───────────
async def test_characterize_rpc_raw_query_full_params(fixed_embed, rpc_capture):
    fn_name, params = await _capture_params(_req(), fixed_embed, rpc_capture)
    assert fn_name == "search_products_v5"
    assert params == {
        "query_embedding": EXPECTED_QUERY_EMBEDDING,
        "query_text": "베이지 니트 스웨터",
        "brand_filter": None,
        "gender_filter": None,
        "subcategory_filter": None,
        "price_min": None,
        "price_max": None,
        "tags_filter": None,
        "k": 50,
        "rrf_k": 60,
    }


# ── (b) price_filter set — price_min / price_max mapping ───────────────────
async def test_characterize_rpc_price_filter(fixed_embed, rpc_capture):
    pf = PriceFilter(minPrice=10000, maxPrice=50000)
    fn_name, params = await _capture_params(_req(price_filter=pf), fixed_embed, rpc_capture)
    assert fn_name == "search_products_v5"
    assert params["price_min"] == 10000
    assert params["price_max"] == 50000
    assert params["query_text"] == "베이지 니트 스웨터"
    assert params["brand_filter"] is None
    assert params["k"] == 50 and params["rrf_k"] == 60


# ── (c) brand_filter set — passed through verbatim ─────────────────────────
async def test_characterize_rpc_brand_filter(fixed_embed, rpc_capture):
    fn_name, params = await _capture_params(_req(brand_filter=["uniqlo", "cos"]), fixed_embed, rpc_capture)
    assert params["brand_filter"] == ["uniqlo", "cos"]
    assert params["gender_filter"] is None
    assert params["subcategory_filter"] is None
    assert params["tags_filter"] is None


# ── (d) query_text 3-tier fallback: ko present vs only en ─────────────────
async def test_characterize_rpc_query_text_ko_present(fixed_embed, rpc_capture):
    _, params = await _capture_params(
        _req(search_query="english only", search_query_ko="한국어 우선"),
        fixed_embed,
        rpc_capture,
    )
    # search_query_ko wins over search_query when present.
    assert params["query_text"] == "한국어 우선"


async def test_characterize_rpc_query_text_en_only(fixed_embed, rpc_capture):
    _, params = await _capture_params(
        _req(search_query="english only", search_query_ko=None),
        fixed_embed,
        rpc_capture,
    )
    # search_query_ko is None -> falls back to search_query.
    assert params["query_text"] == "english only"


async def test_characterize_rpc_query_embedding_exact_format(fixed_embed, rpc_capture):
    """Lock the :.7f pgvector serialization explicitly (arithmetic drift net)."""
    _, params = await _capture_params(_req(), fixed_embed, rpc_capture)
    qe = params["query_embedding"]
    assert qe.startswith("[0.1234567,0.1234567,")
    assert qe.endswith(",0.1234567]")
    assert qe.count("0.1234567") == FIXED_EMBED_DIM
    assert qe.count(",") == FIXED_EMBED_DIM - 1
