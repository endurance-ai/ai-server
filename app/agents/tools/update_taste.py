"""SPEC-AGENT-V2-REACT / T-003d — `update_taste` tool wrapper.

Thin wrapper over TasteProfileStore.update.

@MX:NOTE: [AUTO] Side effect: TasteProfile mutation (in-memory or Postgres).
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import UpdateTasteResult

logger = logging.getLogger(__name__)

_VALID_SOURCES = {"click", "onboard", "pinterest", "critique", "free_text", "no_click", "re_query"}


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> UpdateTasteResult:
    source = args.get("source")
    if source not in _VALID_SOURCES:
        return UpdateTasteResult(ok=False, error=f"invalid_source:{source}", applied=False)

    user_key = ctx.get("user_key")
    if not user_key:
        return UpdateTasteResult(ok=False, error="missing_user_key", applied=False)

    try:
        from app.infrastructure.memory.taste_profile import get_taste_store

        store = get_taste_store()
        profile = store.get_or_create(user_key)

        for b in args.get("brand_likes", []) or []:
            profile.reinforce_liked_brand(b)
        for b in args.get("brand_dislikes", []) or []:
            profile.reinforce_disliked_brand(b)
        likes = list(args.get("keyword_likes", []) or [])
        dislikes = list(args.get("keyword_dislikes", []) or [])
        if likes:
            profile.reinforce_liked_keywords(likes)
        if dislikes:
            profile.reinforce_disliked_keywords(dislikes)
        store.update(profile)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tool.update_taste] raised: %r", exc)
        return UpdateTasteResult(ok=False, error=f"store_failed:{type(exc).__name__}", applied=False)

    return UpdateTasteResult(ok=True, error=None, applied=True)
