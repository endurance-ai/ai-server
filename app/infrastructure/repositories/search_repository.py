"""Search repository (SPEC-ARCH-AI-001 PR2, REQ-AI-002).

Encapsulates the `search_products_v5` RPC call. The hardcoded RPC name and
the full parameter-dict mapping now live HERE in exactly one place
(previously inline in app/pipeline/search.py:54, then app/services/
search_service.py). Business code (search_service) no longer references the
RPC name or constructs the param dict.

[HARD] Behavior byte-identical (REQ-AI-007): the param dict this builds and
the fn_name it passes are identical to the pre-extraction values; the
characterization Net(3) param snapshot is unchanged.

Patch-seam preservation: the RPC is dispatched through DatabaseService
(SPEC-ARCH-AI-001 PR1 DI seam, now wired -- review P1-b). DatabaseService
resolves the `SupabaseProvider` CLASS attribute at call time; the existing
monkeypatch seam (`app.pipeline.search.SupabaseProvider.rpc`) mutates that
same shared class object, so the characterization net and
tests/test_pipeline_with_enhance.py keep intercepting byte-identically.
PR3 (REQ-AI-003) introduces full DI; relocating the seam is out of scope.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.infrastructure.repositories.search_rpc_contract import validate_rpc_rows

logger = logging.getLogger(__name__)

# The single source of truth for the search RPC name (REQ-AI-002). v6 swaps
# the repository, not scattered string literals.
_RPC_NAME = "search_products_v5"


def embedding_to_pgvector(values: list[float]) -> str:
    """pgvector text input format: '[v1,v2,...]'."""
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


class SearchRepository:
    """Wraps the `search_products_v5` RPC. Sole owner of the RPC name +
    param mapping (REQ-AI-002)."""

    @staticmethod
    def build_params(
        *,
        embedding: list[float],
        query_text: str,
        brand_filter: list[str] | None,
        price_min: int | None,
        price_max: int | None,
    ) -> dict[str, Any]:
        """Construct the `search_products_v5` RPC param dict. This mapping
        exists ONLY here (byte-identical to the pre-PR2 inline dict)."""
        return {
            "query_embedding": embedding_to_pgvector(embedding),
            "query_text": query_text,
            "brand_filter": brand_filter,
            # DIAG (임시): gender 매핑 깨짐 ('male' vs DB 'men') + subcategory 100% NULL
            # → 두 hard filter 가 dense 후보 풀을 0으로 만들어 임베딩 검증 불가
            # 진단 끝나면 원복: "gender_filter": [req.gender] if req.gender else None
            #                  "subcategory_filter": req.item.subcategory
            "gender_filter": None,
            "subcategory_filter": None,
            "price_min": price_min,
            "price_max": price_max,
            "tags_filter": None,
            "k": settings.SEARCH_DEFAULT_K,
            "rrf_k": 60,
        }

    @staticmethod
    async def search(params: dict[str, Any]) -> list[dict[str, Any]]:
        """Invoke the `search_products_v5` RPC with the supplied params.

        Dispatched through the SPEC-mandated DatabaseService DI seam
        (review P1-b: previously DatabaseService was exported but never
        wired). DatabaseService resolves the SupabaseProvider class attribute
        at call time; the monkeypatch seam
        `app.pipeline.search.SupabaseProvider.rpc` mutates that same shared
        class object, so interception stays byte-identical.
        """
        from app.services.database_service import DatabaseService

        rows = await DatabaseService.rpc(_RPC_NAME, params)
        # REQ-AI-006: validate the RPC response shape BEFORE it flows into
        # scoring/diversify. validate_rpc_rows returns the ORIGINAL rows
        # untouched on success (no coercion) so the happy path is
        # byte-identical; it only adds a structured drift-error branch.
        return validate_rpc_rows(rows)
