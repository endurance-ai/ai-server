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
from app.agents.tools._keyword_utils import strip_color_tokens as _strip_color_tokens
from app.agents.tools.search_products import (
    _candidate_to_dict,  # noqa: F401 — used in non-DEMO path; DEMO block re-imports locally
    _is_real_image_url,
    _query_gender,
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
#
# 260701 — Anchor ONLY on the mobile prefix shape at the start of the message
# (leading `[#<digits>`). The old ``r"#(\d+)"`` matched anywhere, so a free-text
# turn like "그 #1 같은 거" would accidentally trigger a product_id=1 fetch.
# fail-open still catches that edge (no embedding row → text fallback), but
# tightening the pattern avoids the spurious DB round-trip + log noise entirely.
_PINNED_PID_RE = re.compile(r"^\[#(\d+)")


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> RefineSearchResult:
    # 2026-08-26 — "search" pre-message 제거 (search_products 와 동일). 스피너가
    # 이미 검색 중임을 보여줘 잉여였다. dict 키는 보존, 발사만 중단.
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
    refine_brand: list[str] | None = None
    try:
        from app.agents.last_query import get_last_brand, get_last_query

        base_query = get_last_query(ctx.get("chat_id")) or ""
        # 2026-08-19 — 직전 검색 브랜드를 이어받는다. '다른 색상으로' 같은 refine 이
        # 브랜드를 잃고 다른 브랜드 상품을 뽑던 버그 대응. exclude 턴은 아래에서 배제
        # 대상을 이 상속 목록에서 빼고(0건 방지) 결과도 client-side 로 드롭한다.
        # 2026-08-30 — 종전 `action != "exclude_brands"` 가드는 enum 실제값 'exclude'
        # 와 안 맞아 항상 True(죽은 코드)였고, exclude_brands 리스트도 어디서도 적용
        # 안 돼 완전 no-op 였다. 가드를 걷고 항상 상속 → 아래 exclude 처리로 일원화.
        refine_brand = get_last_brand(ctx.get("chat_id"))
    except Exception:  # noqa: BLE001
        base_query = ""
    if not base_query:
        base_query = ctx.get("text_query") or ""

    boost = _as_keyword_list(args.get("boost_keywords"))
    exclude_kw = _as_keyword_list(args.get("exclude_keywords"))
    exclude_brands = _as_keyword_list(args.get("exclude_brands"))
    # v2.6 무드 delta — 직전 결과를 특정 무드로 좁힌다(final_tags 하드필터, service 에서 적용).
    mood = str(args.get("mood") or "").strip() or None
    # 이어받은 positive 브랜드 필터에서 배제 대상 제거 — 그 브랜드로 검색하며 동시에
    # 배제하면 0건이 되므로(예: 직전 'Zara 재킷' → '자라 빼고').
    if refine_brand and exclude_brands:
        _ebl = {x.lower() for x in exclude_brands}
        refine_brand = [b for b in refine_brand if b.lower() not in _ebl] or None

    # P0-b (2026-08-24) — 색 변주 정상화. base_query 는 직전 검색의 상품 쿼리라
    # 이전 색 단어("black cropped hoodie")를 그대로 물고 있다. color 를 새로 주면
    # (color_swap) 그 색 토큰을 임베딩 쿼리에서 걷어내고 새 색을 boost 로 실어,
    # color_family 하드필터(아래 color_family) + 임베딩 소프트신호가 같은 방향을
    # 가리키게 한다. 이게 없으면 임베딩이 옛 색을 당기고 color 필터는 재고부족으로
    # relax·drop 돼(strict_count=0) 색이 안 바뀌던 실트레이스(2026-08-24 닝닝→pink).
    _new_color = str(args.get("color") or "").strip().lower()
    if _new_color:
        base_query = _strip_color_tokens(base_query)
        if _new_color not in {b.lower() for b in boost}:
            boost = [_new_color, *boost]

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
            from app.agents.last_query import set_last_brand, set_last_query

            set_last_query(ctx.get("chat_id"), text_query)
            # 이어받은 브랜드를 다시 저장 → 체인 refine("다른 색상으로" → "더 슬림하게")
            # 도 브랜드를 계속 유지한다.
            set_last_brand(ctx.get("chat_id"), refine_brand)
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

        # 2026-07-16 — v6 p_gender 하드 필터: 리파인의 최종 쿼리(base_query 는
        # 이전 턴의 gender-pinned product query)에서 gender 토큰을 역파싱.
        # 없으면 None(필터 off). 'unisex' 는 service 가 None 으로 매핑.
        refine_gender = _query_gender(text_query or "") or _query_gender(base_query or "")
        if has_image:
            cands = await run_image_search(
                image_url=str(ctx_image),
                text_query=text_query,
                category=category,
                gender=refine_gender,
                fit=fit,
                color_family=color_family,
                mood=mood,
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
                gender=refine_gender,
                fit=fit,
                color_family=color_family,
                mood=mood,
                top_k=15,
                style_node_primary=style_node_primary,
                user_key=user_key,
            )
        else:
            cands = await run_text_only_search(
                text_query=text_query,
                category=category,
                gender=refine_gender,
                brand_filter=refine_brand,
                fit=fit,
                color_family=color_family,
                mood=mood,
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

    # exclude_brands client-side drop (parity with search_products D2). Fixes the
    # prior no-op: the LLM passes action='exclude' + exclude_brands=['Zara'] but
    # the list was never applied anywhere. Drop candidates whose brand matches an
    # excluded brand (case-insensitive exact OR substring).
    if exclude_brands:
        _ebset = {b.lower() for b in exclude_brands}

        def _brand_excluded(c: Any) -> bool:
            b = (c.get("brand") if isinstance(c, dict) else getattr(c, "brand", "")) or ""
            b = str(b).lower()
            return bool(b) and any(x == b or x in b for x in _ebset)

        cands = [c for c in cands if not _brand_excluded(c)]

    # SPEC-AGENT-V3-REACT Gap4 — merge cross-thread dislike (flag-gated; OFF →
    # unchanged → V2 byte-identical). Reuses the search_products helper.
    cands = apply_dislike_discount(ctx, cands)

    # Price bounds — reuse the shared helper from search_products so both
    # entry points apply identical semantics (KRW integer 원, missing-price
    # rows dropped when ANY bound is set). 2026-05-20: previously discarded
    # ("informational only in α") — now wired through to actual filtering.
    #
    # Weak-result rescue (SPEC-AGENT-V2-REACT follow-up, 2026-07-04):
    # a price clamp on a sparse catalog slice frequently drops candidates to
    # 0-1. Keep the pre-filter set so we can relax `max_price` ONCE and
    # re-filter without paying another embed+RPC round-trip. Langfuse trace
    # 14279b4a: "더 저렴한 카고팬츠" → refine returned 0 → agent apologized
    # instead of trying a looser price. The prompt-level rule (see
    # `_PROACTIVE_DIRECTIVE`) tells the LLM to retry; this is the code-level
    # safety net for when the LLM doesn't follow it.
    _pre_filter_cands = list(cands)
    cands = apply_price_filter(cands, min_price, max_price)
    if len(cands) < 3 and max_price is not None and _pre_filter_cands:
        # Bump the ceiling +25%, but no lower than the smallest available
        # price in the pre-filter set (guarantees at least one row clears).
        try:
            _prices = [
                int(getattr(c, "price", 0) or 0) for c in _pre_filter_cands if int(getattr(c, "price", 0) or 0) > 0
            ]
            _min_available = min(_prices) if _prices else None
        except (TypeError, ValueError):
            _min_available = None
        bumped = int(max_price * 1.25)
        if _min_available is not None and _min_available > bumped:
            # Even the cheapest row is above +25%; use its price as the
            # ceiling so the rescue always yields ≥1 result.
            bumped = _min_available
        rescue_cands = apply_price_filter(_pre_filter_cands, min_price, bumped)
        if len(rescue_cands) > len(cands):
            logger.info(
                "🔍 [tool.refine_search] weak result (%d) → price rescue %s → %s (rescued %d cands)",
                len(cands),
                max_price,
                bumped,
                len(rescue_cands),
            )
            cands = rescue_cands
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
    result = RefineSearchResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
    from app.agents.tools.search_products import _build_color_notice, _build_result_digest

    _notice = _build_color_notice()
    if _notice:
        result["notice"] = _notice
    _digest = await _build_result_digest(cands)
    if _digest:
        result["digest"] = _digest
    return result
