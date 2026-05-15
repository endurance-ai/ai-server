"""SPEC-AGENT-001 / REQ-AGENT-004 (node 7/10) — search_node.

Wraps `app/pipeline/runner.py` (via `RecommendationPort.recommend`) plus the
post-search filters in `app/channels/recommendation.py::_apply_post_filters`
(applied inside the port).

Reads the session's `image_url` + `vision_item` + `vision_keywords` for the
base query, layers `critique_delta` + taste_profile, sets exclude_product_ids
to dedupe across refines (REQ-COMPAT-006).
"""

from __future__ import annotations

import logging
from typing import Any

from app.channels.recommendation import (
    ChannelRecommendationRequest,
    get_port,
)
from app.channels.session import get_store
from app.channels.taste_profile import (
    TasteProfile,
    get_taste_store,
    user_key_for,
)
from app.core.config import settings
from app.graphs.state import WorkingState
from app.observability.conversation_log import emit, scrub_exception_message
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


def _selected_vision_item(sess) -> Any | None:
    """Resolve the picker-selected (or single) VisionItem from session state.

    REQ-VISION-STATE-003 — search_node MUST never see a None vision_selected_item
    when vision_result.items is non-empty.
    """
    rich = sess.vision_result
    if rich is None:
        return None
    try:
        items = list(rich.items)
    except Exception:  # noqa: BLE001
        return None
    if not items:
        return None
    idx = sess.vision_selected_item_index
    if idx is None and len(items) == 1:
        idx = 0
    if idx is None:
        return None
    if 0 <= idx < len(items):
        return items[idx]
    return None


def _build_request(
    sess,
    delta,
    taste: TasteProfile | None,
    exclude_already_shown: bool,
) -> ChannelRecommendationRequest:
    """Compose the ChannelRecommendationRequest. Mirrors
    `scenario._build_channel_request` semantics with the addition of rich
    Vision fields (SPEC-VISION-UNIFY-001 / REQ-VISION-SEARCH-003)."""
    keywords = list(sess.vision_keywords or [])
    color: str | None = None
    intent_combined = sess.user_intent
    exclude_brands: list[str] = []
    exclude_keywords: list[str] = []
    boost_brands: list[str] = []
    boost_keywords: list[str] = []
    max_price: int | None = None
    min_price: int | None = None

    # SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-VALUE-MAPPING-001 — sticky boost.
    # sess.boost_keywords 는 clarify 응답에서 누적된 키워드. self-critique
    # fast-path 가 critique_delta 를 갈아치워도 살아남도록 세션에 두고
    # 매 검색마다 합산한다(R6 mitigation).
    sticky_boost = list(getattr(sess, "boost_keywords", None) or [])
    if sticky_boost:
        boost_keywords.extend(sticky_boost)

    if delta is not None:
        exclude_brands.extend(delta.exclude_brands)
        exclude_keywords.extend(delta.exclude_keywords)
        boost_keywords.extend(delta.boost_keywords)
        max_price = delta.max_price
        min_price = delta.min_price
        if delta.color:
            color = delta.color
        if delta.extra_intent:
            extra = delta.extra_intent.strip()
            base = (sess.user_intent or "").strip()
            if not base:
                intent_combined = extra
            elif extra.lower() == base.lower() or extra.lower() in base.lower():
                intent_combined = base
            else:
                intent_combined = f"{base} {extra}"

    if taste is not None:
        boost_brands.extend(taste.boost_brands(top_n=5))
        boost_keywords.extend(taste.boost_keywords(top_n=5))
        exclude_brands.extend(b for b in taste.exclude_brands(threshold=1.5) if b not in boost_brands)

    excl_ids = list(sess.shown_product_ids) if exclude_already_shown else []

    # SPEC-VISION-UNIFY-001 — populate rich item + outfit fields when available.
    selected = _selected_vision_item(sess)
    item_subcategory = selected.subcategory if selected else None
    item_fit = selected.fit if selected else None
    item_fabric = selected.fabric if selected else None
    item_color_family = selected.colorFamily if selected else None
    item_search_query_en = selected.searchQuery if selected else None
    item_search_query_ko = selected.searchQueryKo if selected else None

    return ChannelRecommendationRequest(
        image_url=sess.image_url,
        item_label=sess.vision_item,
        intent=intent_combined,
        keywords=keywords,
        tolerance=0.5,
        color=color,
        exclude_brands=list(dict.fromkeys(b.strip().lower() for b in exclude_brands if b and b.strip())),
        exclude_keywords=list(dict.fromkeys(k.strip().lower() for k in exclude_keywords if k and k.strip())),
        exclude_product_ids=list(excl_ids),
        boost_brands=list(dict.fromkeys(b.strip().lower() for b in boost_brands if b and b.strip())),
        boost_keywords=list(dict.fromkeys(k.strip().lower() for k in boost_keywords if k and k.strip())),
        max_price=max_price,
        min_price=min_price,
        item_subcategory=item_subcategory or None,
        item_fit=item_fit or None,
        item_fabric=item_fabric or None,
        item_color_family=item_color_family or None,
        item_search_query_en=item_search_query_en or None,
        item_search_query_ko=item_search_query_ko or None,
        outfit_style_node_primary=sess.vision_outfit_style_node_primary,
        outfit_style_node_secondary=sess.vision_outfit_style_node_secondary,
        outfit_mood_tags=list(sess.vision_outfit_mood_tags or []),
        outfit_gender=sess.vision_outfit_gender,
    )


