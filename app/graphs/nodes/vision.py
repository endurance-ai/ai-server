"""SPEC-AGENT-001 / REQ-AGENT-004 (node 3/10) — vision_node.

Wraps: `app/channels/vision.py::extract` (LiteLLM `gpt-4o-mini`).

Populates `detected_items` (legacy dict shape, derived from rich
`VisionResult`) plus the new SPEC-VISION-UNIFY-001 rich state fields
(`vision_result`, `vision_outfit_*`). Emits a SystemMessage breadcrumb.

The strength-of-result classification (clear / multi / ambiguous / fallback)
is read by `routing._route_after_vision`.
"""

from __future__ import annotations

import logging

from langchain_core.messages import SystemMessage

from app.channels import vision as vision_module
from app.channels.session import get_store
from app.channels.vision import VisionResult, derive_legacy_dict, derive_legacy_keywords, derive_legacy_label
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


def _summary_message(result: VisionResult) -> SystemMessage:
    n = len(result.items)
    primary = result.items[0].name or result.items[0].subcategory if result.items else "(none)"
    return SystemMessage(content=f"vision: detected {n} item(s); primary={primary}; node={result.styleNode.primary}")


def _outfit_mood_tags(result: VisionResult, top_n: int = 5) -> list[str]:
    """Top-N mood tag labels by score (REQ-VISION-OBSV-001 cap=5)."""
    tags = sorted(result.mood.tags, key=lambda t: t.score, reverse=True)
    return [t.label for t in tags[:top_n] if t.label]


@observe(name="node.vision", as_type="span")
async def vision_node(state: WorkingState) -> dict:
    if not state.image_url:
        return {"log_events": ["vision_node: no image_url; skipping"]}

    try:
        result = await vision_module.extract(state.image_url)
    except Exception as exc:  # REQ-AGENT-007
        logger.exception("👁 [vision] ❌ vision.extract raised")
        return {
            "log_events": [f"vision_node_error: {type(exc).__name__}: {exc}"[:200]],
        }

    # Defensive: extract() should always return a VisionResult. Tests
    # monkeypatching `vision_module.extract` may still return the legacy dict
    # shape; coerce in that case so REQ-VISION-COMPAT-001 holds without
    # rewriting every patched fixture.
    if isinstance(result, dict):
        from app.channels.vision import _legacy_to_vision_result

        result = _legacy_to_vision_result(result)
    elif not isinstance(result, VisionResult):
        return {
            "log_events": [f"vision_node: unexpected type {type(result).__name__}"],
        }

    # Project rich items into legacy-dict shape for `detected_items` so existing
    # routing/picker logic keeps working. Each dict carries both legacy keys
    # (label/description/color/keywords) AND rich keys (subcategory/fit/...).
    detected: list[dict] = [derive_legacy_dict(it) for it in result.items]

    logger.info(
        "👁 [vision] items=%d apparel=%s labels=%s",
        len(result.items),
        result.isApparel,
        [d.get("label", "") for d in detected[:5]],
    )

    outfit_mood = _outfit_mood_tags(result)
    style_primary = result.styleNode.primary or None
    style_secondary = result.styleNode.secondary or None
    detected_gender = result.style.detectedGender or None

    # REQ-VISION-STATE-002 / REQ-COMPAT-002 — persist to session for cross-webhook
    # consumption. Single-item path pre-populates legacy + rich selection so the
    # subsequent critique_apply → search has full context. Multi-item waits
    # for pick_item.
    selected_item = None
    if result.items:
        try:
            sess = get_store().get_or_create(state.chat_id)
            sess.detected_items = list(detected)
            sess.vision_result = result
            sess.vision_outfit_style_node_primary = style_primary
            sess.vision_outfit_style_node_secondary = style_secondary
            sess.vision_outfit_mood_tags = list(outfit_mood)
            sess.vision_outfit_gender = detected_gender
            if len(result.items) == 1:
                only = result.items[0]
                selected_item = only
                sess.vision_selected_item_index = 0
                sess.vision_item = derive_legacy_label(only)
                sess.vision_keywords = derive_legacy_keywords(only)
            get_store().update(sess)
        except Exception:
            logger.exception("👁 [vision] ❌ session persist failed")

    return {
        "detected_items": detected,
        "vision_result": result,
        "vision_selected_item": selected_item,
        "vision_outfit_style_node_primary": style_primary,
        "vision_outfit_style_node_secondary": style_secondary,
        "vision_outfit_mood_tags": list(outfit_mood),
        "vision_outfit_gender": detected_gender,
        "messages": [_summary_message(result)],
        "log_events": [f"vision_node: items={len(result.items)} apparel={result.isApparel}"],
    }
