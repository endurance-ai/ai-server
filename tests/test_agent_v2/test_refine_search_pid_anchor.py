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

from unittest.mock import AsyncMock

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
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)

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
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)

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
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch)

    ctx = {"chat_id": 1, "image_url": "", "text_query": "더 비슷하게"}
    res = await rs.dispatch({"action": "broaden"}, ctx)

    assert res["ok"] is True
    fetch.assert_not_awaited()
    _mock_embed_text.assert_awaited()
    assert _captured_search["embedding"] == _TEXT_VEC
