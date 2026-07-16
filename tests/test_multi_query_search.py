"""상황/TPO 쿼리 멀티 확장 테스트 (2026-07-16 Phase 4a).

`run_multi_query_search` 는 여러 아이템 쿼리를 병렬 검색 후 라운드로빈
인터리브 병합한다. gender 는 모든 서브쿼리에 동일 적용, 부분 실패 허용,
id dedup, top_k 상한을 검증한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.tools import search_products


def _c(pid: int, brand: str = "b") -> dict[str, Any]:
    return {"id": pid, "brand": brand, "name": f"item-{pid}"}


@pytest.fixture
def spy_search(monkeypatch):
    """run_text_only_search 를 쿼리별 고정 결과 + 호출 인자 기록으로 대체."""
    calls: list[dict[str, Any]] = []
    table: dict[str, list[dict[str, Any]]] = {}

    async def fake(**kwargs):
        calls.append(kwargs)
        q = kwargs["text_query"]
        if isinstance(table.get(q), Exception):
            raise table[q]
        return list(table.get(q, []))

    monkeypatch.setattr(search_products, "run_text_only_search", fake)
    return calls, table


async def test_interleave_round_robin(spy_search):
    calls, table = spy_search
    table["dress"] = [_c(1), _c(2), _c(3)]
    table["blouse"] = [_c(4), _c(5)]
    table["heels"] = [_c(6)]
    out = await search_products.run_multi_query_search(queries=["dress", "blouse", "heels"], top_k=15)
    # 라운드로빈: dress[0], blouse[0], heels[0], dress[1], blouse[1], dress[2]
    assert [c["id"] for c in out] == [1, 4, 6, 2, 5, 3]


async def test_gender_applied_to_every_subquery(spy_search):
    calls, table = spy_search
    table.update({"dress": [_c(1)], "blouse": [_c(2)]})
    await search_products.run_multi_query_search(queries=["dress", "blouse"], gender="women", top_k=15)
    assert all(c["gender"] == "women" for c in calls)
    assert {c["text_query"] for c in calls} == {"dress", "blouse"}


async def test_dedup_across_subqueries(spy_search):
    calls, table = spy_search
    # 같은 상품이 두 쿼리에 등장 → 한 번만
    table["dress"] = [_c(1), _c(2)]
    table["gown"] = [_c(1), _c(3)]
    out = await search_products.run_multi_query_search(queries=["dress", "gown"], top_k=15)
    ids = [c["id"] for c in out]
    assert ids == [1, 2, 3]
    assert len(ids) == len(set(ids))


async def test_partial_failure_tolerated(spy_search):
    calls, table = spy_search
    table["dress"] = [_c(1), _c(2)]
    table["boom"] = RuntimeError("modal down")
    out = await search_products.run_multi_query_search(queries=["dress", "boom"], top_k=15)
    assert [c["id"] for c in out] == [1, 2]


async def test_top_k_cap(spy_search):
    calls, table = spy_search
    table["a"] = [_c(i) for i in range(10)]
    table["b"] = [_c(i) for i in range(10, 20)]
    out = await search_products.run_multi_query_search(queries=["a", "b"], top_k=5)
    assert len(out) == 5
