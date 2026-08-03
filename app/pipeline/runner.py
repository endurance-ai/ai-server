import asyncio
import logging

from app.core.config import settings
from app.models.request import RecommendRequest
from app.models.response import Candidate, RecommendResponse
from app.observability.langfuse import observe
from app.pipeline.diversify import diversify_step
from app.pipeline.embed import embed_step
from app.pipeline.enhance_query import enhance_query_step
from app.pipeline.search import search_step
from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)


@observe(name="recommend_pipeline")
async def run_pipeline(req: RecommendRequest, *, user_key: str | None = None) -> RecommendResponse:
    # 요청 요약 — 어떤 item / 필터로 들어왔는지
    logger.info(
        "[STEP 4.2]🎨 [pipeline] === START === item_id=%s search_query=%r search_query_ko=%r "
        "subcategory=%s gender=%s brand_filter=%s tolerance=%.2f final_limit=%s",
        req.item.id,
        req.item.search_query,
        req.item.search_query_ko,
        req.item.subcategory,
        req.gender,
        req.brand_filter,
        req.tolerance,
        req.final_limit,
    )

    state = PipelineState(request=req, user_key=user_key)

    # @MX:WARN: [AUTO] embed_step 과 enhance_query_step 을 asyncio.gather 로 병렬 실행.
    # @MX:REASON: 두 단계는 동일 state 인스턴스를 공유하지만 갱신 필드가 분리(embedding vs enhanced_*)되어 race 없음.
    #             enhance_query_step 은 어떤 예외도 raise 하지 않도록 설계됨 — 방어적으로 return_exceptions=True 적용.
    if settings.PIPELINE_PARALLEL_ENABLED:
        results = await asyncio.gather(
            embed_step(state),
            enhance_query_step(state),
            return_exceptions=True,
        )
        embed_result = results[0]
        enhance_result = results[1]
        # embed 측 예외는 기존 동작대로 전파 (추천 응답 생성 불가).
        if isinstance(embed_result, BaseException):
            raise embed_result
        # enhance 측 예외는 폴백 — state 만 갱신하고 통과.
        if isinstance(enhance_result, BaseException):
            logger.warning("🎨 [pipeline] enhance_query_step 예외 격리 — %r", enhance_result)
            state.enhance_query_status = "fallback"
            state.enhanced_query = None
            state.enhanced_query_ko = None
    else:
        state = await embed_step(state)
        state = await enhance_query_step(state)

    state = await search_step(state)
    state = await diversify_step(state)

    # 최종 요약 — 단계별 latency / count
    logger.info(
        "[STEP 4.9]🎨 [pipeline] === END === counts=%s latency_ms=%s",
        state.counts,
        state.latency_ms,
    )

    # v6 rows carry `distance` (cosine, ASC=better) instead of score/ranks.
    # score = 1.0 - distance preserves the downstream "higher=better, RPC
    # order" semantics; absent distance → 1.0 → score 0.0 (SPEC-SEARCH-V6-001).
    results = [
        Candidate(
            id=str(c["id"]),
            brand=c.get("brand", ""),
            name=c.get("name", ""),
            price=c.get("price"),
            image_url=c.get("image_url"),
            product_url=c.get("product_url"),
            platform=c.get("platform"),
            subcategory=c.get("subcategory"),
            score=float(1.0 - c.get("distance", 1.0)),
            dense_rank=None,
            sparse_rank=None,
        )
        for c in state.final_candidates
    ]

    return RecommendResponse(
        item_id=req.item.id,
        results=results,
        counts=state.counts,
        latency_ms=state.latency_ms,
    )
