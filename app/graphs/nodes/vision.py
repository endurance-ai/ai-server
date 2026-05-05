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

    return {
        "detected_items": items,
        "messages": [_summary_message(items)],
        "log_events": [f"vision_node: items={len(items)}"],
    }
