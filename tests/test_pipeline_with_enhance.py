"""Integration tests for pipeline with enhance_query (SPEC-PIPELINE-001).

embed_step (Modal) 과 search_step (Supabase RPC) 을 mock 으로 대체해
runner.run_pipeline 전 경로를 검증한다. search_step 호출 시 query_text 인자를
capture 해서 enhanced vs raw 사용을 명시 검증한다.
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
    """Supabase RPC mock — 호출 인자를 capture 한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, fn_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((fn_name, params))
        # 다양성 캡 통과 후 1개 정도가 응답에 포함되도록 최소 결과 반환
        return [
            {
                "id": "p-1",
                "brand": "uniqlo",
                "name": "Knit",
                "score": 0.8,
                "platform": "shop",
                "subcategory": "knit",
                "dense_rank": 1,
                "sparse_rank": 1,
            }
        ]


@pytest.fixture
def patch_pipeline(monkeypatch):
    """embed + supabase RPC 를 항상 mock. LLM 은 케이스별 override."""
    monkeypatch.setattr(
        "app.pipeline.embed.EmbedProvider.embed_image_url",
        AsyncMock(return_value=[0.1] * 1024),
    )
    rpc = _RPCCapture()
    monkeypatch.setattr("app.pipeline.search.SupabaseProvider.rpc", rpc)
    return rpc


async def test_pipeline_enhance_ok_uses_refined_query(patch_pipeline, monkeypatch, capsys):
    """flag=on + LLM 정상 → search_step 이 refined query 로 호출됨."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value=_ok_chat_response()),
    )

    # DEBUG: verify settings instance identity + flag
    import sys as _sys
    from app.pipeline import enhance_query as _eq
    print(
        f"DEBUG_ENHANCE id(app_settings)={id(app_settings)} "
        f"id(eq.settings)={id(_eq.settings)} "
        f"flag_app={app_settings.ENHANCE_QUERY_ENABLED} "
        f"flag_eq={_eq.settings.ENHANCE_QUERY_ENABLED} "
        f"sys.modules.app.core.config.settings.flag="
        f"{_sys.modules['app.core.config'].settings.ENHANCE_QUERY_ENABLED}",
        flush=True,
    )

    resp = await run_pipeline(_build_request())

    # search_step 이 refined_ko 를 받았어야 함
    assert len(patch_pipeline.calls) == 1
    fn_name, params = patch_pipeline.calls[0]
    assert fn_name == "search_products_v5"
    assert params["query_text"] == "베이지 오버사이즈 니트 스웨터"
    # 응답 정상
    assert resp.item_id == "item-1"


async def test_pipeline_enhance_fallback_uses_raw_query(patch_pipeline, monkeypatch):
    """flag=on + LLM 타임아웃 → search_step 이 raw search_query_ko 로 호출됨."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(side_effect=TimeoutError()),
    )

    resp = await run_pipeline(_build_request())

    fn_name, params = patch_pipeline.calls[0]
    assert params["query_text"] == "베이지색 오버사이즈 니트 스웨터 가을 데일리룩"
    assert resp.item_id == "item-1"


async def test_pipeline_enhance_disabled_uses_raw_query(patch_pipeline, monkeypatch):
    """flag=off → LLM 호출 0회, search_step 이 raw 사용."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", False)
    chat_mock = AsyncMock(return_value=_ok_chat_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    await run_pipeline(_build_request())

    assert chat_mock.await_count == 0
    fn_name, params = patch_pipeline.calls[0]
    assert params["query_text"] == "베이지색 오버사이즈 니트 스웨터 가을 데일리룩"


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

    # raw 쿼리로 fallback 되어 search 호출됨
    assert resp.item_id == "item-1"
    fn_name, params = patch_pipeline.calls[0]
    assert params["query_text"] == "베이지색 오버사이즈 니트 스웨터 가을 데일리룩"


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
    assert params["query_text"] == "베이지 오버사이즈 니트 스웨터"
    assert resp.item_id == "item-1"


async def test_pipeline_skipped_empty_query(patch_pipeline, monkeypatch):
    """search_query 양쪽 모두 빈 문자열 → status=skipped, raw(빈) 쿼리로 search 호출."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    chat_mock = AsyncMock(return_value=_ok_chat_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    req = _build_request(search_query="", search_query_ko="")
    await run_pipeline(req)

    assert chat_mock.await_count == 0
    fn_name, params = patch_pipeline.calls[0]
    # raw search_query_ko ("") or search_query ("") → 빈 문자열
    assert params["query_text"] == ""
