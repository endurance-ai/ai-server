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
import re
from typing import Any

from app.agents.tool_registry import RefineSearchResult
from app.agents.tools._keyword_utils import as_keyword_list as _as_keyword_list
from app.agents.tools._keyword_utils import dedup_join as _dedup_join
from app.agents.tools.search_products import (
    _candidate_to_dict,  # noqa: F401 — used in non-DEMO path; DEMO block re-imports locally
    _is_real_image_url,
    apply_dislike_discount,
    apply_price_filter,
    effective_max_price,
    persist_last_results,  # noqa: F401 — used in non-DEMO path; DEMO block re-imports locally
    pipeline_exc_detail,
    run_image_search,
    run_smart_blended_search,
    run_text_only_search,
)

logger = logging.getLogger(__name__)

# Test seam — import alias so tests can reference the helper without a
# module-internal underscore prefix concern.
_as_keyword_list_for_test = _as_keyword_list

# Mobile sends a pinned-product anchor as `[#<id> · <brand> · <name> · ₩<price>]`
# in front of the user's text (see kikoai-mobile home.tsx runStreamingTurn).
# When this id is present, refine_search re-anchors the search on that product's
# image embedding directly, bypassing the `last_query` text path that otherwise
# bleeds the previous session's query into the result set.
_PINNED_PID_RE = re.compile(r"#(\d+)")


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

    # Reconstruct text_query from the PREVIOUS search's product query + any
    # boost keywords. 260522 fix: prefer the cross-turn stored query
    # (`last_query`) over `ctx['text_query']` — on a refine turn the latter is
    # the RAW refinement instruction ("더 저렴하게 해줘 20만원 이하로"), and
    # embedding that returned semantically-unrelated cheap junk (bags/perfume).
    # The stored query is the actual product query ('grey floral lace dress
    # women'); a "cheaper" refine then just re-applies the price clamp on it.
    base_query = ""
    try:
        from app.agents.last_query import get_last_query

        base_query = get_last_query(ctx.get("chat_id")) or ""
    except Exception:  # noqa: BLE001
        base_query = ""
    if not base_query:
        base_query = ctx.get("text_query") or ""

    boost = _as_keyword_list(args.get("boost_keywords"))
    exclude_kw = _as_keyword_list(args.get("exclude_keywords"))

    # 260701 — Pinned-product anchor: when the user's CURRENT message text
    # carries a `#<id>` prefix (mobile pinned card → critique chip), refine
    # the search against that product's own embedding instead of the previous
    # text query. Without this, `last_query` (the prior session search text)
    # bleeds into the result set and the picks ignore which card was pinned.
    # fail-open: any error/miss falls back to the existing text-only path.
    pinned_embedding: list[float] | None = None
    pinned_category: str | None = None
    pinned_pid: int | None = None
    raw_msg = ctx.get("text_query") or ""
    _pid_match = _PINNED_PID_RE.search(raw_msg) if isinstance(raw_msg, str) else None
    if _pid_match is not None:
        try:
            pinned_pid = int(_pid_match.group(1))
        except (TypeError, ValueError):
            pinned_pid = None
    if pinned_pid is not None:
        try:
            from app.providers.database import DatabaseProvider

            pinned_embedding = await DatabaseProvider.get_product_embedding(pinned_pid)
            pinned_category = await DatabaseProvider.get_product_category(pinned_pid)
        except Exception:  # noqa: BLE001 — fail-open to text path
            pinned_embedding = None
            pinned_category = None
        if pinned_embedding is not None:
            logger.info(
                "🔍 [tool.refine_search] anchored on pinned product_id=%s (dim=%d category=%r)",
                pinned_pid,
                len(pinned_embedding),
                pinned_category,
            )
        else:
            logger.info(
                "🔍 [tool.refine_search] #%s in message but embedding unavailable — falling back to text path",
                pinned_pid,
            )
    # B15 — dedup tokens (case-insensitive, order-preserving). Without this,
    # chained refines accumulate the same token over and over (Langfuse trace
    # 66e78b7e: "wide jeans women roomy roomy roomy"). base_query order is
    # semantically meaningful so we walk it first, then append only
    # boost tokens not already present.
    text_query = _dedup_join(base_query, boost) or "fashion"

    # 260522: persist the refined query so a CHAINED refine reuses it (and
    # ctx so an in-turn respond/refine sees it). Mirrors search_products.
    # 260701 — Anchor turns: skip last_query persistence. The text_query on
    # an anchor turn is the mobile prefix ("[#id · brand · ...] 더 비슷하게"),
    # which is not a useful seed for any FUTURE refine that may not carry the
    # #id. Persisting it would pollute base_query on subsequent legacy refines
    # with prefix tokens. ctx["text_query"] is left alone so in-turn respond
    # can still read it for trace purposes.
    if text_query and text_query != "fashion" and pinned_embedding is None:
        ctx["text_query"] = text_query
        try:
            from app.agents.last_query import set_last_query

            set_last_query(ctx.get("chat_id"), text_query)
        except Exception:  # noqa: BLE001
            pass

    # Translate action → price clamp / drops. The ceiling falls back to the
    # per-request mobile filter slider (ctx.req_price_max) when the LLM didn't
    # supply an explicit max_price for this refine.
    max_price = effective_max_price(args.get("max_price"), ctx)
    min_price = None if args.get("drop_min_price") else args.get("min_price")

    try:
        # SPEC-SEARCH-V6-001 family-gate plumbing fix (mirrors search_products):
        # the search `category` is the REAL Vision garment category from ctx
        # (`vision_category`), NOT the brand style-node letter. Sharing the
        # same run_image_search/run_text_only_search, the same fix applies so
        # refine turns also engage the canonical family gate.
        # 260701 — Pinned anchor also overrides category/style_node. ctx
        # values were set by the PREVIOUS turn's Vision/text search and bleed
        # into the family gate when the user pins a card from a different
        # category (e.g. prior=knit → pin=jeans → family gate filters to knit
        # and the pinned-jeans embedding only returns knits). When anchored,
        # we use the pinned product's own category and clear the brand
        # style-node letter from the prior turn (per-product letter not
        # currently stored — None falls through to no style gate).
        if pinned_embedding is not None:
            category = pinned_category or ctx.get("vision_category")
        else:
            category = ctx.get("vision_category")
        # 260701 — Pinned anchor also clears fit / color_family. They were
        # set by the previous turn's Vision/text search ("white knit" →
        # ctx.color_family="white") and would otherwise narrow the anchored
        # search (e.g. user pins a blue jeans card → still filtered to
        # "white"). args wins when the LLM explicitly supplies a colour for
        # this refine (e.g. "다른 색상").
        if pinned_embedding is not None:
            fit = args.get("fit")
            color_family = args.get("color")
        else:
            fit = ctx.get("fit")
            color_family = args.get("color") or ctx.get("color_family")
        # SPEC-SEARCH-V6-STYLE-WIRING — refine turns reuse the Vision letter
        # that the original search already established (kept in ctx for the
        # whole chat turn by react_loop._build_ctx). text-only follow-up:
        # the LLM may also supply an explicit override in args (text turns
        # have no Vision letter); args wins when present.
        _args_sn = args.get("style_node_primary")
        if pinned_embedding is not None:
            # Pinned anchor: ignore ctx.style_node_primary (prior turn's letter).
            # Only respect an explicit LLM override.
            style_node_primary = _args_sn if (isinstance(_args_sn, str) and _args_sn.strip()) else None
        else:
            style_node_primary = (
                _args_sn if (isinstance(_args_sn, str) and _args_sn.strip()) else ctx.get("style_node_primary")
            )
        # SPEC-PERSONALIZE-RERANK — same user, same TasteProfile lookup.
        user_key = ctx.get("user_key")

        # Multi-turn image blending (Level 1): when no current image URL exists
        # but the original image URL is stored from the Vision turn, blend the
        # image vector with the text modifier so the original outfit identity
        # is preserved across follow-up turns ("more casual", "different colour").
        origin_url = None
        if not has_image:
            try:
                from app.agents.origin_image import get_origin_url

                origin_url = get_origin_url(ctx.get("chat_id"))
            except Exception:  # noqa: BLE001
                pass

        if has_image:
            cands = await run_image_search(
                image_url=str(ctx_image),
                text_query=text_query,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=15,
                style_node_primary=style_node_primary,
                user_key=user_key,
            )
        elif origin_url:
            # Intent-aware Level 2 advanced blending — vector arithmetic for
            # color_swap/fit_change, intent-tuned weighted-sum otherwise.
            # Prior outfit context anchors the FROM-attribute extraction.
            prior_ctx_parts = [
                str(base_query or text_query or ""),
                str(category or ""),
                str(fit or ""),
                str(color_family or ""),
            ]
            prior_ctx = " ".join(p for p in prior_ctx_parts if p).strip()
            cands = await run_smart_blended_search(
                origin_url=origin_url,
                modifier_query=text_query,
                chat_id=ctx.get("chat_id"),
                prior_outfit_context=prior_ctx or None,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=15,
                style_node_primary=style_node_primary,
                user_key=user_key,
            )
        else:
            cands = await run_text_only_search(
                text_query=text_query,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=15,
                style_node_primary=style_node_primary,
                user_key=user_key,
                override_embedding=pinned_embedding,
            )
    except Exception as exc:  # noqa: BLE001
        # P1-6 (260521): shared enrichment helper. Host in log only (internal
        # infra — security 260522); status code kept in Result.error.
        logger.warning(
            "[tool.refine_search] pipeline raised: %s (%r)",
            pipeline_exc_detail(exc, include_host=True),
            exc,
        )
        return RefineSearchResult(
            ok=False,
            error=f"pipeline_failed:{pipeline_exc_detail(exc, include_host=False)}",
            candidates_count=0,
            top_candidates=[],
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

    # 260611 — emit `search_done` (is_refine=True) so the next turn's memory
    # context still surfaces this as the active query for further refinement.
    try:
        from app.agents.tools.search_products import emit_search_done

        emit_search_done(
            ctx=ctx,
            text_query=text_query,
            cands=cands,
            is_refine=True,
        )
    except Exception:  # noqa: BLE001 — observability is best-effort
        logger.debug("[refine_search] search_done emit best-effort skip", exc_info=True)

    top = [_candidate_to_dict(c) for c in cands[:5]]
    return RefineSearchResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
