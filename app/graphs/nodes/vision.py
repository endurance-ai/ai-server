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
from app.channels.taste_profile import user_key_for
from app.channels.vision import VisionResult, derive_legacy_dict, derive_legacy_keywords, derive_legacy_label
from app.graphs.state import WorkingState
from app.observability.conversation_log import emit, scrub_exception_message
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


def _summary_message(result: VisionResult) -> SystemMessage:
    n = len(result.items)
    primary = result.items[0].name or result.items[0].subcategory if result.items else "(none)"
    return SystemMessage(content=f"vision: detected {n} item(s); primary={primary}; node={result.styleNode.primary}")


# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_node_error(state: WorkingState, *, node_name: str, exc: Exception, recovered: bool) -> None:
    """LOG-T22 — emit `node_error` for an inner-except path. Never raises."""
    try:
        emit(
            event_type="node_error",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=state.turn_no,
            payload={
                "node_name": node_name,
                "exception_type": type(exc).__name__,
                # security P1-01: scrub DSN/Bearer/api_key/secrets from exception text
                "message": scrub_exception_message(exc),
                "recovered": recovered,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[%s] node_error emit best-effort", node_name)


def _emit_vision_done(
    state: WorkingState,
    result: VisionResult | None,
    *,
    error: str | None = None,
) -> None:
    """LOG-T13 — emit `vision_done` with v2 schema payload (or error field)."""
    try:
        if result is not None:
            sn = getattr(result, "styleNode", None)
            mood = getattr(result, "mood", None)
            payload: dict = {
                "style_node_primary": getattr(sn, "primary", None) if sn else None,
                "style_node_secondary": getattr(sn, "secondary", None) if sn else None,
                "sensitivity_tags": list(getattr(result, "sensitivityTags", []) or []),
                "mood": [t.label for t in getattr(mood, "tags", []) if getattr(t, "label", "")]
                if mood is not None
                else [],
                "palette": [p.label for p in (getattr(result, "palette", []) or []) if getattr(p, "label", "")],
                "style": getattr(getattr(result, "style", None), "aesthetic", None),
                "gender": getattr(getattr(result, "style", None), "detectedGender", None),
                "items": [
                    {
                        "subcategory": getattr(it, "subcategory", None),
                        "fit": getattr(it, "fit", None),
                        "colorFamily": getattr(it, "colorFamily", None),
                        "searchQuery": getattr(it, "searchQuery", None),
                        "searchQueryKo": getattr(it, "searchQueryKo", None),
                    }
                    for it in (result.items or [])
                ],
                "schema_v2_used": True,
                "error": None,
            }
        else:
            payload = {
                "schema_v2_used": False,
                "items": [],
                "error": error,
            }
        emit(
            event_type="vision_done",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=3,
            payload=payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[vision] vision_done emit best-effort")


def _outfit_mood_tags(result: VisionResult, top_n: int = 5) -> list[str]:
    """Top-N mood tag labels by score (REQ-VISION-OBSV-001 cap=5)."""
    tags = sorted(result.mood.tags, key=lambda t: t.score, reverse=True)
    return [t.label for t in tags[:top_n] if t.label]


@observe(name="node.vision", as_type="span")
async def vision_node(state: WorkingState) -> dict:
    if not state.image_url:
        # No image to analyse — skip the LLM, but DO emit so the conversation
        # log shows the node executed (recovered=True path).
        _emit_vision_done(state, None, error="no_image_url")
        return {"log_events": ["vision_node: no image_url; skipping"], "turn_no": 3}

    # DEMO_MODE — skip Vision LLM, return fixture after a short wait so the
    # UX still feels like "analyzing the image".
    from app.core.config import settings as _settings

    if _settings.DEMO_MODE:
        import asyncio

        from app.pipeline.demo_fixtures import get_demo_vision_result

        # Pinterest link_resolver already adds ~2s for og:image fetch; keep
        # vision delay short so total time-to-first-card stays near 2s.
        await asyncio.sleep(0.5)
        result = get_demo_vision_result()
        logger.info("🎬 [vision] DEMO_MODE — fixture VisionResult (items=%d)", len(result.items))
    else:
        try:
            result = await vision_module.extract(state.image_url)
        except Exception as exc:  # REQ-AGENT-007
            logger.exception("👁 [vision] ❌ vision.extract raised")
            _emit_vision_done(state, None, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            _emit_node_error(state, node_name="vision", exc=exc, recovered=True)
            return {
                "log_events": [f"vision_node_error: {type(exc).__name__}: {exc}"[:200]],
                "turn_no": 3,
            }

    # Defensive: extract() should always return a VisionResult. Tests
    # monkeypatching `vision_module.extract` may still return the legacy dict
    # shape; coerce in that case so REQ-VISION-COMPAT-001 holds without
    # rewriting every patched fixture.
    if isinstance(result, dict):
        from app.channels.vision import _legacy_to_vision_result

        result = _legacy_to_vision_result(result)
    elif not isinstance(result, VisionResult):
        _emit_vision_done(state, None, error=f"unexpected_type:{type(result).__name__}")
        return {
            "log_events": [f"vision_node: unexpected type {type(result).__name__}"],
            "turn_no": 3,
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

    # LOG-T13 — emit BEFORE return so the row reflects the v2 schema payload.
    _emit_vision_done(state, result)

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
        "turn_no": 3,
    }
