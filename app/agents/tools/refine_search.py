"""SPEC-AGENT-V2-REACT / T-003c — `refine_search` tool wrapper (OQ-7 α).

Wraps the v1 critique re-search path. evaluator is folded — this tool does
NOT call evaluator internally. If the LLM wants another refinement, it must
call `refine_search` (or another tool) on the next iteration.

1-shot retry budget enforced via `ctx["refine_count"]` (incremented by react_loop).

@MX:NOTE: [AUTO] Side effect: DB RPC + Modal embedding call.
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import RefineSearchResult
from app.agents.tools.search_products import _candidate_to_dict

logger = logging.getLogger(__name__)


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> RefineSearchResult:
    action = args.get("action") or "broaden"
    # P1-2: image_url is optional. In V2 the agent LLM can call refine_search
    # after a text-only search_products turn (no photo). The pipeline handles
    # empty image_url on the dense path, so proceed text-query-only.
    image_url = ctx.get("image_url") or ""

    # Reconstruct text_query from ctx + boost_keywords if present.
    base_query = ctx.get("text_query") or ""
    boost = list(args.get("boost_keywords") or [])
    exclude_kw = list(args.get("exclude_keywords") or [])
    text_query = " ".join([base_query, *boost]).strip() or "fashion"

    # Translate action → price clamp / drops.
    max_price = args.get("max_price")
    min_price = None if args.get("drop_min_price") else args.get("min_price")

    try:
        from app.models.request import AnalyzedItem, RecommendRequest
        from app.pipeline.runner import run_pipeline

        item = AnalyzedItem(
            id="agent-v2-refine",
            category=ctx.get("style_node_primary") or "apparel",
            subcategory=None,
            fit=ctx.get("fit"),
            color_family=args.get("color") or ctx.get("color_family"),
            search_query=text_query,
        )
        req = RecommendRequest(item=item, image_url=image_url, final_limit=15)
        resp = await run_pipeline(req)
        cands = list(getattr(resp, "candidates", None) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tool.refine_search] pipeline raised: %r", exc)
        return RefineSearchResult(
            ok=False, error=f"pipeline_failed:{type(exc).__name__}", candidates_count=0, top_candidates=[]
        )

    # Apply exclude_keywords client-side as a thin filter (best-effort).
    if exclude_kw:
        ek = {k.lower() for k in exclude_kw}
        cands = [c for c in cands if not any(k in (getattr(c, "title", "") or "").lower() for k in ek)]

    top = [_candidate_to_dict(c) for c in cands[:5]]
    _ = action, max_price, min_price  # informational only in α
    return RefineSearchResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
