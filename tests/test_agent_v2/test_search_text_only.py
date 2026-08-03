"""SPEC-AGENT-V2-REACT — search_products / refine_search text-vs-image routing
(v6-migrated by SPEC-SEARCH-V6-001).

Regression guard for the "always 0 results" root bug: the tool used to
HARD-REQUIRE an image_url, so the LLM fabricated placeholder URLs that Modal
embedded → vector search missed the whole catalog. The fix:

- LLM can no longer supply image_url (removed from SearchProductsArgs).
- Text-only path routes to search_step + diversify_step with a REAL text
  embedding from EmbedProvider.embed_text (v6 embedding-first; the old
  zero-dense + pgroonga sparse trick is gone — pgroonga/v5 were dropped).
  embed_step (image path) is still bypassed; the sentinel URL never reaches
  Modal.
- Photo path uses the real ctx image via run_pipeline.
- No text + no image → clean ``no_query`` error.

Pipeline internals are monkeypatched so these run offline in CI. embed_text
is monkeypatched the same way embed_image_url is (seam parity).
"""

from __future__ import annotations

import pytest

import app.agents.tools.search_products as sp
from app.agents.tool_registry import SearchProductsArgs, validate_args

# Fixed text embedding dim for the v6 text-only path (mirrors embed_image_url
# mock parity; the real Modal /embed/text returns 768).
_TEXT_EMBED_DIM = 768


@pytest.fixture(autouse=True)
def _mock_embed_text(monkeypatch):
    """v6 text-only path calls EmbedProvider.embed_text before search_step.
    Patch it (via the app.pipeline.embed re-export seam, same as
    embed_image_url) to a fixed non-zero 768-vec so no Modal call happens.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.pipeline.embed.EmbedProvider.embed_text",
        AsyncMock(return_value=[0.1] * _TEXT_EMBED_DIM),
    )


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
    # Real text embedding injected (v6); sentinel set; never LLM/placeholder.
    assert calls["embedding_len"] == _TEXT_EMBED_DIM
    # 260522 gender pin: a genderless text_query gets 'unisex' appended
    # deterministically (search_products._ensure_gender_token) so it never
    # silently skews against the women-leaning catalog.
    assert calls["search_query"] == "leather loafers unisex"
    assert calls["image_url"] == sp._TEXT_ONLY_SENTINEL


@pytest.mark.asyncio
async def test_sentinel_never_sent_to_modal(monkeypatch):
    """embed_step (image path) is bypassed — the sentinel URL must never hit
    Modal's image embed. The v6 text path uses embed_text (text only), so
    embed_image_url is never invoked."""
    embed_calls: list[str] = []

    async def spy_embed(image_url: str):
        embed_calls.append(image_url)
        return [0.1] * _TEXT_EMBED_DIM

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
    assert embed_calls == []  # Modal IMAGE embed never invoked on text-only path


# ── Photo-pick path: real ctx image → run_pipeline with the REAL url ───────


@pytest.mark.asyncio
async def test_photo_pick_uses_real_ctx_image(monkeypatch):
    seen: dict[str, object] = {}

    class FakeResp:
        results = [type("C", (), {"id": "p9", "name": "Jacket", "brand": "Z", "price": 100})()]

    # `**_` swallows the new keyword `user_key=` that search_products now
    # forwards to run_pipeline (SPEC-PERSONALIZE-RERANK); the test doesn't
    # care about its value, only that the dispatch path still resolves.
    async def fake_run_pipeline(req, **_):
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
    # 260522 gender pin — genderless query → 'unisex' appended deterministically.
    assert seen["search_query"] == "casual jacket unisex"


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


# ── v6: text path uses a REAL embedding — NO zero-dense suppression ────────
#
# SPEC-SEARCH-V6-001 retired the zero-dense stopgap (_is_zero_dense_noise /
# _suppress_zero_dense_noise / the zero-vector injection). Its sole
# precondition was a zero query vector (the old v5+pgroonga text trick). v6 is
# embedding-first: the text path sends a REAL embed_text() vector, so v6 rows
# (distance ASC, degraded) flow through unfiltered — the RPC ordering already
# places relevant rows on top. These tests pin the NEW v6 behavior (same
# safety intent: text-path rows are not dropped / not zero-vector-polluted).

# v6 RPC rows for a "black shorts" style query — distance ASC (min=best),
# degraded flag, no score/dense_rank/sparse_rank. All flow through unfiltered.
_BLACK_SHORTS_ROWS = [
    {"id": 1, "name": "Black Cotton Shorts", "brand": "Uniqlo", "distance": 0.10, "degraded": False},
    {"id": 2, "name": "Black Cargo Shorts", "brand": "Stussy", "distance": 0.14, "degraded": False},
    {"id": 3, "name": "Black Linen Shorts", "brand": "Lemaire", "distance": 0.19, "degraded": True},
]


@pytest.mark.asyncio
async def test_v6_text_path_uses_real_embedding_no_suppression(monkeypatch):
    """v6 text path injects a REAL (non-zero) embed_text() vector and does NOT
    drop any rows — every v6 row flows through to diversify in RPC order."""
    seen: dict[str, object] = {}

    async def fake_search_step(state):
        # Real embedding (the autouse _mock_embed_text returns [0.1]*768).
        seen["embedding_all_zero"] = not any(state.embedding or [1])
        seen["embedding_len"] = len(state.embedding or [])
        state.raw_candidates = [dict(r) for r in _BLACK_SHORTS_ROWS]
        return state

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

    res = await sp.dispatch({"text_query": "black shorts"}, {"chat_id": 1, "image_url": ""})

    assert res["ok"] is True
    assert seen["embedding_all_zero"] is False  # REAL embedding, not zero
    assert seen["embedding_len"] == _TEXT_EMBED_DIM
    # All 3 v6 rows survive (no suppression) in RPC distance-ASC order.
    assert res["candidates_count"] == 3
    kept_ids = [c["product_id"] for c in res["top_candidates"]]
    assert kept_ids == [1, 2, 3]


def test_zero_dense_stopgap_removed():
    """The zero-dense stopgap helpers are deleted under v6 (their precondition
    — a zero query vector — no longer exists; v6 is embedding-first)."""
    assert not hasattr(sp, "_is_zero_dense_noise")
    assert not hasattr(sp, "_suppress_zero_dense_noise")
    assert not hasattr(sp, "_EMBED_DIM")


@pytest.mark.asyncio
async def test_image_path_unaffected_by_v6_text_path(monkeypatch):
    """The image path still routes to run_pipeline (v6 image embedding) and is
    entirely separate from the text path — search_step is never entered."""
    captured: dict[str, object] = {}

    class FakeResp:
        results = [
            type("C", (), {"id": "img1", "name": "Wool Coat", "brand": "A", "price": 200})(),
            type("C", (), {"id": "img2", "name": "Wool Scarf", "brand": "A", "price": 50})(),
        ]

    async def fake_run_pipeline(req, **_):
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
