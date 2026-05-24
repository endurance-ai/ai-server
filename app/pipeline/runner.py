import logging

from app.models.request import RecommendRequest
from app.models.response import Candidate, RecommendResponse
from app.observability.langfuse import observe
from app.pipeline.diversify import diversify_step
from app.pipeline.embed import embed_step
from app.pipeline.search import search_step
from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)


@observe(name="recommend_pipeline")
async def run_pipeline(req: RecommendRequest) -> RecommendResponse:
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

    state = PipelineState(request=req)

    state = await embed_step(state)
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
        )
        for c in state.final_candidates
    ]

    return RecommendResponse(
        item_id=req.item.id,
        results=results,
        counts=state.counts,
        latency_ms=state.latency_ms,
    )
