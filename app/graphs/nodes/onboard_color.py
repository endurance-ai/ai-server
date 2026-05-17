"""Onboarding Stage 2 — color card callback handler.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-002 + REQ-ONBOARD-OBS-001.

Handles `onboard:color:{toggle|done|skip}[:value]` callbacks. Bounds (2-3);
advance target = `fit`. Symmetric with `onboard_mood`.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

from app.channels.onboarding_cards import build_color_card, build_fit_card
from app.graphs.nodes._onboard_stage import handle_stage_callback
from app.graphs.nodes._trace import node_done, node_enter
from app.graphs.state import WorkingState
from app.observability.langfuse import observe


# @MX:ANCHOR: [AUTO] Stage 2 callback entry point.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-CARDS-002 — same wire contract as mood; second card
#   in the deterministic onboarding sequence.
@observe(name="onboarding.stage.color", as_type="span")
async def onboard_color(state: WorkingState) -> dict:
    """Stage 2 color callback handler.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    node_enter("onboard_color")
    result = await handle_stage_callback(
        state,
        stage="color",
        next_stage="fit",
        build_card=build_color_card,
        build_next_card=build_fit_card,
    )
    node_done("onboard_color", stage=result.get("onboard_stage"))
    return result


__all__ = ["onboard_color"]
