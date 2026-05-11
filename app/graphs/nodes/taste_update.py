"""SPEC-AGENT-001 / REQ-AGENT-004 (node 9/10) — taste_update.

Wraps `app/channels/taste_profile.py::reinforce_*`. Applies a `TasteUpdate`
(from `state.decision.taste_update`) to the per-user TasteProfile, then hands
off to `respond` for the natural-language acknowledgement.
"""

from __future__ import annotations

import logging

from app.channels.router import RoutedIntent, TasteUpdate
from app.channels.session import get_store
from app.channels.taste_profile import (
    TasteProfile,
    get_taste_store,
    user_key_for,
)
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


def _summarize(update: TasteUpdate) -> str:
    bits: list[str] = []
    if update.liked_brands:
        bits.append("you like " + ", ".join(update.liked_brands[:3]))
    if update.disliked_brands:
        bits.append("you avoid " + ", ".join(update.disliked_brands[:3]))
    if update.liked_keywords:
        bits.append("you like " + ", ".join(update.liked_keywords[:3]))
    if update.disliked_keywords:
        bits.append("you avoid " + ", ".join(update.disliked_keywords[:3]))
    return "; ".join(bits)


def _apply(profile: TasteProfile, update: TasteUpdate) -> None:
    for b in update.liked_brands:
        profile.reinforce_liked_brand(b, weight=2.0)
    for b in update.disliked_brands:
        profile.reinforce_disliked_brand(b, weight=2.0)
    if update.liked_keywords:
        profile.reinforce_liked_keywords(update.liked_keywords, weight=2.0)
    if update.disliked_keywords:
        profile.reinforce_disliked_keywords(update.disliked_keywords, weight=2.0)


@observe(name="node.taste_update", as_type="span")
async def taste_update(state: WorkingState) -> dict:
    breadcrumbs: list[str] = []

    decision = state.decision
    if decision is None or decision.intent != RoutedIntent.TASTE_UPDATE or decision.taste_update is None:
        breadcrumbs.append("taste_update: no taste_update payload — skipping")
        return {"log_events": breadcrumbs}

    sess = get_store().get_or_create(state.chat_id)
    update = decision.taste_update

    try:
        store = get_taste_store()
        profile = store.get_or_create(user_key_for(sess.from_user_id, sess.chat_id))
        _apply(profile, update)
        store.update(profile)
    except Exception as exc:  # REQ-AGENT-007
        logger.exception("[taste_update] reinforce_* raised")
        breadcrumbs.append(f"taste_update_error: {type(exc).__name__}: {exc}"[:200])
        return {"log_events": breadcrumbs}

    summary = _summarize(update)
    breadcrumbs.append(f"taste_update: applied summary={summary[:80]}")
    return {
        "presearch_summary": summary,
        "log_events": breadcrumbs,
    }
