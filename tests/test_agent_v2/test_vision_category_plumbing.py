"""SPEC-SEARCH-V6-001 — bot-path Vision-category → canonical p_category plumb.

Regression guard for the family-gate plumbing bug: search_products used to
read `ctx["style_node_primary"]` (a brand STYLE-NODE letter A–Z) as the
search `category`, so the bot path ALWAYS normalized to `other` and the v6
FILTER 2 family gate never engaged for the primary (conversational) path.

The fix: react_loop._build_ctx now exposes the REAL Vision garment category
as `ctx["vision_category"]` (from state.vision_selected_item /
detected_items[selected_item_index]["category"]), and search_products /
refine_search read THAT as the search category.

This test drives the full bot seam — WorkingState → _build_ctx →
search_products.dispatch → SearchRepository.build_params — and asserts:
  - Vision item category "Outer" plumbs to p_category "outerwear"
    (NOT the style-node letter, NOT "other"); and
  - the style-node letter is NOT what reaches p_category.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import app.agents.tools.search_products as sp
from app.agents.react_loop import _build_ctx
from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.repositories.search_repository import SearchRepository


class _Sess:
    """Minimal session stub — react_loop helpers only read .lang / attrs."""

    lang = "en"
    detected_items: list = []
    vision_item = None


@pytest.mark.asyncio
async def test_vision_category_outer_plumbs_to_canonical_outerwear(monkeypatch):
    captured: dict[str, object] = {}

    real_build_params = SearchRepository.build_params

    def spy_build_params(**kwargs):
        params = real_build_params(**kwargs)
        captured["category_arg"] = kwargs.get("category")
        captured["p_category"] = params["p_category"]
        return params

    monkeypatch.setattr(SearchRepository, "build_params", staticmethod(spy_build_params))

    # Pipeline edges stubbed so the search runs offline; build_params still
    # executes for real (that is the assertion subject).
    # Outfit-contamination fix (2026-06-07): when selected_item_index is set,
    # _build_ctx clears image_url and seeds text_query from the item's
    # searchQuery so the text-only path is taken. Both embed_image_url and
    # embed_text are stubbed to cover either path.
    async def fake_embed_image_url(image_url: str):
        return [0.1] * 768

    async def fake_embed_text(q: str):
        return [0.1] * 768

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_image_url", staticmethod(fake_embed_image_url))
    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(fake_embed_text))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

    async def fake_rpc(fn_name, params):
        return [{"id": "p1", "name": "Trench Coat", "brand": "Acme", "distance": 0.1, "degraded": False}]

    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(fake_rpc))

    # WorkingState with a picked Vision item whose garment category is "Outer".
    # vision_outfit_style_node_primary is a DIFFERENT concept (style-node
    # letter) — it must NOT leak into p_category anymore.
    msg = ChannelMessage(chat_id=1, text="find me one like this", received_at=datetime.now(UTC))
    state = WorkingState(
        message=msg,
        chat_id=1,
        from_user_id=99,
        image_url="https://img.example.com/coat.jpg",
        detected_items=[{"label": "trench coat", "category": "Outer", "searchQuery": "beige trench coat"}],
        selected_item_index=0,
        vision_outfit_style_node_primary="C",  # style-node letter — the old mislabel
    )

    ctx = _build_ctx(state, _Sess())
    assert ctx["vision_category"] == "Outer"
    assert ctx["style_node_primary"] == "C"

    res = await sp.dispatch({"text_query": "beige trench coat"}, ctx)

    assert res["ok"] is True
    # The real Vision garment category, normalized to a canonical 20-token —
    # NOT the style-node letter "C" (which would resolve to "other").
    assert captured["category_arg"] == "Outer"
    assert captured["p_category"] == "outerwear"


@pytest.mark.asyncio
async def test_text_only_no_vision_item_resolves_to_other(monkeypatch):
    """No Vision item in ctx → vision_category None → p_category "other"
    (family gate intentionally skipped — correct graceful degrade)."""
    captured: dict[str, object] = {}

    real_build_params = SearchRepository.build_params

    def spy_build_params(**kwargs):
        params = real_build_params(**kwargs)
        captured["p_category"] = params["p_category"]
        return params

    monkeypatch.setattr(SearchRepository, "build_params", staticmethod(spy_build_params))

    async def fake_embed_text(q: str):
        return [0.1] * 768

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(fake_embed_text))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)

    async def fake_rpc(fn_name, params):
        return [{"id": "p1", "name": "Jeans", "brand": "B", "distance": 0.2, "degraded": True}]

    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(fake_rpc))

    msg = ChannelMessage(chat_id=2, text="slim dark jeans", received_at=datetime.now(UTC))
    state = WorkingState(message=msg, chat_id=2, from_user_id=7)
    ctx = _build_ctx(state, _Sess())
    assert ctx["vision_category"] is None

    res = await sp.dispatch({"text_query": "slim dark jeans"}, ctx)
    assert res["ok"] is True
    assert captured["p_category"] == "other"
