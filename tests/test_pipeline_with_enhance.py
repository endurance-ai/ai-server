"""Integration tests for pipeline with enhance_query (SPEC-PIPELINE-001;
v6-migrated by SPEC-SEARCH-V6-001).

embed_step (Modal) 과 search_step (DB RPC) 을 mock 으로 대체해
runner.run_pipeline 전 경로를 검증한다. v6 는 embedding-first 라 RPC 에
query_text 파라미터가 없다 — enhance_query_step 은 여전히 파이프라인에서
실행되지만 그 출력은 RPC 로 전달되지 않는다 (모듈은 휴면 상태로 보존).
따라서 enhanced-vs-raw 분기 검증은 RPC param 이 아니라 enhance_query_step
호출 횟수 / 응답 정상성으로 확인한다 (v6 param dict 는 6-key 고정).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.models.request import AnalyzedItem, RecommendRequest
from app.pipeline.runner import run_pipeline


def _build_request(
    *,
    search_query: str = "beige oversized knit sweater for autumn daily look",
    search_query_ko: str | None = "베이지색 오버사이즈 니트 스웨터 가을 데일리룩",
) -> RecommendRequest:
    item = AnalyzedItem(
        id="item-1",
        category="top",
        subcategory="knit",
        searchQuery=search_query,
        searchQueryKo=search_query_ko,
    )
    return RecommendRequest(
        item=item,
        imageUrl="https://example.com/x.jpg",
    )


def _ok_chat_response(
    refined_ko: str = "베이지 오버사이즈 니트 스웨터",
    refined_en: str = "beige oversized knit sweater",
) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"refined_ko": refined_ko, "refined_en": refined_en}, ensure_ascii=False),
                }
            }
        ]
    }


class _RPCCapture:
    """DB RPC mock — 호출 인자를 capture 한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, fn_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((fn_name, params))
        # v6 row shape: distance (cosine, ASC=better) + degraded; no
        # score/dense_rank/sparse_rank. id is bigint (int).
        return [
            {
                "id": 1,
                "brand": "uniqlo",
                "name": "Knit",
                "distance": 0.2,
                "platform": "shop",
                "subcategory": "knit",
                "degraded": False,
            }
        ]


@pytest.fixture
def patch_pipeline(monkeypatch):
    """embed + DB RPC 를 항상 mock. LLM 은 케이스별 override."""
    monkeypatch.setattr(
        "app.pipeline.embed.EmbedProvider.embed_image_url",
        AsyncMock(return_value=[0.1] * 1024),
    )
    rpc = _RPCCapture()
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", rpc)
    return rpc


# Expected v6 RPC param dict (the 6 keys). query_embedding is the formatted
# pgvector string of the mocked image embedding ([0.1]*1024 → ":.7f" each).
# SPEC-SEARCH-V6-001 family-gate re-point: the request item is category="top",
# which to_canonical_family normalizes to the canonical token "tops" — so
# p_category is "tops" (was None pre-family-gate). p_subcategory stays None
# (products.subcategory is 100% NULL repo-wide → narrowing is a no-op).
def _expected_v6_params() -> dict[str, Any]:
    return {
        "query_embedding": "[" + ",".join(["0.1000000"] * 1024) + "]",
        "p_style_node_id": None,
        "p_category": "tops",
        "p_subcategory": None,
        "p_brand_names": None,
        "p_limit": 50,
    }


async def test_pipeline_enhance_ok_v6_params(patch_pipeline, monkeypatch, capsys):
    """flag=on + LLM 정상 → v6 RPC 가 6-key param dict 로 호출됨 (query_text
    파라미터 없음; enhance 출력은 RPC 로 전달되지 않음)."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    chat_mock = AsyncMock(return_value=_ok_chat_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    resp = await run_pipeline(_build_request())

    assert len(patch_pipeline.calls) == 1
    fn_name, params = patch_pipeline.calls[0]
    assert fn_name == "search_products_v6"
    assert params == _expected_v6_params()
    assert "query_text" not in params  # v6 has no text param
    assert resp.item_id == "item-1"


async def test_pipeline_enhance_fallback_still_v6_params(patch_pipeline, monkeypatch):
    """flag=on + LLM 타임아웃 → enhance fallback 되어도 v6 param dict 동일
    (enhance 출력이 RPC 에 영향을 주지 않음)."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(side_effect=TimeoutError()),
    )

    resp = await run_pipeline(_build_request())

    fn_name, params = patch_pipeline.calls[0]
    assert fn_name == "search_products_v6"
    assert params == _expected_v6_params()
    assert resp.item_id == "item-1"


async def test_pipeline_enhance_disabled_no_llm_call(patch_pipeline, monkeypatch):
    """flag=off → LLM 호출 0회, v6 param dict 동일."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", False)
    chat_mock = AsyncMock(return_value=_ok_chat_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    await run_pipeline(_build_request())

    assert chat_mock.await_count == 0
    fn_name, params = patch_pipeline.calls[0]
    assert fn_name == "search_products_v6"
    assert params == _expected_v6_params()


async def test_pipeline_parallel_enhance_exception_isolated(patch_pipeline, monkeypatch):
    """parallel=on + enhance 측 예외 → embed 결과 보존, 추천 응답 200 OK 동작."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    monkeypatch.setattr(app_settings, "PIPELINE_PARALLEL_ENABLED", True)

    # enhance_query_step 자체는 예외를 raise 안 하지만, 만약 raise 되더라도 격리되어야 함
    # → enhance_query_step 의 top-level guard 확인용으로 LLMProvider.chat 이 raise 하도록.
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(side_effect=RuntimeError("unexpected")),
    )

    resp = await run_pipeline(_build_request())

    # enhance 측 예외가 격리되어도 v6 search 정상 호출 + 200 OK
    assert resp.item_id == "item-1"
    fn_name, params = patch_pipeline.calls[0]
    assert fn_name == "search_products_v6"
    assert params == _expected_v6_params()


async def test_pipeline_sequential_mode(patch_pipeline, monkeypatch):
    """parallel=off → sequential 경로도 동일하게 동작."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    monkeypatch.setattr(app_settings, "PIPELINE_PARALLEL_ENABLED", False)
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value=_ok_chat_response()),
    )

    resp = await run_pipeline(_build_request())

    fn_name, params = patch_pipeline.calls[0]
    assert fn_name == "search_products_v6"
    assert params == _expected_v6_params()
    assert resp.item_id == "item-1"


async def test_pipeline_skipped_empty_query(patch_pipeline, monkeypatch):
    """search_query 양쪽 모두 빈 문자열 → enhance skip (LLM 0회), v6 param 동일
    (v6 는 query_text 가 없으므로 빈 쿼리도 RPC param 에 영향 없음)."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    chat_mock = AsyncMock(return_value=_ok_chat_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    req = _build_request(search_query="", search_query_ko="")
    await run_pipeline(req)

    assert chat_mock.await_count == 0
    fn_name, params = patch_pipeline.calls[0]
    assert fn_name == "search_products_v6"
    assert params == _expected_v6_params()
