"""refine_search: broaden 이 실제로 넓히고, 같은 결과면 정직하게 알린다 (2026-09-27).

9월 실유저 3턴("색상은 자유롭게 봤어", "더 많이 담았어", "더 넓게 봤어") 모두
refine_search(action="broaden")를 불렀지만 broaden 이 no-op 이라 직전 검색을 그대로
재실행 → 화면 카드는 같은데 봇은 새로 찾은 척했다.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.agents.tools.refine_search as rs
from app.agents.last_query import set_last_brand, set_last_query
from app.agents.tools.search_products import _to_card_candidate
from app.infrastructure.memory.session import get_store

_CHAT = 515151


def _cards(ids: list[int]) -> list[dict[str, Any]]:
    return [
        {"id": i, "name": f"item {i}", "brand": "B", "price": 50000, "image_url": f"https://x/{i}.jpg"} for i in ids
    ]


def _prime_session(visible: list[int], shown: list[int]) -> None:
    store = get_store()
    sess = store.get_or_create(_CHAT)
    sess.last_results = [_to_card_candidate(c) for c in _cards(visible)]
    sess.shown_product_ids = [str(i) for i in shown]
    store.update(sess)


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict[str, Any]] = []
    result_ids: dict[str, list[int]] = {"ids": []}

    async def fake_search(**kwargs):
        calls.append(kwargs)
        return _cards(result_ids["ids"])

    async def no_digest(cands, **_k):
        return None

    monkeypatch.setattr(rs, "run_text_only_search", fake_search)
    monkeypatch.setattr("app.agents.tools.search_products._build_result_digest", no_digest)
    monkeypatch.setattr("app.agents.origin_image.get_origin_url", lambda _cid: None)
    return calls, result_ids


def _ctx() -> dict[str, Any]:
    return {"chat_id": _CHAT, "color_family": "red", "fit": "slim", "style_node_primary": "C", "text_query": "x"}


@pytest.mark.asyncio
async def test_broaden_drops_filters_and_skips_shown(captured):
    calls, result_ids = captured
    set_last_query(_CHAT, "fitted vest women burgundy")
    set_last_brand(_CHAT, ["Birrot"])
    _prime_session(visible=list(range(1, 16)), shown=list(range(1, 16)))
    result_ids["ids"] = list(range(1, 31))  # 넓게 가져오면 앞 15개는 이미 본 것

    res = await rs.dispatch({"action": "broaden"}, _ctx())

    kw = calls[-1]
    assert kw["brand_filter"] is None
    assert kw["color_family"] is None and kw["fit"] is None and kw["style_node_primary"] is None
    assert kw["top_k"] == rs._BROADEN_POOL
    assert "burgundy" not in kw["text_query"]
    assert res["new_count"] == 15
    assert "notice" not in res or "unchanged" not in (res.get("notice") or "")


@pytest.mark.asyncio
async def test_same_results_as_screen_carry_unchanged_notice(captured):
    _, result_ids = captured
    set_last_query(_CHAT, "star moon earrings women")
    _prime_session(visible=list(range(1, 16)), shown=list(range(1, 16)))
    result_ids["ids"] = list(range(1, 16))  # 카탈로그에 새 게 없음

    res = await rs.dispatch({"action": "cheaper"}, _ctx())

    assert res["new_count"] == 0
    assert "unchanged" in res["notice"]


@pytest.mark.asyncio
async def test_non_broaden_refine_keeps_filters(captured):
    calls, result_ids = captured
    set_last_query(_CHAT, "fitted vest women")
    set_last_brand(_CHAT, ["Birrot"])
    _prime_session(visible=[1, 2, 3], shown=[1, 2, 3])
    result_ids["ids"] = [4, 5, 6]

    res = await rs.dispatch({"action": "cheaper", "max_price": 80000}, _ctx())

    kw = calls[-1]
    assert kw["brand_filter"] == ["Birrot"]
    assert kw["color_family"] == "red"
    assert kw["top_k"] == 15
    assert res["new_count"] == 3
