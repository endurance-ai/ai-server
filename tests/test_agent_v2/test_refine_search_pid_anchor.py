"""SPEC-AGENT-V2-REACT — refine_search `#id` anchor (text-only path).

Mobile pins a card and presses a critique chip → home.tsx builds a server
message of the form ``[#12345 · brand · name · ₩price] 더 비슷하게``. Before
this change `refine_search` ignored the `#id` and re-embedded the previous
session's `last_query`, which bled the prior search topic into the result
set. The fix: when the user message text contains ``#<id>``, fetch that
product's image embedding from ``public.product_embeddings`` and feed it
to ``run_text_only_search`` via the new ``override_embedding`` arg —
Modal's text embed is skipped entirely and the search anchors on the pinned
product instead.

These tests pin the wiring:

1. Pinned `#id` present + embedding available → ``embed_text`` NOT called,
   ``search_step`` sees the product's vector.
2. Pinned `#id` present + embedding lookup returns None → fail-open: the
   classic text path runs (``embed_text`` IS called).
3. No `#id` → unchanged behaviour, embedding source is ``embed_text``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agents.tools.refine_search as rs

_DIM = 768
_ANCHOR_VEC = [0.42] * _DIM  # distinct so we can assert provenance
_TEXT_VEC = [0.11] * _DIM


@pytest.fixture
def _mock_embed_text(monkeypatch):
    """The text-fallback embedder. Returns a sentinel vector so we can tell
    apart whether the search ran against the product anchor or the text
    fallback."""
    mock = AsyncMock(return_value=_TEXT_VEC)
    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", mock)
    return mock


@pytest.fixture
def _captured_search(monkeypatch):
    """Mock search_step + diversify_step; capture the embedding the search
    actually saw so each test can assert provenance."""
    captured: dict[str, object] = {}

    async def fake_search_step(state):
        captured["embedding"] = list(state.embedding or [])
        # Capture the resolved AnalyzedItem fields the family gate keys off,
        # so tests can assert that pinned-anchor overrides the prior turn's
        # ctx.vision_category instead of leaking it into the search.
        captured["category"] = state.request.item.category
        captured["fit"] = state.request.item.fit
        captured["color_family"] = state.request.item.color_family
        captured["style_node"] = state.request.style_node.primary if state.request.style_node is not None else None
        state.raw_candidates = [{"id": "p1", "name": "X", "brand": "Y"}]
        return state

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)
    return captured


@pytest.mark.asyncio
async def test_pinned_id_anchors_on_product_embedding(monkeypatch, _mock_embed_text, _captured_search):
    """`#<id>` in the message text + embedding present →
    run_text_only_search receives `override_embedding`, Modal text embed
    is skipped, search_step sees the product's own vector."""
    fetch = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 1,
        "image_url": "",
        # Mobile prefix shape produced by kikoai-mobile home.tsx.
        "text_query": "[#12345 · MM6 Maison Margiela · 트렌치 · ₩680,000] 더 비슷하게",
    }
    res = await rs.dispatch({"action": "broaden"}, ctx)

    assert res["ok"] is True
    fetch.assert_awaited_once_with(12345)
    _mock_embed_text.assert_not_awaited()
    assert _captured_search["embedding"] == _ANCHOR_VEC


@pytest.mark.asyncio
async def test_pinned_id_with_missing_embedding_falls_back_to_text(monkeypatch, _mock_embed_text, _captured_search):
    """If the product has no embedding row (NULL or missing), the lookup
    returns None and refine_search falls open to the existing text-only
    path — Modal text embed is invoked and search runs against that
    vector. The previous behaviour is preserved on the failure edge."""
    fetch = AsyncMock(return_value=None)
    fetch_cat = AsyncMock(return_value=None)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 1,
        "image_url": "",
        "text_query": "[#99999 · Brand] 더 저렴하게",
    }
    res = await rs.dispatch({"action": "narrow"}, ctx)

    assert res["ok"] is True
    fetch.assert_awaited_once_with(99999)
    _mock_embed_text.assert_awaited()  # text fallback path
    assert _captured_search["embedding"] == _TEXT_VEC


