"""SPEC-AGENT-V2-REACT — search_products `#id` anchor (mirror of refine_search).

When mobile pins a card and presses a critique chip, the user message text
starts with `[#<id> · brand · name · ₩price]`. The ReAct agent USUALLY routes
that to `refine_search` (PRs #111, #112, #113), but for broader chip intents
("이런 거 더" with no obvious refinement signal) the LLM can pick
`search_products` instead. Before this change the search_products path was
the same prior-turn-leak liability: `ctx.vision_category` / `style_node`
from the previous Vision/text search would still gate the new search.

The fix mirrors refine_search: when `#<id>` is present in `ctx.text_query`
and there is no fresh image, fetch the pinned product's embedding +
category and:

1. Pass `override_embedding=...` to `run_text_only_search` → Modal text
   embed is skipped, search runs against the product's own vector.
2. Replace `category` with the pinned product's category (avoid prior-turn
   family-gate leak).
3. Clear `ctx.style_node_primary` fallback (prior turn's brand letter).
4. Skip `set_last_query` persistence so the mobile prefix string never
   pollutes a subsequent legacy refine.
5. Prefer the pinned anchor over `origin_url` blending (explicit user
   signal wins).

These tests pin each branch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agents.tools.search_products as sp

_DIM = 768
_ANCHOR_VEC = [0.42] * _DIM
_TEXT_VEC = [0.11] * _DIM


@pytest.fixture
def _mock_embed_text(monkeypatch):
    mock = AsyncMock(return_value=_TEXT_VEC)
    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", mock)
    return mock


@pytest.fixture
def _captured_search(monkeypatch):
    """Stub search_step + diversify; capture the embedding the search sees
    and the AnalyzedItem fields the family gate keys off."""
    captured: dict[str, object] = {}

    async def fake_search_step(state):
        captured["embedding"] = list(state.embedding or [])
        captured["category"] = state.request.item.category
        captured["style_node"] = state.request.style_node.primary if state.request.style_node is not None else None
        state.raw_candidates = [{"id": "p1", "name": "X", "brand": "Y", "distance": 0.2}]
        return state

    async def fake_diversify(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify)
    # Skip pre-message and gender ask paths.
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")
    return captured


@pytest.mark.asyncio
async def test_pinned_id_anchors_on_product_embedding(monkeypatch, _mock_embed_text, _captured_search):
    """#id present in ctx.text_query + embedding available →
    run_text_only_search receives override_embedding, Modal text embed is
    skipped, search_step sees the product's own vector. ctx.vision_category
    is overridden by the pinned product's category."""
    fetch_emb = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 1,
        "user_key": "u:1",
        "image_url": "",
        "text_query": "[#12345 · MM6 Maison Margiela · 트렌치 · ₩680,000] 이런 거 더",
        # Stale prior-turn leak.
        "vision_category": "knit",
        "style_node_primary": "K",
    }
    args = {"text_query": "similar items"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    fetch_emb.assert_awaited_once_with(12345)
    fetch_cat.assert_awaited_once_with(12345)
    _mock_embed_text.assert_not_awaited()
    assert _captured_search["embedding"] == _ANCHOR_VEC
    assert _captured_search["category"] == "Jeans"
    assert _captured_search["style_node"] is None


@pytest.mark.asyncio
async def test_pinned_id_with_missing_embedding_falls_back_to_text(monkeypatch, _mock_embed_text, _captured_search):
    """Embedding lookup returns None → fail-open: legacy text path runs,
    Modal text embed IS invoked, search uses that vector."""
    fetch_emb = AsyncMock(return_value=None)
    fetch_cat = AsyncMock(return_value=None)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 1,
        "user_key": "u:1",
        "image_url": "",
        "text_query": "[#99999 · Brand] 이런 거 더",
    }
    args = {"text_query": "similar items"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    fetch_emb.assert_awaited_once_with(99999)
    _mock_embed_text.assert_awaited()
    assert _captured_search["embedding"] == _TEXT_VEC


@pytest.mark.asyncio
async def test_no_pinned_id_uses_text_path_unchanged(monkeypatch, _mock_embed_text, _captured_search):
    """Plain search without #id → embedding lookup never called, text path
    embedder runs exactly as before."""
    fetch_emb = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {"chat_id": 1, "user_key": "u:1", "image_url": "", "text_query": "트렌치 코트"}
    args = {"text_query": "trench coat women"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    fetch_emb.assert_not_awaited()
    fetch_cat.assert_not_awaited()
    _mock_embed_text.assert_awaited()
    assert _captured_search["embedding"] == _TEXT_VEC


@pytest.mark.asyncio
async def test_pinned_id_with_fresh_image_skips_anchor(monkeypatch, _mock_embed_text, _captured_search):
    """Fresh image_url in ctx (Vision photo turn) → anchor MUST NOT fire.
    The image-search path has its own image anchor; we don't want to
    double-anchor or accidentally embed-fetch on every photo turn whose
    caption happens to contain `#1`."""
    fetch_emb = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)
    # Image path goes through run_image_search → mock it cheap.
    monkeypatch.setattr(sp, "run_image_search", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.agents.tools.search_products._is_real_image_url", lambda u: True)

    ctx = {
        "chat_id": 1,
        "user_key": "u:1",
        "image_url": "https://example.com/photo.jpg",
        "text_query": "look at this #12345",
    }
    args = {"text_query": "match this outfit"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    fetch_emb.assert_not_awaited()
    fetch_cat.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_id_does_not_persist_last_query(monkeypatch, _mock_embed_text, _captured_search):
    """Anchor turn must NOT call set_last_query — args.text_query on an
    anchor turn is the LLM's interpretation of the pinned-prefix chip and
    persisting it would pollute base_query on subsequent legacy refines."""
    fetch_emb = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    set_lq = MagicMock()
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)
    monkeypatch.setattr("app.agents.last_query.set_last_query", set_lq)

    ctx = {
        "chat_id": 1,
        "user_key": "u:1",
        "image_url": "",
        "text_query": "[#54321 · Pezze Vintage · 청바지 · ₩120,000] 이런 거 더",
    }
    args = {"text_query": "similar items women"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    set_lq.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_search_still_persists_last_query(monkeypatch, _mock_embed_text, _captured_search):
    """Non-anchor search must keep the existing last_query persistence so
    a NEXT-TURN refine_search reuses the translated/gender-pinned query."""
    fetch_emb = AsyncMock(return_value=None)
    fetch_cat = AsyncMock(return_value=None)
    set_lq = MagicMock()
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)
    monkeypatch.setattr("app.agents.last_query.set_last_query", set_lq)

    ctx = {"chat_id": 1, "user_key": "u:1", "image_url": "", "text_query": "트렌치 코트"}
    args = {"text_query": "trench coat"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    set_lq.assert_called()


@pytest.mark.asyncio
async def test_pinned_anchor_skips_origin_url_blending(monkeypatch, _mock_embed_text, _captured_search):
    """origin_url (cross-turn image blend) + #id both set → explicit user
    pin wins, blended search is skipped, text-only path runs with the
    anchor vector."""
    fetch_emb = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="Jeans")
    blended = AsyncMock(return_value=[])
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)
    monkeypatch.setattr(sp, "run_smart_blended_search", blended)
    monkeypatch.setattr("app.agents.origin_image.get_origin_url", lambda _cid: "https://example.com/origin.jpg")

    ctx = {
        "chat_id": 1,
        "user_key": "u:1",
        "image_url": "",
        "text_query": "[#54321 · Pezze Vintage · 청바지 · ₩120,000] 이런 거 더",
    }
    args = {"text_query": "similar items women"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    blended.assert_not_awaited()
    assert _captured_search["embedding"] == _ANCHOR_VEC
