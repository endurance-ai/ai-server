from app.models.request import RecommendRequest
from app.models.response import Candidate, RecommendResponse
from app.observability.langfuse import observe
from app.pipeline.diversify import diversify_step
from app.pipeline.embed import embed_step
from app.pipeline.search import search_step
from app.pipeline.state import PipelineState


@observe(name="recommend_pipeline")
async def run_pipeline(req: RecommendRequest) -> RecommendResponse:
    state = PipelineState(request=req)
    state = await embed_step(state)
    state = await search_step(state)
    state = await diversify_step(state)

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
            score=float(c.get("score", 0.0)),
            dense_rank=c.get("dense_rank"),
            sparse_rank=c.get("sparse_rank"),
        )
        for c in state.final_candidates
    ]

    return RecommendResponse(
        item_id=req.item.id,
        results=results,
        counts=state.counts,
        latency_ms=state.latency_ms,
    )
