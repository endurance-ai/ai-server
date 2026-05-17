"""SPEC-AGENT-V2-REACT — search_products / refine_search text-vs-image routing.

Regression guard for the "always 0 results" root bug: the tool used to
HARD-REQUIRE an image_url, so the LLM fabricated placeholder URLs that Modal
embedded → vector search missed the whole 78k catalog. The fix:

- LLM can no longer supply image_url (removed from SearchProductsArgs).
- Text-only path routes to search_step + diversify_step with a zero dense
  vector (no Modal call, sentinel URL never reaches Modal).
- Photo path uses the real ctx image via run_pipeline.
- No text + no image → clean ``no_query`` error.

Pipeline internals are monkeypatched so these run offline in CI.
"""

from __future__ import annotations

import pytest

import app.agents.tools.search_products as sp
from app.agents.tool_registry import SearchProductsArgs, validate_args

# ── LLM schema no longer exposes image_url ─────────────────────────────────


def test_search_products_args_has_no_image_url_field():
    """The LLM must not be able to supply (and thus fabricate) an image_url."""
    assert "image_url" not in SearchProductsArgs.__annotations__


def test_validate_args_rejects_llm_supplied_image_url():
    ok, err = validate_args("search_products", {"text_query": "loafers", "image_url": "https://evil/x.jpg"})
    assert ok is False
    assert err is not None and "image_url" in err


# ── _is_real_image_url predicate ───────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://i.pinimg.com/originals/a/b/c.jpg", True),
        ("http://example.com/x.png", True),
        ("", False),
        (None, False),
        (sp._TEXT_ONLY_SENTINEL, False),
        ("not-a-url", False),
        ("ftp://host/x.jpg", False),
    ],
)
def test_is_real_image_url(value, expected):
    assert sp._is_real_image_url(value) is expected


# ── Text-only path: no image → search_step/diversify, NOT run_pipeline ──────


@pytest.mark.asyncio
async def test_text_only_uses_search_step_not_run_pipeline(monkeypatch):
    calls: dict[str, object] = {}

    async def fake_search_step(state):
        calls["embedding_len"] = len(state.embedding or [])
        calls["search_query"] = state.request.item.search_query
        calls["image_url"] = state.request.image_url
        state.raw_candidates = [{"id": "p1", "name": "Leather Loafer", "brand": "Acme"}]
        return state

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    async def boom_run_pipeline(req):  # must NOT be called on text-only path
        raise AssertionError("run_pipeline called on text-only path")

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)
    monkeypatch.setattr("app.pipeline.runner.run_pipeline", boom_run_pipeline)

    ctx = {"chat_id": 1, "image_url": "", "text_query": ""}
    res = await sp.dispatch({"text_query": "leather loafers"}, ctx)

    assert res["ok"] is True
    assert res["candidates_count"] == 1
    assert res["top_candidates"][0]["title"] == "Leather Loafer"  # name → title fallback
    # Zero dense vector injected; sentinel set; never the LLM/placeholder.
    assert calls["embedding_len"] == sp._EMBED_DIM
    assert calls["search_query"] == "leather loafers"
    assert calls["image_url"] == sp._TEXT_ONLY_SENTINEL


@pytest.mark.asyncio
async def test_sentinel_never_sent_to_modal(monkeypatch):
    """embed_step is bypassed — the sentinel URL must never hit Modal."""
    embed_calls: list[str] = []

    async def spy_embed(image_url: str):
        embed_calls.append(image_url)
        return [0.1] * sp._EMBED_DIM

    async def fake_search_step(state):
        state.raw_candidates = [{"id": "p1", "name": "X", "brand": "B"}]
        return state

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.providers.embedding.EmbedProvider.embed_image_url", staticmethod(spy_embed))
    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

    res = await sp.dispatch({"text_query": "denim jeans"}, {"chat_id": 1, "image_url": ""})
    assert res["ok"] is True
    assert embed_calls == []  # Modal embed never invoked on text-only path


# ── Photo-pick path: real ctx image → run_pipeline with the REAL url ───────


@pytest.mark.asyncio
async def test_photo_pick_uses_real_ctx_image(monkeypatch):
    seen: dict[str, object] = {}

    class FakeResp:
        results = [type("C", (), {"id": "p9", "name": "Jacket", "brand": "Z", "price": 100})()]

    async def fake_run_pipeline(req):
        seen["image_url"] = req.image_url
        seen["search_query"] = req.item.search_query
        return FakeResp()

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", fake_run_pipeline)

    real = "https://i.pinimg.com/originals/d4/57/e8/abc.jpg"
    ctx = {"chat_id": 1, "image_url": real, "text_query": "casual"}
    res = await sp.dispatch({"text_query": "casual jacket"}, ctx)

    assert res["ok"] is True
    assert res["candidates_count"] == 1
    assert seen["image_url"] == real  # the REAL resolved url, not a placeholder
    assert seen["search_query"] == "casual jacket"


# ── No query + no image → clean error (agent will ask the user) ────────────


@pytest.mark.asyncio
async def test_no_query_no_image_returns_clean_error():
    res = await sp.dispatch({}, {"chat_id": 1, "image_url": "", "text_query": ""})
    assert res["ok"] is False
    assert res["error"] == "no_query"
    assert res["candidates_count"] == 0


@pytest.mark.asyncio
async def test_text_query_alone_is_sufficient(monkeypatch):
    async def fake_search_step(state):
        state.raw_candidates = [{"id": "p1", "name": "Coat", "brand": "B"}]
        return state

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

    # No image anywhere, only a text_query → must still search.
    res = await sp.dispatch({"text_query": "trench coat"}, {"chat_id": 1, "image_url": ""})
    assert res["ok"] is True
    assert res["candidates_count"] == 1


