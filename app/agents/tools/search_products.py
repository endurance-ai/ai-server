"""SPEC-AGENT-V2-REACT / T-003b — `search_products` tool wrapper.

Thin wrapper around `app.pipeline.runner.run_pipeline`. Translates LLM-friendly
flat args into the existing `RecommendRequest` shape.

@MX:NOTE: [AUTO] Side effect: DB RPC + Modal embedding call.
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import SearchProductsResult

logger = logging.getLogger(__name__)


def _candidate_to_dict(cand: Any) -> dict[str, Any]:
    """Best-effort serialization of a Candidate to a small LLM-consumable dict."""
    try:
        if hasattr(cand, "model_dump"):
            d = cand.model_dump()
        elif isinstance(cand, dict):
            d = dict(cand)
        else:
            d = {k: getattr(cand, k, None) for k in ("product_id", "brand", "title", "price", "image_url")}
    except Exception:  # noqa: BLE001
        d = {}
    return {
        "product_id": d.get("product_id") or d.get("id"),
        "brand": d.get("brand"),
        "title": (d.get("title") or "")[:80],
        "price": d.get("price"),
    }


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> SearchProductsResult:
    text_query = (args.get("text_query") or "").strip()
    if not text_query:
        return SearchProductsResult(ok=False, error="missing_text_query", candidates_count=0, top_candidates=[])

    # Lazy import — avoids loading pipeline stack at registry import time.
    try:
        from app.models.request import AnalyzedItem, RecommendRequest
        from app.pipeline.runner import run_pipeline
    except Exception as exc:  # noqa: BLE001
        return SearchProductsResult(ok=False, error=f"import_failed:{exc!r}", candidates_count=0, top_candidates=[])

    image_url = args.get("image_url") or ctx.get("image_url") or ""
    if not image_url:
        return SearchProductsResult(ok=False, error="missing_image_url", candidates_count=0, top_candidates=[])

    try:
        item = AnalyzedItem(
            id="agent-v2",
            category=args.get("style_node_primary") or "apparel",
            subcategory=None,
            fit=args.get("fit"),
            color_family=args.get("color_family"),
            search_query=text_query,
        )
        req = RecommendRequest(item=item, image_url=image_url, final_limit=int(args.get("top_k") or 15))
        resp = await run_pipeline(req)
        cands = list(getattr(resp, "candidates", None) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tool.search_products] pipeline raised: %r", exc)
        return SearchProductsResult(
            ok=False, error=f"pipeline_failed:{type(exc).__name__}", candidates_count=0, top_candidates=[]
        )

    top = [_candidate_to_dict(c) for c in cands[:5]]
    return SearchProductsResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
