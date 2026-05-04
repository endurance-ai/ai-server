"""Unit tests for enhance_query_step (SPEC-PIPELINE-001).

LLMProvider.chat 을 mock 으로 대체해 모든 폴백 분기를 검증한다.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.models.request import AnalyzedItem, RecommendRequest
from app.pipeline.enhance_query import enhance_query_step
from app.pipeline.state import PipelineState


def _make_state(
    *,
    search_query: str = "beige oversized knit sweater for autumn",
    search_query_ko: str | None = "베이지색 오버사이즈 니트 스웨터 가을 데일리룩",
    subcategory: str | None = "knit",
) -> PipelineState:
    item = AnalyzedItem(
        id="item-1",
        category="top",
        subcategory=subcategory,
        searchQuery=search_query,
        searchQueryKo=search_query_ko,
    )
    req = RecommendRequest(item=item, imageUrl="https://example.com/x.jpg")
    return PipelineState(request=req)


def _ok_response(
    refined_ko: str = "베이지 오버사이즈 니트 스웨터",
    refined_en: str = "beige oversized knit sweater",
) -> dict[str, Any]:
    import json

    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"refined_ko": refined_ko, "refined_en": refined_en}, ensure_ascii=False),
                }
            }
        ]
    }


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """기본은 enabled=True. 개별 테스트가 필요시 override."""
    from app.core.config import settings as app_settings
    from app.pipeline import enhance_query as eq_mod

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", True)
    # enhance_query 모듈은 import time 에 settings 를 그대로 참조하므로 monkeypatch 가 충분.
    yield eq_mod


async def test_enhance_query_ok(monkeypatch):
    """정상 응답 → status=ok, enhanced_* 채워짐."""
    chat_mock = AsyncMock(return_value=_ok_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "ok"
    assert out.enhanced_query_ko == "베이지 오버사이즈 니트 스웨터"
    assert out.enhanced_query == "beige oversized knit sweater"
    assert chat_mock.await_count == 1


async def test_enhance_query_timeout(monkeypatch):
    """asyncio.TimeoutError → status=fallback, fallback_reason=timeout."""

    async def _slow(*args, **kwargs):
        raise TimeoutError()

    # asyncio.wait_for 가 직접 TimeoutError 를 raise 하도록 chat 자체에서 raise
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", AsyncMock(side_effect=TimeoutError()))

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"
    assert out.enhanced_query is None
    assert out.enhanced_query_ko is None


async def test_enhance_query_http_5xx(monkeypatch):
    """httpx.HTTPStatusError 5xx → status=fallback."""
    fake_response = httpx.Response(status_code=503, request=httpx.Request("POST", "http://x"))
    err = httpx.HTTPStatusError("server error", request=fake_response.request, response=fake_response)
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", AsyncMock(side_effect=err))

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_http_4xx(monkeypatch):
    """httpx.HTTPStatusError 4xx → status=fallback."""
    fake_response = httpx.Response(status_code=400, request=httpx.Request("POST", "http://x"))
    err = httpx.HTTPStatusError("bad request", request=fake_response.request, response=fake_response)
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", AsyncMock(side_effect=err))

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_network_error(monkeypatch):
    """httpx.RequestError → status=fallback."""
    err = httpx.ConnectError("dns failure", request=httpx.Request("POST", "http://x"))
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", AsyncMock(side_effect=err))

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_empty_response(monkeypatch):
    """빈 content → status=fallback."""
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value={"choices": [{"message": {"content": ""}}]}),
    )

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_malformed_response_shape(monkeypatch):
    """choices/message 구조 깨짐 → status=fallback (empty 분기)."""
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value={"unexpected": "shape"}),
    )

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_parse_error(monkeypatch):
    """JSON 파싱 실패 + regex 폴백도 실패 → status=fallback."""
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value={"choices": [{"message": {"content": "이건 JSON이 아닙니다 그냥 문장입니다"}}]}),
    )

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_regex_fallback_succeeds(monkeypatch):
    """평문 안에 JSON 블록이 있으면 정규식으로 추출 성공 → status=ok."""
    content = 'Here you go: {"refined_ko": "니트 스웨터", "refined_en": "knit sweater"} 라고 합니다.'
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value={"choices": [{"message": {"content": content}}]}),
    )

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "ok"
    assert out.enhanced_query_ko == "니트 스웨터"
    assert out.enhanced_query == "knit sweater"


async def test_enhance_query_length_invalid_empty(monkeypatch):
    """refined_ko 가 빈 문자열 → status=fallback (length_invalid)."""
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value=_ok_response(refined_ko="", refined_en="x")),
    )

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_length_invalid_too_long(monkeypatch):
    """refined 길이 200 초과 → status=fallback."""
    long_str = "a" * 201
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value=_ok_response(refined_ko=long_str, refined_en="ok")),
    )

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_missing_keys(monkeypatch):
    """refined_ko 만 있고 refined_en 누락 → 파싱 실패로 처리되어 status=fallback."""
    import json

    content = json.dumps({"refined_ko": "니트"})
    monkeypatch.setattr(
        "app.pipeline.enhance_query.LLMProvider.chat",
        AsyncMock(return_value={"choices": [{"message": {"content": content}}]}),
    )

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "fallback"


async def test_enhance_query_disabled(monkeypatch):
    """ENHANCE_QUERY_ENABLED=false → status=disabled, LLM 호출 0."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", False)
    chat_mock = AsyncMock(return_value=_ok_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    state = _make_state()
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "disabled"
    assert chat_mock.await_count == 0


async def test_enhance_query_skipped_empty_input(monkeypatch):
    """search_query, search_query_ko 모두 비어있음 → status=skipped, LLM 호출 0."""
    chat_mock = AsyncMock(return_value=_ok_response())
    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", chat_mock)

    state = _make_state(search_query="", search_query_ko="")
    out = await enhance_query_step(state)

    assert out.enhance_query_status == "skipped"
    assert chat_mock.await_count == 0


async def test_enhance_query_top_level_exception_isolated(monkeypatch):
    """LLMProvider.chat 외부에서 예상 못한 예외 → top-level except 로 fallback."""

    class BoomError(Exception):
        pass

    monkeypatch.setattr("app.pipeline.enhance_query.LLMProvider.chat", AsyncMock(side_effect=BoomError("boom")))

    state = _make_state()
    out = await enhance_query_step(state)

    # raise 되지 않고 fallback 으로 처리되어야 함.
    assert out.enhance_query_status == "fallback"
