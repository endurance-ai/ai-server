"""SPEC-AGENT-001 / REQ-AGENT-004 (node 3/10) — vision_node.

Wraps: `app/channels/vision.py::extract` (LiteLLM `gpt-4o-mini`).

Populates `detected_items` and emits a SystemMessage breadcrumb (plan.md Q4).
The strength-of-result classification (clear / multi / ambiguous / fallback)
is read by `routing._route_after_vision`.
"""

from __future__ import annotations

import logging

from langchain_core.messages import SystemMessage

from app.channels import vision as vision_module
from app.channels.session import get_store
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)


def _summary_message(items: list[dict]) -> SystemMessage:
    primary = (items[0].get("label") or "item") if items else "(none)"
    return SystemMessage(content=f"vision: detected {len(items)} item(s); primary={primary}")


async def vision_node(state: WorkingState) -> dict:
    if not state.image_url:
        return {"log_events": ["vision_node: no image_url; skipping"]}

    try:
        data = await vision_module.extract(state.image_url)
    except Exception as exc:  # REQ-AGENT-007
        logger.exception("[vision_node] vision.extract raised")
        return {
            "log_events": [f"vision_node_error: {type(exc).__name__}: {exc}"[:200]],
        }

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []

    # REQ-COMPAT-002 — persist vision result to session so later turns
    # (picker tap → intent reply → search) can resolve item context. Mirrors
    # the original scenario.handle_new_image which wrote session.vision_item /
    # session.vision_keywords / session.detected_items right after extract.
    if items:
        try:
            sess = get_store().get_or_create(state.chat_id)
            sess.detected_items = list(items)
            # Single-item path: pre-populate vision_item / keywords so the
            # subsequent critique_apply → search_node has context. Multi-item
            # path waits for pick_item to set these from the user's choice.
            if len(items) == 1:
                only = items[0]
                sess.vision_item = only.get("label") or "item"
                sess.vision_keywords = list(only.get("keywords") or [])
            get_store().update(sess)
        except Exception:
            logger.exception("[vision_node] session persist failed")

    return {
        "detected_items": items,
        "messages": [_summary_message(items)],
        "log_events": [f"vision_node: items={len(items)}"],
    }
