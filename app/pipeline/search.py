import logging

from app.core.config import settings
from app.observability.langfuse import observe
from app.pipeline.state import PipelineState
from app.providers.database import SupabaseProvider

logger = logging.getLogger(__name__)


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
        # DIAG (임시): gender 매핑 깨짐 ('male' vs DB 'men') + subcategory 100% NULL
        # → 두 hard filter 가 dense 후보 풀을 0으로 만들어 임베딩 검증 불가
        # 진단 끝나면 원복: "gender_filter": [req.gender] if req.gender else None
        #                  "subcategory_filter": req.item.subcategory
        "gender_filter": None,
        "subcategory_filter": None,
        "price_min": req.price_filter.min_price if req.price_filter else None,
        "price_max": req.price_filter.max_price if req.price_filter else None,
        "tags_filter": None,
        "k": settings.SEARCH_DEFAULT_K,
        "rrf_k": 60,
    }

    # RPC 입력 파라미터 — query_embedding 은 길어서 dim 만
    diag_params = {k: v for k, v in params.items() if k != "query_embedding"}
    diag_params["query_embedding_dim"] = len(state.embedding)
    logger.info("[STEP 4.5][search] Supabase RPC 호출 시작 — fn=search_products_v5 params=%s", diag_params)

    rows = await SupabaseProvider.rpc("search_products_v5", params)
    state.raw_candidates = rows
    state.counts["raw"] = len(rows)

    # 결과 분포 — top-5 점수 / dense_rank / sparse_rank / 브랜드/플랫폼/subcategory
    if rows:
        logger.info("[STEP 4.6][search] RPC 응답 — raw_count=%d (dense+sparse RRF top-K)", len(rows))
        for i, r in enumerate(rows[:5]):
            logger.info(
                "[STEP 4.6][search]   #%d id=%s score=%.4f dense_rank=%s sparse_rank=%s "
                "brand=%s platform=%s subcat=%s name=%r",
                i + 1,
                r.get("id"),
                float(r.get("score", 0.0)),
                r.get("dense_rank"),
                r.get("sparse_rank"),
                r.get("brand"),
                r.get("platform"),
                r.get("subcategory"),
                (r.get("name") or "")[:60],
            )
        # 점수 통계 — min/max/median
        scores = [float(r.get("score", 0.0)) for r in rows]
        scores_sorted = sorted(scores, reverse=True)
        logger.info(
            "[STEP 4.6][search] score_dist top=%.4f median=%.4f bottom=%.4f",
            scores_sorted[0],
            scores_sorted[len(scores_sorted) // 2],
            scores_sorted[-1],
        )
        # dense/sparse 어느 path 에서 들어왔는지
        dense_only = sum(1 for r in rows if r.get("dense_rank") is not None and r.get("sparse_rank") is None)
        sparse_only = sum(1 for r in rows if r.get("dense_rank") is None and r.get("sparse_rank") is not None)
        both = sum(1 for r in rows if r.get("dense_rank") is not None and r.get("sparse_rank") is not None)
        logger.info(
            "[STEP 4.6][search] path_breakdown dense_only=%d sparse_only=%d both=%d",
            dense_only,
            sparse_only,
            both,
        )
    else:
        logger.warning("[STEP 4.6][search] ⚠️ raw_count=0 — 0결과! hard filter (subcategory/gender 등) 너무 빡셈")

    state.end("search")
    return state