# ── refine_search: was image-mandatory (sent "" to Modal) — now text-only ──


# ── STOPGAP: zero-dense noise suppression on the zero-sentinel text path ───
#
# Live-log root cause: a constant zero-dense item (dense_rank set, no sparse
# match) ranked #1 for EVERY unrelated text query. RRF gives the zero vector's
# deterministic pgvector ranking a real fusion score, so it pollutes the top
# instead of sinking. Stopgap: drop pure-dense rows before diversify on the
# zero-sentinel path ONLY. The image path (run_pipeline) is unaffected.

# Mixed RPC rows for a "black shorts" style query. Only p1 is the genuine
# pgroonga hit (sparse_rank set); p2 is dense+sparse (both → keep); p3/p4 are
# pure zero-dense pollution (dense_rank set, sparse_rank None → drop).
_BLACK_SHORTS_ROWS = [
    {
        "id": "e640f7d4",
        "name": "Auralee Herringbone Shirt",
        "brand": "Auralee",
        "score": 0.0164,
        "dense_rank": 1,
        "sparse_rank": None,
    },  # zero-noise #1 in prod
    {
        "id": "p1",
        "name": "Black Cotton Shorts",
        "brand": "Uniqlo",
        "score": 0.0150,
        "dense_rank": None,
        "sparse_rank": 1,
    },  # real sparse hit
    {
        "id": "p2",
        "name": "Black Cargo Shorts",
        "brand": "Stussy",
        "score": 0.0140,
        "dense_rank": 7,
        "sparse_rank": 3,
    },  # both → keep
    {
        "id": "p3",
        "name": "Beige Linen Coat",
        "brand": "Lemaire",
        "score": 0.0130,
        "dense_rank": 2,
        "sparse_rank": None,
    },  # zero-noise
]


@pytest.mark.asyncio
async def test_zero_sentinel_text_path_drops_dense_only_keeps_sparse(monkeypatch):
    """(a) Zero-sentinel text path drops dense_rank!=None & sparse_rank==None
    rows and keeps sparse-only + both."""
    seen: dict[str, object] = {}

    async def fake_search_step(state):
        seen["embedding_all_zero"] = not any(state.embedding or [1])
        state.raw_candidates = [dict(r) for r in _BLACK_SHORTS_ROWS]
        return state

    async def fake_diversify_step(state):
        # diversify reads raw_candidates; we surface what survived the filter.
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

    res = await sp.dispatch({"text_query": "black shorts"}, {"chat_id": 1, "image_url": ""})

    assert res["ok"] is True
    assert seen["embedding_all_zero"] is True  # zero sentinel confirmed
    # 4 rows in → 2 zero-noise dropped (Auralee shirt, Lemaire coat) → 2 kept.
    assert res["candidates_count"] == 2
    kept_ids = {c["product_id"] for c in res["top_candidates"]}
    assert kept_ids == {"p1", "p2"}  # sparse-only + both survive
    assert "e640f7d4" not in kept_ids  # the prod #1 zero-noise is gone


def test_is_zero_dense_noise_predicate():
    assert sp._is_zero_dense_noise({"dense_rank": 1, "sparse_rank": None}) is True
    assert sp._is_zero_dense_noise({"dense_rank": None, "sparse_rank": 2}) is False  # sparse-only
    assert sp._is_zero_dense_noise({"dense_rank": 3, "sparse_rank": 4}) is False  # both
    assert sp._is_zero_dense_noise({"dense_rank": None, "sparse_rank": None}) is False
    assert sp._is_zero_dense_noise("not-a-dict") is False


@pytest.mark.asyncio
async def test_image_path_real_embedding_is_unaffected_by_suppression(monkeypatch):
    """(b) A real (non-zero) embedding path is UNAFFECTED — run_pipeline owns
    ranking; the dense-only suppression must NOT trigger there."""
    captured: dict[str, object] = {}

    class FakeResp:
        # Same shape that would be zero-noise on the text path: dense-only.
        # On the IMAGE path dense is meaningful → these MUST all be kept.
        results = [
            type("C", (), {"id": "img1", "name": "Wool Coat", "brand": "A", "price": 200})(),
            type("C", (), {"id": "img2", "name": "Wool Scarf", "brand": "A", "price": 50})(),
        ]

    async def fake_run_pipeline(req):
        captured["image_url"] = req.image_url
        return FakeResp()

    async def boom_search_step(state):  # text path must NOT be entered
        raise AssertionError("search_step (text path) called on image path")

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", fake_run_pipeline)
    monkeypatch.setattr("app.pipeline.search.search_step", boom_search_step)

    real = "https://i.pinimg.com/originals/aa/bb/cc.jpg"
    res = await sp.dispatch({"text_query": "winter outfit"}, {"chat_id": 1, "image_url": real})

    assert res["ok"] is True
    assert captured["image_url"] == real
    # BOTH results survive — suppression never runs on the image path.
    assert res["candidates_count"] == 2


@pytest.mark.asyncio
async def test_refine_search_text_only_routes_to_sparse(monkeypatch):
    import app.agents.tools.refine_search as rf

    async def fake_search_step(state):
        state.raw_candidates = [{"id": "p1", "name": "Jeans", "brand": "B"}]
        return state

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    async def boom_run_pipeline(req):
        raise AssertionError("refine_search must not call run_pipeline on text-only path")

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)
    monkeypatch.setattr("app.pipeline.runner.run_pipeline", boom_run_pipeline)

    ctx = {"chat_id": 1, "image_url": "", "text_query": "denim"}
    res = await rf.dispatch({"action": "broaden", "boost_keywords": ["jeans"]}, ctx)
    assert res["ok"] is True
    assert res["candidates_count"] == 1
