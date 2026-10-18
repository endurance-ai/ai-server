"""가격 경계를 검색 RPC 하드 필터로 넘긴다 (2026-10-18).

예전에는 RPC 가 가격을 몰라 유사도 상위 50개를 받은 뒤 앱(apply_price_filter)에서
걸렀다. 상위 50개 안에 조건에 맞는 상품이 없으면 카탈로그에 싼 상품이 있어도 0건이
됐다(dev '5만원 이하 니트' 로그: kept=0 dropped=30). dev 60일 툴 호출 기록에서 가격
조건이 붙은 검색 62건 중 22건이 0~4건이었다. 이제 p_min_price / p_max_price 로 RPC
안에서 거르고, refine 의 결과 부족 구제는 올린 상한으로 RPC 를 다시 부른다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.agents.tools.refine_search as rs
import app.agents.tools.search_products as sp
from app.agents.last_query import set_last_query

_ROOT = Path(__file__).resolve().parents[2]

# 유사도 순으로 비싼 상품 60개가 앞에 있고, 조건에 맞는 싼 상품 5개는 그 뒤에 있다.
_CATALOG = [
    {"id": f"p{i}", "name": f"Knit {i}", "brand": f"B{i}", "price": 150000, "distance": 0.1 + i * 0.001}
    for i in range(60)
] + [
    {"id": f"c{i}", "name": f"Cheap knit {i}", "brand": f"C{i}", "price": 39000, "distance": 0.5 + i * 0.001}
    for i in range(5)
]


def _fake_rpc(calls: list[dict[str, Any]]):
    """DB 처럼 동작하는 RPC: 가격 키가 오면 거른 뒤 상위 p_limit 를 돌려준다."""

    async def rpc(fn_name, params):
        calls.append(dict(params, _fn=fn_name))
        lo, hi = params.get("p_min_price"), params.get("p_max_price")
        rows = [r for r in _CATALOG if (lo is None or r["price"] >= lo) and (hi is None or r["price"] <= hi)]
        return [dict(r, degraded=False) for r in rows[: params.get("p_limit", 50)]]

    return rpc


@pytest.fixture
def pipeline(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def embed_text(q: str):
        return [0.1] * 768

    async def keep_all(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(embed_text))
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(_fake_rpc(calls)))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", keep_all)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda _ctx: "unisex")
    return calls


@pytest.mark.asyncio
async def test_cheap_items_beyond_top50_are_found(pipeline):
    # 수정 전: RPC 가 비싼 상위 50개만 돌려주고 앱에서 걸러 0건.
    res = await sp.dispatch({"text_query": "knit sweater", "max_price": "50000"}, {"chat_id": 31, "user_key": "u:31"})

    assert pipeline[-1]["p_max_price"] == 50000
    assert "p_min_price" not in pipeline[-1]
    ids = {c.get("product_id") for c in res["top_candidates"]}
    assert ids and all(i.startswith("c") for i in ids)


@pytest.mark.asyncio
async def test_slider_ceiling_reaches_rpc(pipeline):
    await sp.dispatch({"text_query": "knit sweater"}, {"chat_id": 32, "user_key": "u:32", "req_price_max": 70000})

    assert pipeline[-1]["p_max_price"] == 70000


@pytest.mark.asyncio
async def test_no_price_sends_no_price_keys(pipeline):
    await sp.dispatch({"text_query": "knit sweater"}, {"chat_id": 33, "user_key": "u:33"})

    assert "p_max_price" not in pipeline[-1]
    assert "p_min_price" not in pipeline[-1]


@pytest.mark.parametrize(("raw", "expected"), [("50000", 50000), (50000.0, 50000), (0, None), ("abc", None)])
def test_price_bound_coercion(raw, expected):
    assert sp.price_bound(raw) == expected


@pytest.mark.asyncio
async def test_refine_rescue_researches_with_raised_ceiling(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_search(**kwargs):
        calls.append(kwargs)
        n = 1 if kwargs.get("max_price") == 50000 else 6
        return [
            {"id": i, "name": f"k{i}", "brand": "B", "price": 45000, "image_url": f"https://x/{i}.jpg"}
            for i in range(n)
        ]

    async def no_digest(cands, **_k):
        return None

    monkeypatch.setattr(rs, "run_text_only_search", fake_search)
    monkeypatch.setattr("app.agents.tools.search_products._build_result_digest", no_digest)
    monkeypatch.setattr("app.agents.origin_image.get_origin_url", lambda _cid: None)
    set_last_query(34, "knit sweater")

    res = await rs.dispatch({"action": "cheaper", "max_price": 50000}, {"chat_id": 34, "text_query": "x"})

    # 수정 전: 가격을 넘기지 않고 받은 후보 안에서만 다시 걸렀다.
    assert [c.get("max_price") for c in calls] == [50000, 62500]
    assert res["candidates_count"] == 6


@pytest.mark.parametrize("fname", ["search_products_hybrid_v1.sql", "search_products_v6.sql"])
def test_sql_applies_price_wherever_platform_gate_applies(fname):
    sql = (_ROOT / "sql" / "functions" / fname).read_text()
    assert re.search(r"p_min_price\s+integer DEFAULT NULL", sql)
    assert re.search(r"p_max_price\s+integer DEFAULT NULL", sql)
    gates = sql.count("AND (p_platform IS NULL OR p.platform = p_platform)")
    assert gates > 0
    assert sql.count("AND (p_min_price IS NULL OR p.price >= p_min_price)") == gates
    assert sql.count("AND (p_max_price IS NULL OR p.price <= p_max_price)") == gates
    # 가격 조건이 있을 때만 HNSW 반복 스캔 — 없으면 인덱스가 꺼낸 40개 안에서만 걸러진다.
    assert "set_config('hnsw.iterative_scan', 'relaxed_order', true)" in sql
