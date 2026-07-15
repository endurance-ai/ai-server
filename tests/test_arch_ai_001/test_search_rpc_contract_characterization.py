"""Net (3) -- DatabaseProvider.rpc("search_products_v6", ...) param mapping
(re-pointed by SPEC-SEARCH-V6-001).

Locks the FULL params dict app.pipeline.search.search_step passes to the v6
RPC, byte-for-byte, including the `:.7f` pgvector string. This is the anchor
for REQ-AI-002 (the hardcoded RPC name + param mapping lives in
SearchRepository).

The v5 byte-identity subject is legitimately retired (v5 + pgroonga +
product_search_text dropped — SPEC-SEARCH-V6-001). The SAME safety intent — a
locked param snapshot guarding against silent param drift — is preserved
against the NEW v6 6-key shape (mirrors how kikoai/app retired its own v5
byte-identity net while keeping the equivalent v6 guard).

v6 is embedding-first: query_embedding is the sole ranking signal, plus
NULL style-node, the CANONICAL FAMILY `p_category` (SPEC-SEARCH-V6-001
re-point), NULL subcategory, an optional brand-name list, and the limit.
There is no query_text / price / gender / subcategory / tags / rrf_k param
(those were dropped with v5).

SPEC-SEARCH-V6-001 family-gate re-point: `p_category` is no longer the v5/v6
literal `None`. The v6 FILTER 2 canonical family gate now receives
`to_canonical_family(item.category)`. The `_req()` item is `category="top"`,
which the single-source Vision-alias map normalizes to the canonical token
`"tops"`. The snapshot is re-pointed from `None` → `"tops"` accordingly (the
SAME locked-snapshot safety intent, now anchored to the v6 family-gate
value). `p_subcategory` (2026-07-15 활성화) stays `None` for THIS request:
`item.subcategory="knit"` is not a canonical vocab token — the fail-open
path (모르는 값 전달 = EXACT·무완화 필터라 0결과). Recognized values are
asserted in tests/test_search_precision_filters.py.
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


# ── (a) v6 6-key param snapshot — embedding-first, no text/price/gender ─────
async def test_characterize_rpc_v6_full_params(fixed_embed, rpc_capture):
    fn_name, params = await _capture_params(_req(), fixed_embed, rpc_capture)
    assert fn_name == "search_products_v6"
    assert params == {
        "query_embedding": EXPECTED_QUERY_EMBEDDING,
        "p_style_node_id": None,
        # SPEC-SEARCH-V6-001 re-point: item.category="top" → canonical "tops"
        # (was None pre-family-gate). v6 FILTER 2 family gate engages.
        "p_category": "tops",
        "p_subcategory": None,
        "p_brand_names": None,
        # SPEC-SEARCH-V6-COLOR: color filter param — None here (fixture has
        # no color_family). Backward-compatible with pre-color rollout.
        "p_color_family": None,
        "p_limit": 50,
    }


# ── (b) price_filter set — DROPPED under v6 (no price param at all) ─────────
async def test_characterize_rpc_v6_ignores_price_filter(fixed_embed, rpc_capture):
    pf = PriceFilter(minPrice=10000, maxPrice=50000)
    fn_name, params = await _capture_params(_req(price_filter=pf), fixed_embed, rpc_capture)
    assert fn_name == "search_products_v6"
    # v6 has NO price param — price_filter must not leak into the RPC dict.
    assert "price_min" not in params
    assert "price_max" not in params
    assert set(params.keys()) == {
        "query_embedding",
        "p_style_node_id",
        "p_category",
        "p_subcategory",
        "p_brand_names",
        # SPEC-SEARCH-V6-COLOR added — 7-key param dict (was 6).
        "p_color_family",
        "p_limit",
    }


# ── (c) brand_filter set — mapped verbatim to p_brand_names ────────────────
async def test_characterize_rpc_v6_brand_filter(fixed_embed, rpc_capture):
    fn_name, params = await _capture_params(_req(brand_filter=["uniqlo", "cos"]), fixed_embed, rpc_capture)
    assert params["p_brand_names"] == ["uniqlo", "cos"]
    assert params["p_style_node_id"] is None
    # SPEC-SEARCH-V6-001 re-point: item.category="top" → canonical "tops".
    assert params["p_category"] == "tops"
    assert params["p_subcategory"] is None


# ── (d) query_text DROPPED — neither ko nor en query reaches the RPC ───────
async def test_characterize_rpc_v6_no_query_text_param(fixed_embed, rpc_capture):
    _, params = await _capture_params(
        _req(search_query="english only", search_query_ko="한국어 우선"),
        fixed_embed,
        rpc_capture,
    )
    # v6 is embedding-first: no text param regardless of the request query.
    assert "query_text" not in params


async def test_characterize_rpc_v6_query_embedding_exact_format(fixed_embed, rpc_capture):
    """Lock the :.7f pgvector serialization explicitly (arithmetic drift net)."""
    _, params = await _capture_params(_req(), fixed_embed, rpc_capture)
    qe = params["query_embedding"]
    assert qe.startswith("[0.1234567,0.1234567,")
    assert qe.endswith(",0.1234567]")
    assert qe.count("0.1234567") == FIXED_EMBED_DIM
    assert qe.count(",") == FIXED_EMBED_DIM - 1