@pytest.mark.asyncio
async def test_no_pinned_id_uses_text_path_unchanged(monkeypatch, _mock_embed_text, _captured_search):
    """A plain refine without any `#id` (legacy / agent-only invocation)
    must not call the embedding lookup at all and must use the text
    embedder exactly as before."""
    fetch = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {"chat_id": 1, "image_url": "", "text_query": "더 비슷하게"}
    res = await rs.dispatch({"action": "broaden"}, ctx)

    assert res["ok"] is True
    fetch.assert_not_awaited()
    fetch_cat.assert_not_awaited()
    _mock_embed_text.assert_awaited()
    assert _captured_search["embedding"] == _TEXT_VEC


@pytest.mark.asyncio
async def test_free_text_hash_digit_does_not_trigger_anchor(monkeypatch, _mock_embed_text, _captured_search):
    """Free-text like "그 #1 같은 거" MUST NOT trigger a spurious anchor
    (product_id=1 lookup). The mobile prefix always starts with `[#<id>`;
    the regex is anchored to the message start with the literal bracket."""
    fetch = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {"chat_id": 1, "image_url": "", "text_query": "그 #1 같은 거 더 저렴하게"}
    res = await rs.dispatch({"action": "narrow"}, ctx)

    assert res["ok"] is True
    fetch.assert_not_awaited()
    fetch_cat.assert_not_awaited()
    _mock_embed_text.assert_awaited()  # legacy text path


@pytest.mark.asyncio
async def test_pinned_id_overrides_prior_turn_category(monkeypatch, _mock_embed_text, _captured_search):
    """Live regression (2026-07-01): user searched knits in the prior turn
    (ctx.vision_category="knit", ctx.style_node_primary="K"), then pinned a
    jeans card and pressed "더 비슷하게". Before this fix the v6 family gate
    used the stale knit category and returned knits even though the
    embedding was anchored on the jeans. The fix: pinned-anchor also
    overrides category (from products.category) and clears the prior turn's
    style_node_primary letter."""
    fetch = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 1,
        "image_url": "",
        "text_query": "[#54321 · Pezze Vintage · 청바지 · ₩120,000] 더 비슷하게",
        # Stale ctx from the prior knit search — must NOT leak through.
        "vision_category": "knit",
        "style_node_primary": "K",
    }
    res = await rs.dispatch({"action": "broaden"}, ctx)

    assert res["ok"] is True
    fetch.assert_awaited_once_with(54321)
    fetch_cat.assert_awaited_once_with(54321)
    assert _captured_search["embedding"] == _ANCHOR_VEC
    # The pinned product's category, not the prior turn's "knit".
    assert _captured_search["category"] == "Jeans"
    # Prior turn's brand letter is cleared on anchor.
    assert _captured_search["style_node"] is None


@pytest.mark.asyncio
async def test_pinned_id_clears_prior_fit_and_color(monkeypatch, _mock_embed_text, _captured_search):
    """Prior turn was "white knit" so ctx.color_family="white" and ctx.fit
    came along. User then pins a BLUE jeans card and presses 더 비슷하게.
    Before this fix the anchored search was still narrowed to "white" and
    the prior fit. Pinned-anchor must clear both unless the LLM explicitly
    supplied them for this refine."""
    fetch = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 1,
        "image_url": "",
        "text_query": "[#54321 · Pezze Vintage · 청바지 · ₩120,000] 더 비슷하게",
        "vision_category": "knit",
        "color_family": "white",
        "fit": "oversized",
    }
    res = await rs.dispatch({"action": "broaden"}, ctx)

    assert res["ok"] is True
    assert _captured_search["color_family"] is None
    assert _captured_search["fit"] is None


