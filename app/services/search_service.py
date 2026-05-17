"""Search service (SPEC-ARCH-AI-001 PR1 + PR2).

Search orchestration + query-text selection. PR2 (REQ-AI-002): the RPC name
and param-dict construction moved OUT of this service into
app/infrastructure/repositories/search_repository.py — this service no longer
references "search_products_v5" or builds the param dict; it delegates both
to SearchRepository. The diagnostic log lines below are byte-identical to the
pre-PR2 text (REQ-AI-007) so no characterization assertion shifts.

The actual RPC invocation still goes through the
app.pipeline.search.SupabaseProvider.rpc monkeypatch seam (now dispatched
inside SearchRepository), with the identical (fn_name, params) tuple; the
Net(3) param snapshot is unchanged. PR3 (REQ-AI-003) introduces DI.
"""

import logging

from app.infrastructure.repositories.search_repository import SearchRepository
from app.infrastructure.repositories.search_repository import (
    embedding_to_pgvector as _embedding_to_pgvector,
)
from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)

# Back-compat re-export: app/pipeline/search.py shim re-exports this name and
# tests reference _embedding_to_pgvector. The implementation now lives in the
# repository (single source for the pgvector format alongside the param map).
embedding_to_pgvector = _embedding_to_pgvector


async def search_service(state: PipelineState) -> PipelineState:
    if state.embedding is None:
        raise RuntimeError("search_step requires state.embedding (call embed_step first)")

    req = state.request
    state.start("search")

    # enhance_query (SPEC-PIPELINE-001): status=="ok" 일 때만 정제 쿼리 사용. 그 외 raw.
    if state.enhance_query_status == "ok" and state.enhanced_query_ko:
        query_text = state.enhanced_query_ko
    elif state.enhance_query_status == "ok" and state.enhanced_query:
        query_text = state.enhanced_query
    else:
        query_text = req.item.search_query_ko or req.item.search_query

    # REQ-AI-002: param mapping + RPC name owned solely by SearchRepository.
    params = SearchRepository.build_params(
        embedding=state.embedding,
        query_text=query_text,
        brand_filter=req.brand_filter,
        price_min=req.price_filter.min_price if req.price_filter else None,
        price_max=req.price_filter.max_price if req.price_filter else None,
    )

    # RPC 입력 파라미터 — query_embedding 은 길어서 dim 만
    diag_params = {k: v for k, v in params.items() if k != "query_embedding"}
    diag_params["query_embedding_dim"] = len(state.embedding)
    logger.info("[STEP 4.5][search] Supabase RPC 호출 시작 — fn=search_products_v5 params=%s", diag_params)

    rows = await SearchRepository.search(params)
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
