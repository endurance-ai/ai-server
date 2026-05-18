"""SPEC-AGENT-001 / REQ-AGENT-001 + REQ-AGENT-005 — graph build & compile.

`build_graph()` is exposed as a public factory (tests use it for isolation).
`GRAPH` is the module-level compiled singleton — production code uses this
(REQ-AGENT-001 acceptance #4: compile cached at module level).

No checkpointer (REQ-AGENT-008): one webhook = one short-lived ainvoke().

SPEC-AGENT-V2-CLEANUP-001 — the ReAct agent topology is now the PERMANENT,
ONLY topology. The legacy V1 (18-node) graph and the AGENT_V2_REACT_ENABLED /
AGENT_V3_* feature flags were removed.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graphs.nodes.apply_clarify import apply_clarify
from app.graphs.nodes.ask_clarify import ask_clarify
from app.graphs.nodes.ingest import ingest
from app.graphs.nodes.onboard_color import onboard_color
from app.graphs.nodes.onboard_fit import onboard_fit

# SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — 6 onboarding nodes.
from app.graphs.nodes.onboard_intro import onboard_intro
from app.graphs.nodes.onboard_mood import onboard_mood
from app.graphs.nodes.onboard_pinterest import onboard_pinterest
from app.graphs.nodes.pick_item import pick_item
from app.graphs.nodes.pinterest_ingest import pinterest_ingest
from app.graphs.nodes.resolve_image import resolve_image
from app.graphs.nodes.vision import vision_node
from app.graphs.routing import _route_after_onboard_fit, _route_after_resolve
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)


def _log_topology_banner() -> None:
    """Emit ONE prominent INFO line stating the resolved topology.

    Logging-only, never raises. SPEC-AGENT-V2-CLEANUP-001 — there is now a
    single topology (the ReAct agent), so this is a single unconditional line.
    """
    try:
        logger.info("🤖 [topology] ReAct agent (permanent) | memory+reflexion+proactive+dislike active")
    except Exception:  # noqa: BLE001 — banner must never break graph build
        pass


# SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — fit branches on Pinterest flag.
_ONBOARD_FIT_BRANCHES: dict[str, str] = {
    "onboard_pinterest": "onboard_pinterest",
    "__end__": END,
}


def build_graph() -> Any:
    """SPEC-AGENT-V2-CLEANUP-001 — ReAct agent topology (the only topology).

    Onboarding subgraph (6 nodes) is preserved. Post-onboarding text/photo/
    callback funnels through the `agent` node which runs the ReAct loop.
    """
    from app.graphs.nodes.agent import agent as agent_node
    from app.graphs.nodes.intro import intro as intro_node

    builder = StateGraph(WorkingState)

    builder.add_node("ingest", ingest)
    builder.add_node("intro", intro_node)
    builder.add_node("resolve_image", resolve_image)
    builder.add_node("vision_node", vision_node)
    builder.add_node("pick_item", pick_item)
    builder.add_node("ask_clarify", ask_clarify)
    builder.add_node("apply_clarify", apply_clarify)
    builder.add_node("agent", agent_node)
    # Onboarding subgraph — preserved.
    builder.add_node("onboard_intro", onboard_intro)
    builder.add_node("onboard_mood", onboard_mood)
    builder.add_node("onboard_color", onboard_color)
    builder.add_node("onboard_fit", onboard_fit)
    builder.add_node("onboard_pinterest", onboard_pinterest)
    builder.add_node("pinterest_ingest", pinterest_ingest)

    def _route_after_ingest_v2(state: WorkingState) -> str:
        # Onboarding gate FIRST (preserved).
        from app.graphs.routing import (
            _resolve_onboard_stage_target,
            first_touch_intro_required,
            is_continuous_pinterest,
            onboarding_required,
        )
        from app.infrastructure.memory.session import SessionState, get_store

        sess = get_store().get_or_create(state.chat_id)
        if onboarding_required(state, sess):
            return _resolve_onboard_stage_target(sess, state)
        if is_continuous_pinterest(state, sess):
            return "pinterest_ingest"
        # SPEC-AGENT-V2-REACT — onboarding-cards OFF: a brand-new user's FIRST
        # message gets the one-shot service intro instead of the agent. Gated
        # strictly on `onboarded_at IS NULL`; intro marks it set so the 2nd
        # message (re-sent link/text) flows normally. Placed after the
        # onboarding gate (unchanged when flag ON) and continuous-pinterest,
        # before item:/clarify:/photo/url/pick and the contentless `__end__`
        # guard (the predicate itself requires user signal).
        if first_touch_intro_required(state, sess):
            return "intro"

        msg = state.message
        cb = msg.callback_data or ""
        # Picker callback → pick_item (still deterministic).
        if cb.startswith("item:"):
            return "pick_item"
        # Photo / URL → vision pre-step.
        if msg.photo_file_id or msg.urls:
            return "resolve_image"
        # AWAITING_ITEM_PICK with digit-pick text — keep deterministic fallback.
        if msg.text and sess.state == SessionState.AWAITING_ITEM_PICK:
            return "pick_item"
        # SPEC-AGENT-V2-REACT §15 Decision 2 — empty/contentless input guard.
        # Telegram spuriously delivers contentless Updates (service messages,
        # stickers, blank-text echoes). These are NOT user turns: route to END
        # SILENTLY (no message sent) rather than spawning the agent on an empty
        # context (which hallucinates "Oops I can't recall"). All four must be
        # true: blank text AND no callback AND no urls AND no photo. Callbacks
        # (clarify:/crit:/onboard:) carry non-empty callback_data and were
        # routed by the positive branches above, so they never reach here.
        if not (msg.text or "").strip() and not msg.callback_data and not msg.urls and not msg.photo_file_id:
            return "__end__"
        # Everything else (text, clarify:* / crit:* callbacks for onboarded users)
        # goes to the agent. ingest.Step C inline-handled boost_keywords for
        # clarify:* before we arrive here.
        return "agent"

    ingest_branches_v2: dict[str, str] = {
        "pick_item": "pick_item",
        "resolve_image": "resolve_image",
        "agent": "agent",
        "intro": "intro",  # SPEC-AGENT-V2-REACT first-touch service intro (cards OFF)
        "__end__": END,  # SPEC-AGENT-V2-REACT §15 Decision 2 — silent END for contentless Update
        "pinterest_ingest": "pinterest_ingest",
        "onboard_intro": "onboard_intro",
        "onboard_mood": "onboard_mood",
        "onboard_color": "onboard_color",
        "onboard_fit": "onboard_fit",
        "onboard_pinterest": "onboard_pinterest",
    }

    def _route_after_pick_v2(state: WorkingState) -> str:
        return "agent" if state.selected_item_index is not None else "__end__"

    def _route_after_vision_v2(state: WorkingState) -> str:
        rich = state.vision_result
        items = state.detected_items
        if rich is not None and not getattr(rich, "isApparel", True):
            return "agent"
        if items and len(items) > 1:
            return "pick_item"
        return "agent"

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges("ingest", _route_after_ingest_v2, ingest_branches_v2)
    builder.add_conditional_edges(
        "resolve_image",
        _route_after_resolve,
        {"vision_node": "vision_node", "respond": "agent"},
    )
    builder.add_conditional_edges("vision_node", _route_after_vision_v2, {"pick_item": "pick_item", "agent": "agent"})
    builder.add_conditional_edges("pick_item", _route_after_pick_v2, {"agent": "agent", "__end__": END})
    builder.add_edge("apply_clarify", "agent")
    builder.add_edge("ask_clarify", END)
    builder.add_edge("agent", END)
    # SPEC-AGENT-V2-REACT — first-touch intro is per-turn terminal: the user's
    # 2nd message is a fresh webhook (new graph run) and onboarded_at is now
    # set, so the ingest gate routes it normally (agent / resolve_image).
    builder.add_edge("intro", END)
    # Onboarding edges — preserved.
    builder.add_edge("onboard_intro", END)
    builder.add_edge("onboard_mood", END)
    builder.add_edge("onboard_color", END)
    builder.add_conditional_edges("onboard_fit", _route_after_onboard_fit, _ONBOARD_FIT_BRANCHES)
    builder.add_edge("onboard_pinterest", END)
    builder.add_edge("pinterest_ingest", END)

    _log_topology_banner()
    return builder.compile()


# Module-level compiled singleton (REQ-AGENT-001 acceptance #4).
GRAPH = build_graph()
_log_topology_banner()