@pytest.mark.asyncio
async def test_pinned_id_respects_explicit_color_override(monkeypatch, _mock_embed_text, _captured_search):
    """When the LLM supplies an explicit colour for this refine
    (e.g. "다른 색상으로 보여줘 → blue"), it must win over the cleared
    default. fit follows the same arg-wins-on-anchor rule."""
    fetch = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 1,
        "image_url": "",
        "text_query": "[#54321 · Pezze Vintage · 청바지 · ₩120,000] 다른 색",
        "vision_category": "knit",
        "color_family": "white",
        "fit": "oversized",
    }
    res = await rs.dispatch({"action": "broaden", "color": "blue", "fit": "slim"}, ctx)

    assert res["ok"] is True
    assert _captured_search["color_family"] == "blue"
    assert _captured_search["fit"] == "slim"


@pytest.mark.asyncio
async def test_pinned_id_does_not_persist_last_query(monkeypatch, _mock_embed_text, _captured_search):
    """Anchor turn must NOT call set_last_query — the text_query is the
    mobile prefix ("[#id · brand · ...] chip"), persisting it would
    pollute base_query on subsequent legacy (non-#id) refines."""
    fetch = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    set_lq = MagicMock()
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)
    monkeypatch.setattr("app.agents.last_query.set_last_query", set_lq)

    ctx = {
        "chat_id": 1,
        "image_url": "",
        "text_query": "[#54321 · Pezze Vintage · 청바지 · ₩120,000] 더 비슷하게",
    }
    res = await rs.dispatch({"action": "broaden"}, ctx)

    assert res["ok"] is True
    set_lq.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_refine_still_persists_last_query(monkeypatch, _mock_embed_text, _captured_search):
    """Non-anchor refines must keep the existing last_query persistence
    so chained refines reuse the seed query (B15 dedup contract)."""
    fetch = AsyncMock(return_value=None)
    fetch_cat = AsyncMock(return_value=None)
    set_lq = MagicMock()
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)
    monkeypatch.setattr("app.agents.last_query.set_last_query", set_lq)

    ctx = {"chat_id": 1, "image_url": "", "text_query": "더 저렴하게"}
    res = await rs.dispatch({"action": "narrow", "boost_keywords": ["budget"]}, ctx)

    assert res["ok"] is True
    set_lq.assert_called()


@pytest.mark.asyncio
async def test_exclude_brands_drops_matching_candidates(monkeypatch, _mock_embed_text):
    """Regression (2026-08-30) — refine_search `exclude_brands` was a TOTAL no-op:
    the list was never applied anywhere, and the brand-inheritance guard compared
    action to the string 'exclude_brands' which is not a real enum value
    (enum: broaden/refine/exclude/cheaper/color_swap), so '자라 빼고' on a refine
    turn silently kept Zara. Fix: drop candidates whose brand matches the excluded
    brand (case-insensitive exact OR substring), parity with search_products (D2)."""

    async def fake_search_step(state):
        state.raw_candidates = [
            {"id": "keep1", "name": "Black Jacket", "brand": "COS"},
            {"id": "drop1", "name": "Black Jacket", "brand": "Zara"},
            {"id": "keep2", "name": "Wool Coat", "brand": "Acne Studios"},
            {"id": "drop2", "name": "Denim Jacket", "brand": "ZARA Home"},
        ]
        return state

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

    ctx = {"chat_id": 1, "image_url": "", "text_query": "자라 빼고"}
    res = await rs.dispatch({"action": "exclude", "exclude_brands": ["Zara"]}, ctx)

    assert res["ok"] is True
    ids = {c.get("product_id") for c in res["top_candidates"]}
    assert "keep1" in ids
    assert "keep2" in ids
    assert "drop1" not in ids  # brand 'Zara' exact (case-insensitive)
    assert "drop2" not in ids  # 'ZARA Home' — 'zara' substring match
