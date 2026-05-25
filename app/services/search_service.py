"""Search service (SPEC-ARCH-AI-001 PR1 + PR2; v6-migrated by SPEC-SEARCH-V6-001).

Search orchestration. PR2 (REQ-AI-002): the RPC name and param-dict
construction live in app/infrastructure/repositories/search_repository.py —
this service delegates both to SearchRepository.

v6 embedding-first → query_text/enhance_query RPC path retired (module
retained, dormant): enhance_query_step (SPEC-PIPELINE-001, flag-off by
default) still runs in the pipeline but its output is no longer consumed for
RPC params — search_products_v6 has no text param.

The actual RPC invocation goes through the
app.pipeline.search.DatabaseProvider.rpc monkeypatch seam (dispatched inside
SearchRepository).
"""

import logging

from app.infrastructure.repositories.category_family import to_canonical_family
from app.infrastructure.repositories.search_repository import SearchRepository
from app.infrastructure.repositories.search_repository import (
    embedding_to_pgvector as _embedding_to_pgvector,
)

# search_rpc_contract imports only pydantic/typing (no app.* back-edge) so a
# module-top import is cycle-free -- no lazy/seam pattern needed here.
from app.infrastructure.repositories.search_rpc_contract import RpcContractError
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

    # v6 embedding-first → query_text/enhance_query RPC path retired (module
    # retained, dormant). search_products_v6 has no text param: query_embedding
    # is the sole ranking signal, so enhance_query_step's output is no longer
    # consumed here (the step still runs in the pipeline, just not fed to RPC).

    # REQ-AI-002: param mapping + RPC name owned solely by SearchRepository.
    # SPEC-SEARCH-V6-001 family gate: pass the real Vision/app item category;
    # SearchRepository normalizes it to one of the 20 canonical tokens.
    params = SearchRepository.build_params(
        embedding=state.embedding,
        brand_filter=req.brand_filter,
        category=req.item.category,
    )

    # Family-gate verification hook (SPEC-SEARCH-V6-001). Distinct from v6's
    # OWN post-RPC node-presence `degraded` (logged separately below): this
    # line surfaces the category-miss → `other` gate-skip BEFORE the RPC so
    # "category-miss other-skip" is never conflated with node-presence
    # `degraded`. raw=Vision/app value, canonical=resolved 20-token.
    canonical = to_canonical_family(req.item.category)
    logger.info(
        "[STEP 4.5][search] category raw=%r → canonical=%r family_gate=%s",
        req.item.category,
        canonical,
        "active" if canonical != "other" else "skipped(other)",
    )

    # RPC 입력 파라미터 — query_embedding 은 길어서 dim 만
    diag_params = {k: v for k, v in params.items() if k != "query_embedding"}
    diag_params["query_embedding_dim"] = len(state.embedding)
    logger.info("[STEP 4.5][search] DB RPC 호출 시작 — fn=search_products_v6 params=%s", diag_params)

    # REQ-AI-006: SearchRepository.search raises RpcContractError when the RPC
    # returns a row violating the documented contract. Catch it at the SERVICE
    # boundary (the contract-raise stays at the repository so the
    # validate_rpc_rows unit assertions remain valid). We log ONE structured
    # ERROR line with ONLY the row index + exception class name -- explicitly
    # NOT str(exc)/the pydantic detail/row values (no row content in logs or
    # response: avoids info disclosure). Then fail OPEN: rows=[] and continue
    # the normal empty-result path (matches the codebase's existing
    # raw_count=0 resilience pattern; persistent drift no longer 502s/DoSes).
    # The drift is still surfaced (REQ-AI-006 "surface, not silent") via this
    # ERROR log + the Langfuse trace, without leaking row content.
    try:
        rows = await SearchRepository.search(params)
    except RpcContractError as exc:
        logger.error(
            "[STEP 4.5][search] RPC contract drift -- failing open to empty result (row_index=%s exc=%s)",
            exc.row_index,
            type(exc).__name__,
        )
        rows = []
    state.raw_candidates = rows
    state.counts["raw"] = len(rows)

    # 결과 분포 — top-5 distance / degraded / 브랜드/플랫폼/subcategory (v6:
    # RPC 가 distance ASC 로 이미 정렬해 반환 — min=best).
    if rows:
        logger.info("[STEP 4.6][search] RPC 응답 — raw_count=%d (v6 embedding-first, distance ASC)", len(rows))
        for i, r in enumerate(rows[:5]):
            logger.info(
                "[STEP 4.6][search]   #%d id=%s distance=%.4f degraded=%s brand=%s platform=%s subcat=%s name=%r",
                i + 1,
                r.get("id"),
                float(r.get("distance", 1.0)),
                r.get("degraded"),
                r.get("brand"),
                r.get("platform"),
                r.get("subcategory"),
                (r.get("name") or "")[:60],
            )
        # distance 통계 — min/median/max (ASC 정렬이므로 min=best)
        distances = [float(r.get("distance", 1.0)) for r in rows]
        distances_sorted = sorted(distances)
        logger.info(
            "[STEP 4.6][search] distance_dist min=%.4f median=%.4f max=%.4f (ASC → min=best)",
            distances_sorted[0],
            distances_sorted[len(distances_sorted) // 2],
            distances_sorted[-1],
        )
        # degraded(스타일노드 필터 드롭 → 카테고리-only 폴백) 행 수 — 관측용
        degraded_count = sum(1 for r in rows if r.get("degraded"))
        logger.info("[STEP 4.6][search] degraded_count=%d (style-node filter dropped → category-only)", degraded_count)
    else:
        logger.warning("[STEP 4.6][search] ⚠️ raw_count=0 — 0결과! (v6 embedding-first 검색 풀 비어있음)")

    state.end("search")
    return state
