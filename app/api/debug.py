"""Admin Debug endpoints — step-by-step v6 pipeline tracing.

kikoai/app (Next.js) 의 어드민 검색 디버거가 호출한다. ReAct LLM 의 텍스트 정제
단계와 Vision 분석 단계를 각각 들여다볼 수 있게 raw input/output/latency 를
그대로 반환한다.

production code 와 동일한 코드 경로를 import 해서 재사용 — 다른 세션이
react_loop.py / vision.py 를 수정 중이라 single source of truth 유지가 핵심.

Auth: 표준 `verify_internal_token` 의존성. dev 환경 (INTERNAL_API_TOKEN 미설정)
에선 통과.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import verify_internal_token
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])

# 모델 화이트리스트 (Haiku / Nova Lite 비교 가능)
# LiteLLM proxy 의 canonical model_name 만 (litellm-config.yaml 기준).
# 별칭/full ARN 받지 않음 — proxy 가 reject 함.
ALLOWED_REWRITE_MODELS = {
    "claude-haiku-4-5",
    "nova-lite",
    "gpt-4o-mini",
}


# ──────────────────────────────────────────────────────────────────────
# 1) /debug/rewrite-query
# ──────────────────────────────────────────────────────────────────────


class RewriteRequest(BaseModel):
    user_text: str = Field(..., min_length=1, max_length=2000)
    model_id: str | None = None
    include_system_prompt: bool = True


class RewriteResponse(BaseModel):
    ok: bool
    model_used: str
    latency_ms: int
    system_prompt: str | None = None
    user_message: str
    raw_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    parsed_text_query: str | None = None
    parsed_tool_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    raw_content: str | None = None
    error: str | None = None


@router.post(
    "/rewrite-query",
    response_model=RewriteResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def rewrite_query(req: RewriteRequest) -> RewriteResponse:
    chosen_model = (req.model_id or settings.AGENT_LLM_MODEL or "").strip()
    if not chosen_model:
        raise HTTPException(status_code=400, detail="model_id missing")
    if chosen_model not in ALLOWED_REWRITE_MODELS:
        raise HTTPException(status_code=400, detail=f"model '{chosen_model}' not whitelisted")

    try:
        from app.agents.llm_client import _build_tools_schema
        from app.agents.react_loop import _SYSTEM_PROMPT
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"react_loop import: {e}") from e

    try:
        from app.providers.litellm_chat import LiteLLMChatOpenAI
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"langchain import: {e}") from e

    api_key = settings.LITELLM_MASTER_KEY or "missing-litellm-master-key"
    client = LiteLLMChatOpenAI(
        model=chosen_model,
        base_url=settings.LITELLM_BASE_URL + "/v1",
        api_key=api_key,
        temperature=0.4,
        timeout=max(0.1, settings.AGENT_LLM_TIMEOUT_S),
        extra_body={"drop_params": True},
    )
    llm = client.bind_tools(_build_tools_schema(), tool_choice=None)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": req.user_text},
    ]

    t0 = time.perf_counter()
    try:
        ai_msg = await llm.ainvoke(messages)
        try:
            from app.observability.turn_cost import accumulate_lc

            usage = getattr(ai_msg, "usage_metadata", None)
            if isinstance(usage, dict):
                accumulate_lc(
                    chosen_model,
                    usage,
                    response_metadata=getattr(ai_msg, "response_metadata", None),
                    source="debug_rewrite",
                )
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.warning("[debug.rewrite] LLM failed: %r", exc)
        return RewriteResponse(
            ok=False,
            model_used=chosen_model,
            latency_ms=elapsed,
            system_prompt=_SYSTEM_PROMPT if req.include_system_prompt else None,
            user_message=req.user_text,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = int((time.perf_counter() - t0) * 1000)

    tool_calls = getattr(ai_msg, "tool_calls", None) or []
    raw_tool_calls = []
    parsed_text_query: str | None = None
    parsed_tool_name: str | None = None
    for tc in tool_calls:
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
        args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        raw_tool_calls.append({"name": name, "args": args, "id": tc_id})
        if not parsed_text_query and isinstance(args, dict):
            tq = args.get("text_query")
            if isinstance(tq, str) and tq.strip():
                parsed_text_query = tq.strip()
                parsed_tool_name = name

    raw_content = getattr(ai_msg, "content", None)
    if isinstance(raw_content, list):
        raw_content = " ".join((p.get("text") if isinstance(p, dict) else str(p)) for p in raw_content)

    usage = getattr(ai_msg, "usage_metadata", None) or {}
    response_meta = getattr(ai_msg, "response_metadata", None) or {}
    finish_reason = response_meta.get("finish_reason") if isinstance(response_meta, dict) else None

    return RewriteResponse(
        ok=True,
        model_used=chosen_model,
        latency_ms=elapsed,
        system_prompt=_SYSTEM_PROMPT if req.include_system_prompt else None,
        user_message=req.user_text,
        raw_tool_calls=raw_tool_calls,
        parsed_text_query=parsed_text_query,
        parsed_tool_name=parsed_tool_name,
        prompt_tokens=usage.get("input_tokens") if isinstance(usage, dict) else None,
        completion_tokens=usage.get("output_tokens") if isinstance(usage, dict) else None,
        finish_reason=finish_reason,
        raw_content=raw_content if isinstance(raw_content, str) else None,
    )


# ──────────────────────────────────────────────────────────────────────
# 2) /debug/vision-analyze
# ──────────────────────────────────────────────────────────────────────


class VisionRequest(BaseModel):
    image_url: str = Field(..., min_length=4)


class VisionItemTrace(BaseModel):
    label_ko: str | None = None
    category: str | None = None
    subcategory: str | None = None
    fit: str | None = None
    color_family: str | None = None
    detail: str | None = None
    keywords_en: list[str] = Field(default_factory=list)
    search_query: str | None = None
    confidence: float | None = None


class VisionResponse(BaseModel):
    ok: bool
    model_used: str
    latency_ms: int
    image_url: str
    items: list[VisionItemTrace] = Field(default_factory=list)
    picked_item_index: int | None = None
    mood_tags: list[str] = Field(default_factory=list)
    style_node_primary: str | None = None
    style_node_secondary: str | None = None
    error: str | None = None


@router.post(
    "/vision-analyze",
    response_model=VisionResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def vision_analyze(req: VisionRequest) -> VisionResponse:
    try:
        from app.channels.schemas import _ssrf_guard_url
        from app.channels.vision import _model as _vision_model
        from app.channels.vision import extract as vision_extract
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"vision import: {e}") from e

    # SSRF guard (2026-05-20) — admin-auth-gated endpoint still must not be
    # weaponizable as a probe vector for internal IPs (169.254.169.254 metadata,
    # 172.31.x EC2 private range, etc.).
    try:
        _ssrf_guard_url(req.image_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"ssrf_blocked: {exc}") from exc

    model_used = _vision_model()

    t0 = time.perf_counter()
    try:
        result = await vision_extract(req.image_url)
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.warning("[debug.vision] extract failed: %r", exc)
        return VisionResponse(
            ok=False,
            model_used=model_used,
            latency_ms=elapsed,
            image_url=req.image_url,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = int((time.perf_counter() - t0) * 1000)

    items: list[VisionItemTrace] = []
    for it in getattr(result, "items", []) or []:
        items.append(
            VisionItemTrace(
                label_ko=getattr(it, "label_ko", None),
                category=getattr(it, "category", None),
                subcategory=getattr(it, "subcategory", None),
                fit=getattr(it, "fit", None),
                color_family=getattr(it, "color_family", None),
                detail=getattr(it, "detail", None),
                keywords_en=list(getattr(it, "keywords_en", []) or []),
                search_query=getattr(it, "search_query", None),
                confidence=getattr(it, "confidence", None),
            )
        )

    picked_idx: int | None = getattr(result, "picked_index", None)
    if picked_idx is None and items:
        picked_idx = 0

    mood = getattr(result, "mood", None)
    mood_tags: list[str] = []
    if mood is not None:
        raw_tags = getattr(mood, "tags", None) or []
        for t in raw_tags:
            label = getattr(t, "label", None) if not isinstance(t, str) else t
            if label:
                mood_tags.append(label)

    style_node = getattr(result, "style_node", None)
    sn_primary = getattr(style_node, "primary", None) if style_node else None
    sn_secondary = getattr(style_node, "secondary", None) if style_node else None

    return VisionResponse(
        ok=True,
        model_used=model_used,
        latency_ms=elapsed,
        image_url=req.image_url,
        items=items,
        picked_item_index=picked_idx,
        mood_tags=mood_tags,
        style_node_primary=sn_primary,
        style_node_secondary=sn_secondary,
    )


# ──────────────────────────────────────────────────────────────────────
# 3) /debug/models
# ──────────────────────────────────────────────────────────────────────


@router.get("/models", dependencies=[Depends(verify_internal_token)])
async def list_models() -> dict[str, Any]:
    return {
        "rewrite_models": sorted(ALLOWED_REWRITE_MODELS),
        "default_rewrite_model": settings.AGENT_LLM_MODEL or None,
        "vision_model": settings.VISION_MODEL,
    }


# ──────────────────────────────────────────────────────────────────────
# 4) /debug/resolve-url — IG/Pinterest URL → 실제 이미지 URL 리스트
# ──────────────────────────────────────────────────────────────────────


class ResolveRequest(BaseModel):
    url: str = Field(..., min_length=4)


class ResolveResponse(BaseModel):
    ok: bool
    source_url: str
    detected_kind: str  # "instagram" / "pinterest" / "other"
    latency_ms: int
    images: list[str] = Field(default_factory=list)
    error: str | None = None


class EditorialCandidatesRequest(BaseModel):
    concept: str = Field(..., min_length=2, max_length=500)
    gender: str = Field(default="women", pattern="^(women|men|unisex)$")
    limit: int = Field(default=30, ge=6, le=48)


@router.post(
    "/editorial-candidates",
    dependencies=[Depends(verify_internal_token)],
)
async def editorial_candidates(req: EditorialCandidatesRequest) -> Any:
    """Natural-language concept → multi-query recall → vision quality review."""
    from app.services.editorial_candidates import generate_editorial_candidates

    return await generate_editorial_candidates(
        concept=req.concept.strip(),
        gender=req.gender,
        limit=req.limit,
    )


@router.post(
    "/resolve-url",
    response_model=ResolveResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def resolve_url(req: ResolveRequest) -> ResolveResponse:
    """Resolve IG/Pinterest/일반 URL → 이미지 URL 리스트.

    production 봇과 동일한 `app.channels.link_resolver.resolve()` 재사용.
    - IG URL → Apify (instagram_apify.fetch_post_images) 로 슬라이드 이미지 전체
    - Pinterest URL → og:image 추출 + 풀해상도 치환
    - 일반 URL → og:image / fetch
    """
    from urllib.parse import urlparse

    try:
        from app.channels.link_resolver import resolve as link_resolve
        from app.channels.schemas import _ssrf_guard_url
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"link_resolver import: {e}") from e

    host = (urlparse(req.url).hostname or "").lower()
    if host == "pin.it" or host.endswith("pinterest.com"):
        kind = "pinterest"
    elif "instagram.com" in host:
        kind = "instagram"
    else:
        kind = "other"

    # SSRF guard (2026-05-20) — link_resolver only guards intermediate og:image
    # extractions, NOT the initial fetch of req.url. Without this pre-flight,
    # an authed admin can probe internal endpoints (EC2 metadata, K8s control
    # plane). Defense in depth even on auth-gated routes.
    try:
        _ssrf_guard_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"ssrf_blocked: {exc}") from exc

    t0 = time.perf_counter()
    try:
        images = await link_resolve(req.url)
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.perf_counter() - t0) * 1000)
        return ResolveResponse(
            ok=False,
            source_url=req.url,
            detected_kind=kind,
            latency_ms=elapsed,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = int((time.perf_counter() - t0) * 1000)

    return ResolveResponse(
        ok=True,
        source_url=req.url,
        detected_kind=kind,
        latency_ms=elapsed,
        images=list(images or []),
    )


# ──────────────────────────────────────────────────────────────────────
# 5) /debug/cap — 일별 토큰 캡 + 등급 관리
# ──────────────────────────────────────────────────────────────────────

_TIER_CAPS_LABEL = {
    "free": "CAP_TIER_FREE",
    "standard": "CAP_TIER_STANDARD",
    "pro": "CAP_TIER_PRO",
    "developer": "unlimited",
}


class CapStatusResponse(BaseModel):
    chat_id: int
    tier: str
    daily_cap: int  # 0 = unlimited
    used_today: int
    remaining: int  # -1 = unlimited
    cap_enabled: bool
    over_limit: bool
    seconds_until_reset: int


class SetTierRequest(BaseModel):
    chat_id: int
    tier: str = Field(..., pattern="^(free|standard|pro|developer)$")


class SetTierResponse(BaseModel):
    ok: bool
    chat_id: int
    tier: str
    daily_cap: int


class ResetCapResponse(BaseModel):
    ok: bool
    chat_id: int


@router.get(
    "/cap/status/{chat_id}",
    response_model=CapStatusResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def cap_status(chat_id: int) -> CapStatusResponse:
    """유저 토큰 사용량 + 등급 + 잔여량 조회."""
    from app.infrastructure.cache.token_cap import (
        _seconds_until_kst_midnight,
        _tier_cap,
        get_usage,
        get_user_tier,
        is_over_limit,
    )

    tier = await get_user_tier(chat_id)
    cap = _tier_cap(tier)
    used = await get_usage(chat_id)
    over = await is_over_limit(chat_id)
    remaining = -1 if cap == 0 else max(0, cap - used)

    return CapStatusResponse(
        chat_id=chat_id,
        tier=tier,
        daily_cap=cap,
        used_today=used,
        remaining=remaining,
        cap_enabled=settings.DAILY_TOKEN_CAP_ENABLED,
        over_limit=over,
        seconds_until_reset=_seconds_until_kst_midnight(),
    )


@router.post(
    "/cap/tier",
    response_model=SetTierResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def set_tier(req: SetTierRequest) -> SetTierResponse:
    """유저 등급 설정. developer 등급은 캡 무제한."""
    from app.infrastructure.cache.token_cap import _tier_cap, set_user_tier

    ok = await set_user_tier(req.chat_id, req.tier)  # type: ignore[arg-type]
    if not ok:
        raise HTTPException(status_code=503, detail="redis unavailable")

    cap = _tier_cap(req.tier)
    logger.info("[debug.cap] set_tier chat=%s tier=%s cap=%s", req.chat_id, req.tier, cap or "unlimited")
    return SetTierResponse(ok=True, chat_id=req.chat_id, tier=req.tier, daily_cap=cap)


@router.delete(
    "/cap/reset/{chat_id}",
    response_model=ResetCapResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def reset_cap(chat_id: int) -> ResetCapResponse:
    """유저 일별 카운터 초기화 (테스트 / 수동 복구용)."""
    from app.infrastructure.cache.token_cap import reset_usage

    ok = await reset_usage(chat_id)
    logger.info("[debug.cap] reset_usage chat=%s ok=%s", chat_id, ok)
    return ResetCapResponse(ok=ok, chat_id=chat_id)
