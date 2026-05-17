"""Onboarding Stage 1 — mood card callback handler.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-002 + REQ-ONBOARD-OBS-001.

Handles `onboard:mood:{toggle|done|skip}[:value]` callbacks. Toggle re-renders
the card in place; done advances to `onboard_color` (bounds 2-3); skip empties
selection and advances. Emits `onboard_select` on advance.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

from app.channels.onboarding_cards import build_color_card, build_mood_card
from app.graphs.nodes._onboard_stage import handle_stage_callback
from app.graphs.nodes._trace import node_done, node_enter
from app.graphs.state import WorkingState
from app.observability.langfuse import observe


# @MX:ANCHOR: [AUTO] Stage 1 callback entry point.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-CARDS-002 — toggle/done/skip wire contract pinned by
#   test_onboard_nodes.py. Routing edge `onboard_intro -> onboard_mood -> onboard_color`
#   depends on the next-stage return value.
@observe(name="onboarding.stage.mood", as_type="span")
async def onboard_mood(state: WorkingState) -> dict:
    """Stage 1 mood callback handler.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    node_enter("onboard_mood")
    result = await handle_stage_callback(
        state,
        stage="mood",
        next_stage="color",
        build_card=build_mood_card,
        build_next_card=build_color_card,
    )
    node_done("onboard_mood", stage=result.get("onboard_stage"))
    return result


__all__ = ["onboard_mood"]
