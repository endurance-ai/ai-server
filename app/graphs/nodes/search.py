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

logger = logging.getLogger(__name__)


def _build_request(
    sess,
    delta,
    taste: TasteProfile | None,
    exclude_already_shown: bool,
) -> ChannelRecommendationRequest:
    """Compose the ChannelRecommendationRequest. Mirrors
    `scenario._build_channel_request` semantics exactly."""
    keywords = list(sess.vision_keywords or [])
    color: str | None = None
    intent_combined = sess.user_intent
    exclude_brands: list[str] = []
    exclude_keywords: list[str] = []
    boost_brands: list[str] = []
    boost_keywords: list[str] = []
    max_price: int | None = None
    min_price: int | None = None

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
    )


async def search_node(state: WorkingState) -> dict:
    sess = get_store().get_or_create(state.chat_id)
    breadcrumbs: list[str] = []

    if not sess.image_url:
        breadcrumbs.append("search_node: no image_url; cannot search")
        return {"candidates": [], "log_events": breadcrumbs}

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
        logger.exception("[search_node] port.recommend raised")
        breadcrumbs.append(f"search_node_error: {type(exc).__name__}: {exc}"[:200])
        return {"candidates": [], "log_events": breadcrumbs}

    candidates: list[Any] = list(result.candidates) if result and result.candidates else []
    breadcrumbs.append(
        f"search_node: candidates={len(candidates)} counts={dict(result.counts or {}) if result else {}}"
    )

    from langchain_core.messages import SystemMessage

    return {
        "candidates": candidates,
        "messages": [SystemMessage(content=f"search: {len(candidates)} candidate(s) after diversify+post-filter")],
        "log_events": breadcrumbs,
    }
