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
from app.agents.tools.search_products import (
    _candidate_to_dict,
    _is_real_image_url,
    apply_dislike_discount,
    apply_price_filter,
    persist_last_results,
    run_image_search,
    run_text_only_search,
)

logger = logging.getLogger(__name__)


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> RefineSearchResult:
    # SPEC-AGENT-UX-P0-001 / REQ-UX-004 — refine 도 같은 "search" 멘트
    # ("잠시만요, …찾아볼게요"). search_products 와 동일 ctx marker 키
    # (`_pre_msg_sent:search`) 라서 같은 턴에서 두 번 호출돼도 idempotent.
    try:
        from app.channels.pre_messages import fire_pre_message
        from app.graphs.nodes._adapter_ctx import _adapter_var

        await fire_pre_message(
            _adapter_var.get(),
            ctx,
            key="search",
            lang=ctx.get("lang") or "en",
            chat_id=ctx.get("chat_id"),
        )
    except Exception:  # noqa: BLE001 — never block refine pipeline
        logger.debug("[tool.refine_search] pre-message skipped")

    action = args.get("action") or "broaden"
    # image_url is sourced from ctx ONLY (never an LLM arg). When no real
    # resolved image is present we route to the text/sparse-only search —
    # the previous code passed image_url="" straight into run_pipeline, which
    # POSTed an empty URL to Modal and produced 0 results (same root bug).
    ctx_image = ctx.get("image_url")
    has_image = _is_real_image_url(ctx_image)

    # Reconstruct text_query from ctx + boost_keywords if present.
    base_query = ctx.get("text_query") or ""
    boost = list(args.get("boost_keywords") or [])
    exclude_kw = list(args.get("exclude_keywords") or [])
    text_query = " ".join([base_query, *boost]).strip() or "fashion"

    # Translate action → price clamp / drops.
    max_price = args.get("max_price")
    min_price = None if args.get("drop_min_price") else args.get("min_price")

    try:
        # SPEC-SEARCH-V6-001 family-gate plumbing fix (mirrors search_products):
        # the search `category` is the REAL Vision garment category from ctx
        # (`vision_category`), NOT the brand style-node letter. Sharing the
        # same run_image_search/run_text_only_search, the same fix applies so
        # refine turns also engage the canonical family gate.
        category = ctx.get("vision_category")
        fit = ctx.get("fit")
        color_family = args.get("color") or ctx.get("color_family")
        if has_image:
            cands = await run_image_search(
                image_url=str(ctx_image),
                text_query=text_query,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=15,
            )
        else:
            cands = await run_text_only_search(
                text_query=text_query,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=15,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tool.refine_search] pipeline raised: %r", exc)
        return RefineSearchResult(
            ok=False, error=f"pipeline_failed:{type(exc).__name__}", candidates_count=0, top_candidates=[]
        )

    # Apply exclude_keywords client-side as a thin filter (best-effort).
    if exclude_kw:
        ek = {k.lower() for k in exclude_kw}
        cands = [c for c in cands if not any(k in (getattr(c, "title", "") or "").lower() for k in ek)]

    # SPEC-AGENT-V3-REACT Gap4 — merge cross-thread dislike (flag-gated; OFF →
    # unchanged → V2 byte-identical). Reuses the search_products helper.
    cands = apply_dislike_discount(ctx, cands)

    # Price bounds — reuse the shared helper from search_products so both
    # entry points apply identical semantics (KRW integer 원, missing-price
    # rows dropped when ANY bound is set). 2026-05-20: previously discarded
    # ("informational only in α") — now wired through to actual filtering.
    cands = apply_price_filter(cands, min_price, max_price)
    _ = action  # action is informational metadata, not used for filtering

    # Persist FULL refined candidates so `respond` renders real cards
    # internally (parity with search_products; LLM never serializes cards).
    persist_last_results(ctx, cands)

    top = [_candidate_to_dict(c) for c in cands[:5]]
    return RefineSearchResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