# @MX:ANCHOR: [AUTO] SPEC-CONVERSATION-LOG-001 / REQ-LOG-PAYLOAD-RICH-001 — search_done emit
# @MX:REASON: ML-critical payload — top_k_product_ids[] parallel rrf_scores[] invariant
# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_search_done(
    state: WorkingState,
    *,
    req: ChannelRecommendationRequest | None,
    candidates: list,
    counts: dict,
) -> None:
    """LOG-T16 — emit `search_done` (REQ-LOG-PAYLOAD-RICH-001).

    Parallel-array invariant: ``len(top_k_product_ids) == len(rrf_scores)``.
    If any candidate is missing a score we coerce to 0.0 so the invariant
    is mechanically preserved (downstream ML pipelines can still spot the
    zero scores as "score absent").
    """
    try:
        top_k_product_ids: list[str] = []
        rrf_scores: list[float] = []
        for c in candidates:
            pid = str(getattr(c, "id", "") or "")
            top_k_product_ids.append(pid)
            raw_score = getattr(c, "rrf_score", None)
            if raw_score is None:
                raw_score = getattr(c, "score", None)
            try:
                rrf_scores.append(float(raw_score) if raw_score is not None else 0.0)
            except (TypeError, ValueError):
                rrf_scores.append(0.0)
        # Defensive invariant guard — REQ-LOG-PAYLOAD-RICH-001 acceptance.
        assert len(top_k_product_ids) == len(rrf_scores), (
            f"search_done parallel arrays mismatched: ids={len(top_k_product_ids)} scores={len(rrf_scores)}"
        )

        if req is not None:
            query_payload = {
                "text_query": (req.intent or req.item_search_query_en or req.item_label or "") or None,
                "sparse_terms": list(req.keywords or []),
                "embedding_present": bool(req.image_url),
                "filters": {
                    "exclude_brands": list(req.exclude_brands or []),
                    "exclude_keywords": list(req.exclude_keywords or []),
                    "exclude_product_ids_count": len(req.exclude_product_ids or []),
                    "boost_brands": list(req.boost_brands or []),
                    "boost_keywords": list(req.boost_keywords or []),
                    "max_price": req.max_price,
                    "min_price": req.min_price,
                    "color": req.color,
                },
            }
        else:
            query_payload = {
                "text_query": None,
                "sparse_terms": [],
                "embedding_present": False,
                "filters": {},
            }

        emit(
            event_type="search_done",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=6,
            payload={
                "query": query_payload,
                # Embedding bytes are produced inside the pipeline runner and
                # never reach this node. sha256-prefix-16 hashing therefore
                # happens here only as a stable "no-embedding" marker (None).
                # @MX:NOTE: future enhancement — surface embedding from pipeline.
                "embedding_ref": None,
                "top_k_product_ids": top_k_product_ids,
                "rrf_scores": rrf_scores,
                "dense_count": int(counts.get("dense_count", 0) or counts.get("dense", 0) or 0),
                "sparse_count": int(counts.get("sparse_count", 0) or counts.get("sparse", 0) or 0),
                # @MX:NOTE: filter_drop_log requires pipeline instrumentation
                # (recommendation.py._apply_post_filters). Surface empty list
                # here pending Phase 4 enhancement (out of scope per R12).
                "filter_drop_log": [],
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[search] search_done emit best-effort")


@observe(name="node.search", as_type="span")
async def search_node(state: WorkingState) -> dict:
    sess = get_store().get_or_create(state.chat_id)
    breadcrumbs: list[str] = []

    # Pre-search loading UX — friendly "지금 찾는 중" line + upload_photo
    # indicator. Common across DEMO and production. Best-effort: adapter
    # unbinding (test contexts) or transient telegram errors must not block
    # the actual search.
    from app.channels.lang import session_lang
    from app.graphs.nodes._adapter_ctx import get_adapter

    _loading_lang = session_lang(sess)
    _loading_text = (
        "잠시만요, 어울리는 아이템 찾고 있어요… 🔍"
        if _loading_lang == "ko"
        else "Hold on — pulling some matches for you… 🔍"
    )
    try:
        _adapter = get_adapter()
        await _adapter.send_text(state.chat_id, _loading_text)
        try:
            await _adapter.send_chat_action(state.chat_id, "upload_photo")
        except Exception:  # noqa: BLE001 — chat action is best-effort
            pass
    except RuntimeError:
        # Adapter ContextVar not bound (test contexts). Skip pre-message.
        pass
    except Exception:  # noqa: BLE001 — never let UX hint block the search
        logger.exception("🔍 [search] pre-search loading hint failed")

    # DEMO_MODE — local POC short-circuit. Bypass Modal/DB, return fixture
    # candidates as if search_products_v5 returned them. Same downstream
    # send_results / critique buttons / session caching.
    if settings.DEMO_MODE:
        import asyncio

        from langchain_core.messages import SystemMessage

        from app.pipeline.demo_fixtures import get_demo_candidates

        # Demo-only artificial pause so cards don't feel instant.
        await asyncio.sleep(2.0)

        candidates = get_demo_candidates()
        logger.info("🎬 [search] DEMO_MODE active — returning %d fixture candidates", len(candidates))
        breadcrumbs.append(f"search_node: DEMO_MODE fixture n={len(candidates)}")
        _emit_search_done(state, req=None, candidates=list(candidates), counts={})
        return {
            "candidates": candidates,
            "messages": [SystemMessage(content=f"search: {len(candidates)} candidate(s) (DEMO_MODE)")],
            "log_events": breadcrumbs,
            "turn_no": 6,
        }

    if not sess.image_url:
        breadcrumbs.append("search_node: no image_url; cannot search")
        _emit_search_done(state, req=None, candidates=[], counts={})
        return {"candidates": [], "log_events": breadcrumbs, "turn_no": 6}

    delta = state.critique_delta
    taste: TasteProfile | None = None
    if settings.TASTE_PROFILE_ENABLED:
        try:
            taste = get_taste_store().get_or_create(user_key_for(sess.from_user_id, sess.chat_id))
        except Exception:
            taste = None

    # "more" wants neighbors of the anchor (including some shown ones); other ops
    # exclude already-shown products (matches scenario.handle_critique_tap).
    exclude_shown = True
    if delta is not None and delta.op == "more":
        exclude_shown = False

    req = _build_request(sess, delta, taste, exclude_already_shown=exclude_shown)

    try:
        result = await get_port().recommend(req)
    except Exception as exc:  # REQ-AGENT-007
        logger.exception("🔍 [search] ❌ port.recommend raised")
        breadcrumbs.append(f"search_node_error: {type(exc).__name__}: {exc}"[:200])
        _emit_search_done(state, req=req, candidates=[], counts={})
        # LOG-T22 — node_error on recovered path (REQ-LOG-EMIT-EVERY-NODE-001 floor).
        try:
            emit(
                event_type="node_error",
                user_key=user_key_for(sess.from_user_id, state.chat_id),
                chat_id=state.chat_id,
                thread_id=state.thread_id,
                turn_no=6,
                payload={
                    "node_name": "search",
                    "exception_type": type(exc).__name__,
                    # security P1-01: secret scrub before persist
                    "message": scrub_exception_message(exc),
                    "recovered": True,
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("[search] node_error emit best-effort")
        return {"candidates": [], "log_events": breadcrumbs, "turn_no": 6}

    candidates: list[Any] = list(result.candidates) if result and result.candidates else []
    counts = dict(result.counts or {}) if result else {}
    logger.info(
        "🔍 [search] candidates=%d counts=%s exclude_shown=%s delta=%s",
        len(candidates),
        counts,
        exclude_shown,
        delta.op if delta else None,
    )
    breadcrumbs.append(f"search_node: candidates={len(candidates)} counts={counts}")

    from langchain_core.messages import SystemMessage

    # LOG-T16 — emit at success terminus (REQ-LOG-PAYLOAD-RICH-001).
    _emit_search_done(state, req=req, candidates=candidates, counts=counts)

    return {
        "candidates": candidates,
        "messages": [SystemMessage(content=f"search: {len(candidates)} candidate(s) after diversify+post-filter")],
        "log_events": breadcrumbs,
        "turn_no": 6,
    }
