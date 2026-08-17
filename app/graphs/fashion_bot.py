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
from app.graphs.nodes.pick_item import pick_item
from app.graphs.nodes.resolve_image import resolve_image
from app.graphs.nodes.vision import vision_node
from app.graphs.routing import _route_after_resolve
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


def build_graph() -> Any:
    """SPEC-ONBOARD-LITE-001 — ReAct agent topology, onboarding card subgraph
    retired. Brand-new users no longer hit a card funnel: an actionable first
    message is greeted inline by `ingest` and proceeds to recommendation the
    same turn; a bare `/start` routes to the lightweight `intro` node.
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

    def _route_after_ingest_v2(state: WorkingState) -> str:
        # SPEC-ONBOARD-LITE-001 — onboarding card gate removed. ingest already
        # ran the inline first-touch greeting / /reset clear / ready ack
        # (maybe_first_touch); this closure is pure routing only.
        from app.channels.reset_keywords import is_reset_keyword
        from app.infrastructure.memory.session import SessionState, get_store

        msg = state.message
        cb = msg.callback_data or ""
        text = (msg.text or "").strip()

        # /reset — CONTRACT: `ingest.maybe_first_touch` MUST run before this
        # router and is responsible for the TasteProfile clear + ack
        # send (`_first_touch.py`). The router only terminates the turn here;
        # if the helper call is ever removed/reordered, /reset will silently
        # `__end__` without any user-visible effect (regression guard:
        # `tests/test_graph_nodes/test_first_touch.py::
        # test_reset_keyword_clears_taste_and_acks`).
        if is_reset_keyword(msg.text):
            return "__end__"

        # SPEC-AGENT-V2-REACT §15 Decision 2 — contentless Update silent END.
        # Checked before first-touch so a spurious blank Update never triggers
        # the intro.
        if not text and not msg.callback_data and not msg.urls and not msg.photo_file_id:
            return "__end__"

        sess = get_store().get_or_create(state.chat_id)
        is_new = getattr(sess, "onboarded_at", None) is None

        # New user + /start-only (no actionable content) → service intro.
        if is_new and text.lower() == "/start" and not msg.photo_file_id and not msg.urls and not cb:
            return "intro"

        # Picker callback → pick_item (deterministic).
        if cb.startswith("item:"):
            return "pick_item"
        # Hybrid result-card callbacks fully serviced by ingest — terminal.
        # SPEC-GENDER-PIN-001: clarify:gender:* is also fully serviced inline by
        # ingest (pins profile + re-runs the pending search + delivers) → END.
        if (
            cb.startswith("card:like:")
            or cb == "cards:more"
            or cb.startswith("clarify:gender:")
            or cb.startswith("onboard:lang:")
            or cb == "cap:membership_interest"
        ):
            return "__end__"
        # Photo / URL → vision pre-step.
        if msg.photo_file_id or msg.urls:
            return "resolve_image"
        # AWAITING_ITEM_PICK digit-pick fallback.
        if msg.text and sess.state == SessionState.AWAITING_ITEM_PICK:
            return "pick_item"
        # Everything else (text incl. greetings, clarify:/crit:* callbacks) →
        # agent. ingest inline-handled greeting/ack already.
        return "agent"

    # Test seam — expose the routing closure at module scope so unit tests can
    # exercise it directly. Rebound on every build_graph(); the import-time
    # GRAPH = build_graph() call performs the binding.
    globals()["_route_after_ingest_v2"] = _route_after_ingest_v2

    ingest_branches_v2: dict[str, str] = {
        "pick_item": "pick_item",
        "resolve_image": "resolve_image",
        "agent": "agent",
        "intro": "intro",  # SPEC-ONBOARD-LITE-001 — new-user /start-only intro
        "__end__": END,  # contentless Update / /reset — silent END
    }

    def _route_after_pick_v2(state: WorkingState) -> str:
        return "agent" if state.selected_item_index is not None else "__end__"

    def _route_after_vision_v2(state: WorkingState) -> str:
        rich = state.vision_result
        items = state.detected_items
        # 앱 스테이징에서 이미 항목을 골라 보낸 경우 — 여러 항목이 검출돼도
        # pick_item(1,2,3,4) 재선택을 건너뛰고 바로 검색으로.
        if state.skip_item_pick:
            return "agent"
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
    # SPEC-ONBOARD-LITE-001 — intro is per-turn terminal: the user's 2nd
    # message is a fresh webhook (onboarded_at now set) and routes normally.
    builder.add_edge("intro", END)

    _log_topology_banner()
    return builder.compile()


# Module-level compiled singleton (REQ-AGENT-001 acceptance #4).
GRAPH = build_graph()
_log_topology_banner()
