from app.core.config import settings
from app.observability.langfuse import observe
from app.pipeline.state import PipelineState
from app.providers.database import SupabaseProvider


def _embedding_to_pgvector(values: list[float]) -> str:
    """pgvector text input format: '[v1,v2,...]'."""
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


@observe(name="pipeline.search")
async def search_step(state: PipelineState) -> PipelineState:
    if state.embedding is None:
        raise RuntimeError("search_step requires state.embedding (call embed_step first)")

    req = state.request
    state.start("search")

    params = {
        "query_embedding": _embedding_to_pgvector(state.embedding),
        "query_text": req.item.search_query_ko or req.item.search_query,
        "brand_filter": req.brand_filter,
        "gender_filter": [req.gender] if req.gender else None,
        "subcategory_filter": req.item.subcategory,
        "price_min": req.price_filter.min_price if req.price_filter else None,
        "price_max": req.price_filter.max_price if req.price_filter else None,
        "tags_filter": None,
        "k": settings.SEARCH_DEFAULT_K,
        "rrf_k": 60,
    }

    rows = await SupabaseProvider.rpc("search_products_v5", params)
    state.raw_candidates = rows
    state.counts["raw"] = len(rows)
    state.end("search")
    return state
