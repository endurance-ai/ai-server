"""Per-request mobile filter UI (gender + price_max) plumbing.

The mobile filter UI sends gender (공용/여성/남성) + a price slider with every
chat message. ChatRequest normalizes them and chat_service threads them into
InputState → react_loop._build_ctx → ctx (`req_gender` / `req_price_max`).
search_products / refine_search read those as overrides:

  - req_gender wins over the pinned taste profile (per-request only, no persist)
  - req_price_max is the price ceiling fallback when the LLM omits max_price
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.agents.tools.search_products as sp
from app.api.chat import ChatRequest

# ── ChatRequest normalization ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("남성", "men"),
        ("male", "men"),
        ("MEN", "men"),
        ("여성", "women"),
        ("female", "women"),
        ("공용", "unisex"),
        ("other", "unisex"),
        ("상관없음", "unisex"),
        ("garbage", None),
        (None, None),
    ],
)
def test_chat_request_gender_normalize(raw, expected):
    req = ChatRequest(message="hi", gender=raw)
    assert req.gender == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (50000, 50000),
        ("80000", 80000),
        (0, None),
        (-100, None),
        (None, None),
        ("bad", None),
    ],
)
def test_chat_request_price_max_clamp(raw, expected):
    req = ChatRequest(message="hi", price_max=raw)
    assert req.price_max == expected


# ── search_products dispatch overrides ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_req_gender_overrides_profile_pin(monkeypatch):
    """ctx['req_gender'] (filter UI) MUST win over the pinned taste profile."""
    captured: dict[str, str] = {}

    async def spy_embed_text(q: str):
        captured["embed_text"] = q
        return [0.1] * 768

    async def stub_rpc(fn_name, params):
        return []

    async def stub_diversify(state):
        state.final_candidates = []
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(spy_embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(stub_rpc))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_diversify)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    # Profile is pinned to women, but the request filter says men → men wins.
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")

    args = {"text_query": "black hoodie"}
    ctx = {"chat_id": 7, "user_key": "u:7", "req_gender": "men"}
    await sp.dispatch(args, ctx)

    embedded = captured["embed_text"]
    assert embedded.endswith("men")
    assert "women" not in embedded


@pytest.mark.asyncio
async def test_req_price_max_applied_when_llm_omits(monkeypatch):
    """ctx['req_price_max'] MUST cap results when the LLM supplies no max_price."""

    async def stub_embed_text(q: str):
        return [0.1] * 768

    async def stub_rpc(fn_name, params):
        return [
            {"id": "cheap", "name": "Tee", "brand": "A", "price": 30000, "distance": 0.1, "degraded": False},
            {"id": "mid", "name": "Tee", "brand": "B", "price": 49000, "distance": 0.2, "degraded": False},
            {"id": "pricey", "name": "Tee", "brand": "C", "price": 90000, "distance": 0.3, "degraded": False},
        ]

    async def stub_diversify(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(stub_embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(stub_rpc))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", stub_diversify)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "unisex")

    args = {"text_query": "white tee"}  # no max_price from the LLM
    ctx = {"chat_id": 7, "user_key": "u:7", "req_price_max": 50000}
    result = await sp.dispatch(args, ctx)

    top_ids = {c.get("product_id") for c in result["top_candidates"]}
    assert "cheap" in top_ids
    assert "mid" in top_ids
    assert "pricey" not in top_ids  # 90000 > 50000 ceiling


def test_effective_max_price_arg_wins_over_ctx():
    # LLM-supplied arg takes priority over the filter slider.
    assert sp.effective_max_price(20000, {"req_price_max": 50000}) == 20000
    # No arg → fall back to the filter ceiling.
    assert sp.effective_max_price(None, {"req_price_max": 50000}) == 50000
    # Neither → None (no ceiling).
    assert sp.effective_max_price(None, {}) is None
    # Non-positive filter value → treated as no ceiling.
    assert sp.effective_max_price(None, {"req_price_max": 0}) is None
