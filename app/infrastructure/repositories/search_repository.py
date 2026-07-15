"""Search repository (SPEC-ARCH-AI-001 PR2, REQ-AI-002 + SPEC-SEARCH-V6-001).

Encapsulates the `search_products_v6` RPC call (migrated from the dropped
`search_products_v5` — see SPEC-SEARCH-V6-001). The hardcoded RPC name and
the full parameter-dict mapping live HERE in exactly one place. Business code
(search_service) no longer references the RPC name or constructs the param
dict.

v6 is embedding-first: `query_embedding` is the sole ranking signal (the RPC
already returns rows ordered by `distance` ASC). There is no text/sparse
param, no price param, no gender/subcategory param — those were dropped with
v5 + pgroonga + product_search_text. The characterization Net(3) param
snapshot is re-pointed to the NEW v6 6-key shape (the v5 byte-identity net
subject is legitimately retired with SPEC basis; the SAME safety intent — a
locked param snapshot — is preserved against v6).

Patch-seam preservation: the RPC is dispatched through DatabaseService
(SPEC-ARCH-AI-001 PR1 DI seam). DatabaseService resolves the
`DatabaseProvider` CLASS attribute at call time; the existing monkeypatch
seam (`app.pipeline.search.DatabaseProvider.rpc`) mutates that same shared
class object, so the characterization net and
tests/test_pipeline_with_enhance.py keep intercepting.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.infrastructure.repositories.category_family import to_canonical_family
from app.infrastructure.repositories.search_rpc_contract import validate_rpc_rows
from app.infrastructure.repositories.style_node import code_to_id as _style_code_to_id

logger = logging.getLogger(__name__)

# The single source of truth for the search RPC name (REQ-AI-002). v6 swaps
# the repository, not scattered string literals (SPEC-SEARCH-V6-001).
_RPC_NAME = "search_products_v6"


def embedding_to_pgvector(values: list[float]) -> str:
    """pgvector text input format: '[v1,v2,...]'."""
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


class SearchRepository:
    """Wraps the `search_products_v6` RPC. Sole owner of the RPC name +
    param mapping (REQ-AI-002, SPEC-SEARCH-V6-001)."""

    @staticmethod
    def build_params(
        *,
        embedding: list[float],
        brand_filter: list[str] | None,
        category: str | None = None,
        style_node_code: str | None = None,
        color_family: str | None = None,
        subcategory: str | None = None,
        gender: str | None = None,
    ) -> dict[str, Any]:
        """Construct the `search_products_v6` RPC param dict (SPEC-SEARCH-V6-001).

        Rationale for the exact key set:
          - v6 is embedding-first: `query_embedding` is the SOLE ranking
            signal (the RPC returns rows already ordered by `distance` ASC).
            No text/sparse param exists (v5 + pgroonga + product_search_text
            were dropped) → query_text retired.
          - `p_category` is the v6 FILTER 2 canonical FAMILY gate. The raw
            `category` (Vision value / app value / None) is normalized via
            `to_canonical_family` to exactly one of the 20 canonical lowercase
            tokens — `other` when there is no apparel match. This always
            satisfies the v6 contract ("`p_category` must be exactly one
            lowercase token") AND the intentional gate-skip behavior (`other`
            → family gate skipped → graceful cosine-only degrade). The
            normalization map is the single source in
            app/infrastructure/repositories/category_family.py (vision_prompt.py
            is the SPEC-VISION-UNIFY-001 frozen mirror — NOT edited).
          - `p_subcategory` (2026-07-15 활성화): products.subcategory 가
            백엔드 정규화로 60%+ 채워짐 (실 DB 확인 — "100% NULL" 전제 무효).
            RPC 는 EXACT 매치 + 어느 rung 에서도 완화하지 않으므로, 호출자
            (search_service)는 반드시 `subcategory_vocab.normalize_subcategory`
            를 통과한 canonical 토큰 또는 None 만 넘긴다 (여기서 재정규화
            하지 않음 — 이중 정규화 방지).
          - There is no v6 RPC price param and no client-side price filter
            (user-confirmed) → price_min/price_max retired.
          - `p_style_node_id` is the v6 RPC's FILTER 1 — a brand-level
            taxonomy filter (`brand_nodes.primary_style_node_id`). Callers
            supply the Vision-derived letter (`A`..`U`) via `style_node_code`;
            resolution to the bigint id is delegated to the single-source
            `style_node.code_to_id` cache. None / unknown letter → None →
            v6 takes its rung-2 `degraded` category-only fallback (the
            previous always-degraded baseline; never worse than today).
          - Brand narrowing is the only legit optional filter preserved →
            p_brand_names.
          - `p_color_family` is the v6 color gate (SPEC-SEARCH-V6-COLOR).
            Vision v2 emits 16 canonical family tokens (BLACK/WHITE/GREY/…);
            passing one narrows the RPC to `UPPER(products.color) = UPPER(...)`.
            None disables the filter. `products.color` is NOT NULL repo-wide,
            so unlike subcategory this is safe to hard-filter on.
        This mapping exists ONLY here (single source — REQ-AI-002).
        """
        return {
            "query_embedding": embedding_to_pgvector(embedding),
            "p_style_node_id": _style_code_to_id(style_node_code),
            # v6 family gate: always exactly one of the 20 canonical lowercase
            # tokens (`other` when no apparel match → gate intentionally
            # skipped). Single-source map: category_family.to_canonical_family.
            "p_category": to_canonical_family(category),
            # EXACT·무완화 필터 — canonical 토큰 또는 None (호출자가 정규화).
            "p_subcategory": subcategory,
            "p_brand_names": brand_filter,
            # Vision colorFamily → v6 color gate (SPEC-SEARCH-V6-COLOR). None
            # → filter off (backward-compatible with pre-color rollout).
            "p_color_family": color_family or None,
            # 2026-07-16 — 상품 레벨 gender 하드 필터. 'men'|'women' 만 유효
            # (unisex/미확인 → None = 필터 off — 호출자 search_service 가 매핑).
            # RPC: p.gender && ARRAY[p_gender,'unisex'] — unisex 상품 항상 포함.
            "p_gender": gender,
            "p_limit": settings.SEARCH_DEFAULT_K,
        }

    @staticmethod
    async def search(params: dict[str, Any]) -> list[dict[str, Any]]:
        """Invoke the `search_products_v6` RPC with the supplied params.

        Dispatched through the SPEC-mandated DatabaseService DI seam
        (review P1-b: previously DatabaseService was exported but never
        wired). DatabaseService resolves the DatabaseProvider class attribute
        at call time; the monkeypatch seam
        `app.pipeline.search.DatabaseProvider.rpc` mutates that same shared
        class object, so interception stays byte-identical.
        """
        from app.services.database_service import DatabaseService

        rows = await DatabaseService.rpc(_RPC_NAME, params)
        # REQ-AI-006: validate the RPC response shape BEFORE it flows into
        # scoring/diversify. validate_rpc_rows returns the ORIGINAL rows
        # untouched on success (no coercion) so the happy path is
        # byte-identical; it only adds a structured drift-error branch.
        return validate_rpc_rows(rows)
