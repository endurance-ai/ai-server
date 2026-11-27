"""refine 이 직전 검색의 품목 · 색 · 핏을 이어받는다 (2026-10-18).

refine 은 품목을 ctx['vision_category'](사진 턴 전용), 색 · 핏을 ctx['color_family'] /
ctx['fit'](어디서도 만들지 않는 키)에서 읽었다. 텍스트 검색 "검정 가디건" 뒤 "더 싸게"가
품목 · 색 게이트 없이 검색됐다. dev 9/1 이후 refine 75건 중 broaden 아닌 31건이 직전
검색의 category 를 잃었고, 색 9건, 핏 4건이 같은 식으로 빠졌다.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.agents.tools.refine_search as rs
import app.agents.tools.search_products as sp

_CHAT = 727272


def _cards(n: int) -> list[dict[str, Any]]:
    return [
        {"id": i, "name": f"item {i}", "brand": f"B{i}", "price": 50000, "image_url": f"https://x/{i}.jpg"}
        for i in range(n)
    ]


@pytest.fixture
def calls(monkeypatch):
    seen: dict[str, list[dict[str, Any]]] = {"search": [], "refine": []}

    async def fake_search(**kwargs):
        seen["search"].append(kwargs)
        return _cards(15)

    async def fake_refine_search(**kwargs):
        seen["refine"].append(kwargs)
        return _cards(15)

    async def no_digest(cands, **_k):
        return None

    monkeypatch.setattr(sp, "run_text_only_search", fake_search)
    monkeypatch.setattr(rs, "run_text_only_search", fake_refine_search)
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda _ctx: "unisex")
    monkeypatch.setattr(sp, "_build_result_digest", no_digest)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr("app.agents.origin_image.get_origin_url", lambda _cid: None)
    from app.agents.last_query import clear_last_query

    clear_last_query(_CHAT)
    return seen


def _ctx() -> dict[str, Any]:
    return {"chat_id": _CHAT, "user_key": f"u:{_CHAT}", "text_query": "더 싸게"}


async def _search(**args):
    await sp.dispatch({"text_query": "black cardigan", **args}, _ctx())


@pytest.mark.asyncio
async def test_cheaper_keeps_category_color_fit(calls):
    await _search(category="cardigan", color_family="black", fit="slim")
    await rs.dispatch({"action": "cheaper", "max_price": 50000}, _ctx())

    kw = calls["refine"][-1]
    # 수정 전: category=None, color_family=None, fit=None
    assert kw["category"] == "cardigan"
    assert kw["color_family"] == "black"
    assert kw["fit"] == "slim"


@pytest.mark.asyncio
async def test_broaden_keeps_category_but_drops_color_and_fit(calls):
    await _search(category="cardigan", color_family="black", fit="slim")
    await rs.dispatch({"action": "broaden"}, _ctx())

    kw = calls["refine"][-1]
    assert kw["category"] == "cardigan"
    assert kw["color_family"] is None and kw["fit"] is None


@pytest.mark.asyncio
async def test_color_swap_uses_new_color_or_none(calls):
    await _search(category="cardigan", color_family="black")
    await rs.dispatch({"action": "color_swap", "color": "navy"}, _ctx())
    assert calls["refine"][-1]["color_family"] == "navy"

    await _search(category="cardigan", color_family="black")
    await rs.dispatch({"action": "color_swap"}, _ctx())
    assert calls["refine"][-1]["color_family"] is None


@pytest.mark.asyncio
async def test_chained_refine_carries_previous_refine_color(calls):
    await _search(category="cardigan", color_family="black")
    await rs.dispatch({"action": "color_swap", "color": "navy"}, _ctx())
    await rs.dispatch({"action": "cheaper", "max_price": 50000}, _ctx())

    kw = calls["refine"][-1]
    assert kw["category"] == "cardigan"
    assert kw["color_family"] == "navy"


@pytest.mark.asyncio
async def test_search_without_filters_clears_previous(calls):
    await _search(category="cardigan", color_family="black")
    await sp.dispatch({"text_query": "something nice"}, _ctx())
    await rs.dispatch({"action": "cheaper", "max_price": 50000}, _ctx())

    kw = calls["refine"][-1]
    assert kw["category"] is None
    assert kw["color_family"] is None
