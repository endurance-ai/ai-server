"""B22 — search_products MUST merge `boost_keywords` into the embedded
text_query and apply `exclude_keywords` as a client-side filter.

Pre-fix history (regression guard): both fields were declared in the
SearchProductsArgs TypedDict from the V2 ReAct rollout (e21f746, 2026-05-16)
but the dispatch never read them. LLM correctly extracted brand-reference
boost tokens ("stomper chunky heavy" for a 발렌시아가 스톰퍼 query) and the
dispatch silently threw them away (Langfuse trace 499840bb, 09:22 73e5c867).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.agents.tools.search_products as sp


@pytest.mark.asyncio
async def test_boost_keywords_merge_into_text_query(monkeypatch):
    """LLM-supplied `boost_keywords` MUST end up in the embedded text_query
    (the actual string passed to Modal /embed/text). Pre-fix the boost was
    silently dropped."""
    captured: dict[str, str] = {}

    async def spy_embed_text(q: str):
        captured["embed_text"] = q
        return [0.1] * 768

    async def stub_rpc(fn_name, params):
        return [{"id": "p1", "name": "Item", "brand": "Acme", "distance": 0.2, "degraded": False}]

    async def stub_diversify(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(spy_embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(stub_rpc))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_diversify)
    # Skip pre-message + gender ask paths.
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")

    args = {
        "text_query": "bold boots women",
        "boost_keywords": ["stomper", "chunky", "heavy"],
    }
    ctx = {"chat_id": 7, "user_key": "u:7"}
    await sp.dispatch(args, ctx)

    embedded = captured["embed_text"]
    # All three boost tokens flow through to the embedding.
    assert "stomper" in embedded
    assert "chunky" in embedded
    assert "heavy" in embedded
    # Original text_query order preserved at the head.
    assert embedded.startswith("bold boots women")


@pytest.mark.asyncio
async def test_boost_keywords_dedup_against_text_query(monkeypatch):
    """When the boost overlaps tokens already in text_query, dedup avoids
    embedding the same token twice."""
    captured: dict[str, str] = {}

    async def spy_embed_text(q: str):
        captured["embed_text"] = q
        return [0.1] * 768

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(spy_embed_text))
    monkeypatch.setattr(
        "app.pipeline.search.DatabaseProvider.rpc",
        staticmethod(AsyncMock(return_value=[])),
    )

    async def stub_diversify(state):
        state.final_candidates = []
        return state

    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_diversify)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")

    args = {
        "text_query": "black blazer women",
        "boost_keywords": ["blazer", "cropped"],  # 'blazer' already in text_query
    }
    await sp.dispatch(args, {"chat_id": 7, "user_key": "u:7"})

    embedded = captured["embed_text"]
    # 'blazer' appears exactly once (no token duplication).
    assert embedded.count("blazer") == 1
    # 'cropped' was a genuine new token → added.
    assert "cropped" in embedded


@pytest.mark.asyncio
async def test_exclude_keywords_filters_candidates(monkeypatch):
    """LLM-supplied `exclude_keywords` MUST drop candidates whose name
    contains any excluded token (case-insensitive, same as refine_search)."""

    async def stub_embed_text(q: str):
        return [0.1] * 768

    async def stub_rpc(fn_name, params):
        return [
            {"id": "keep1", "name": "Linen Wide Pants", "brand": "A", "distance": 0.1, "degraded": False},
            {"id": "drop1", "name": "Denim Slim Jean", "brand": "B", "distance": 0.2, "degraded": False},
            {"id": "keep2", "name": "Cotton Trousers", "brand": "C", "distance": 0.3, "degraded": False},
            {"id": "drop2", "name": "JEAN washed flare", "brand": "D", "distance": 0.4, "degraded": False},
        ]

    async def stub_diversify(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(stub_embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(stub_rpc))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_diversify)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")

    args = {
        "text_query": "wide pants women",
        "exclude_keywords": ["jean"],
    }
    ctx = {"chat_id": 7, "user_key": "u:7"}
    result = await sp.dispatch(args, ctx)
    top_ids = {c.get("product_id") for c in result["top_candidates"]}
    assert "keep1" in top_ids
    assert "keep2" in top_ids
    assert "drop1" not in top_ids  # 'jean' lowercase in name
    assert "drop2" not in top_ids  # 'JEAN' uppercase in name — still filtered
