"""LLM 기반 sparse 검색 쿼리 정제 단계 (SPEC-PIPELINE-001).

raw search_query / search_query_ko 를 pgroonga BM25 에 적합한 형태로 정제한다.
모든 실패 모드에서 raw 쿼리로 폴백하며, 어떤 예외도 호출자에 전파하지 않는다.

# @MX:NOTE: [AUTO] 폴백 정책 — feature flag/빈 입력/타임아웃/HTTP/빈 응답/JSON 파싱/길이(1~200)
# 위반 모두 status='fallback' 또는 'disabled'/'skipped' 로 처리하여 raw 쿼리 사용. 절대 raise 금지.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.observability.langfuse import observe
from app.pipeline.state import PipelineState
from app.providers.llm import LLMProvider

logger = logging.getLogger(__name__)

# 응답 정규식 폴백 — JSON 파싱 실패 시 평문에서 {"refined_ko":"...", "refined_en":"..."} 추출
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT = (
    "You normalize Korean and English fashion search queries for BM25/pgroonga sparse retrieval.\n"
    'Output JSON only with this exact shape: {"refined_ko": "<korean>", "refined_en": "<english>"}.\n'
    "Rules:\n"
    "- Keep core nouns (item type, color, material, silhouette, brand, category).\n"
    "- Drop filler words, decorative adjectives, sentence connectors, emojis.\n"
    "- Do not invent attributes not in the input.\n"
    "- Each refined string: 1 to 200 characters. No commentary outside JSON.\n"
    "- Return both languages. If one side is empty, infer from the other."
)

_MIN_LEN = 1
_MAX_LEN = 200


def _build_messages(req_item: Any) -> list[dict[str, Any]]:
    """프롬프트 메시지 구성. portal/app 이 전달한 메타를 그대로 직렬화."""
    user_payload = {
        "raw_ko": req_item.search_query_ko or "",
        "raw_en": req_item.search_query or "",
        "subcategory": req_item.subcategory or "",
        "category": getattr(req_item, "category", "") or "",
        # attributes 는 현재 schema 에 없음 — 향후 확장 대비. 안전 getattr.
        "attributes": getattr(req_item, "attributes", None),
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def _parse_response(content: str) -> tuple[str, str] | None:
    """LLM 응답에서 (refined_ko, refined_en) 추출. 실패 시 None."""
    if not content or not content.strip():
        return None
    # 1차: 직접 JSON 파싱
    parsed: dict[str, Any] | None = None
    try:
        candidate = json.loads(content)
        if isinstance(candidate, dict):
            parsed = candidate
    except json.JSONDecodeError:
        parsed = None
    # 2차: 정규식으로 JSON 블록 추출
    if parsed is None:
        match = _JSON_BLOCK_RE.search(content)
        if match is None:
            return None
        try:
            candidate = json.loads(match.group(0))
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            return None
    if parsed is None:
        return None
    refined_ko = parsed.get("refined_ko")
    refined_en = parsed.get("refined_en")
    if not isinstance(refined_ko, str) or not isinstance(refined_en, str):
        return None
    return refined_ko, refined_en


def _validate_lengths(refined_ko: str, refined_en: str) -> bool:
    """정제 쿼리 길이 검증. 1~200자 범위."""
    return _MIN_LEN <= len(refined_ko) <= _MAX_LEN and _MIN_LEN <= len(refined_en) <= _MAX_LEN


def _set_fallback(state: PipelineState, reason: str) -> None:
    """폴백 상태 기록 + 구조화 warning 로그."""
    state.enhance_query_status = "fallback"
    state.enhanced_query = None
    state.enhanced_query_ko = None
    logger.warning(
        "[enhance_query] fallback — reason=%s model=%s",
        reason,
        settings.ENHANCE_QUERY_MODEL,
    )


# @MX:ANCHOR: [AUTO] enhance_query 파이프라인 진입점 — runner.py 에서 embed_step 과 병렬 실행.
# @MX:REASON: 단일 진입점. fan_in >= 1 (현재 runner.py), 향후 다른 파이프라인 변형에서도 진입 가능.
@observe(name="pipeline.enhance_query")
async def enhance_query_step(state: PipelineState) -> PipelineState:
    """LLM 호출로 search_query / search_query_ko 를 정제한다.

    설계 원칙 [HARD]: 어떤 예외도 호출자에 전파하지 않는다. 모든 실패 → status 갱신 후 정상 반환.
    """
    state.start("enhance_query")
    try:
        item = state.request.item
        raw_ko = (item.search_query_ko or "").strip()
        raw_en = (item.search_query or "").strip()

        # feature flag — 비활성 시 LLM 호출 0회
        if not settings.ENHANCE_QUERY_ENABLED:
            state.enhance_query_status = "disabled"
            state.enhanced_query = None
            state.enhanced_query_ko = None
            return state

        # 입력 비어있음 — LLM 호출 0회
        if not raw_ko and not raw_en:
            state.enhance_query_status = "skipped"
            state.enhanced_query = None
            state.enhanced_query_ko = None
            return state

        # LLM 호출 (timeout 가드)
        timeout_s = max(0.001, settings.ENHANCE_QUERY_TIMEOUT_MS / 1000.0)
        messages = _build_messages(item)
        try:
            response = await asyncio.wait_for(
                LLMProvider.chat(
                    model=settings.ENHANCE_QUERY_MODEL,
                    messages=messages,
                    temperature=settings.ENHANCE_QUERY_TEMPERATURE,
                    max_tokens=settings.ENHANCE_QUERY_MAX_TOKENS,
                    response_format={"type": "json_object"},
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            _set_fallback(state, "timeout")
            return state
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            reason = "http_5xx" if status_code >= 500 else "http_4xx"
            _set_fallback(state, reason)
            return state
        except httpx.RequestError:
            _set_fallback(state, "network")
            return state
        except Exception:
            # 알 수 없는 LLM 측 예외 — 절대 raise 하지 않음.
            _set_fallback(state, "unknown")
            return state

        # 응답 추출
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            _set_fallback(state, "empty")
            return state

        if not isinstance(content, str) or not content.strip():
            _set_fallback(state, "empty")
            return state

        parsed = _parse_response(content)
        if parsed is None:
            _set_fallback(state, "parse_error")
            return state

        refined_ko, refined_en = parsed
        if not _validate_lengths(refined_ko, refined_en):
            _set_fallback(state, "length_invalid")
            return state

        # 정상 — 정제 쿼리 채택
        state.enhanced_query_ko = refined_ko
        state.enhanced_query = refined_en
        state.enhance_query_status = "ok"
        logger.info(
            "[enhance_query] ok — refined_ko=%r refined_en=%r model=%s",
            refined_ko[:60],
            refined_en[:60],
            settings.ENHANCE_QUERY_MODEL,
        )
        return state
    except Exception:
        # top-level 가드 — 어떤 예외도 호출자 전파 금지.
        _set_fallback(state, "unknown")
        return state
    finally:
        state.end("enhance_query")
