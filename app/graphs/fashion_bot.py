"""SPEC-AGENT-001 / REQ-AGENT-001 + REQ-AGENT-005 — graph build & compile.

`build_graph()` is exposed as a public factory (tests use it for isolation).
`GRAPH` is the module-level compiled singleton — production code uses this
(REQ-AGENT-001 acceptance #4: compile cached at module level).

No checkpointer (REQ-AGENT-008): one webhook = one short-lived ainvoke().
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.graphs.nodes.apply_clarify import apply_clarify
from app.graphs.nodes.ask_clarify import ask_clarify
from app.graphs.nodes.critique_apply import critique_apply
from app.graphs.nodes.evaluator import evaluator
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
from app.graphs.nodes.respond import respond
from app.graphs.nodes.search import search_node
from app.graphs.nodes.send_results import send_results
from app.graphs.nodes.taste_update import taste_update
from app.graphs.nodes.vision import vision_node
from app.graphs.routing import (
    _route_after_critique,
    _route_after_evaluator,
    _route_after_ingest,
    _route_after_onboard_fit,
    _route_after_pick,
    _route_after_resolve,
    _route_after_router_text,
    _route_after_search,
    _route_after_vision,
)
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)


# Topology branch maps — each maps the routing function's return value to a
# node key understood by langgraph. `__end__` is the canonical sentinel for
# `END`; we translate it explicitly so routing predicates stay test-friendly.

_INGEST_BRANCHES: dict[str, str] = {
    "pick_item": "pick_item",
    # SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-CALLBACK-002 — clarify:* → apply_clarify.
    "apply_clarify": "apply_clarify",
    "critique_apply": "critique_apply",
    "resolve_image": "resolve_image",
    "router_text": "router_text",
    "respond": "respond",
    # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — onboarding gate
    # and continuous Pinterest path. Predicate priorities live in
    # `_route_after_ingest` (routing.py): onboarding gate first, then
    # continuous Pinterest, then existing branches.
    "onboard_intro": "onboard_intro",
    "onboard_mood": "onboard_mood",
    "onboard_color": "onboard_color",
    "onboard_fit": "onboard_fit",
    "onboard_pinterest": "onboard_pinterest",
    "pinterest_ingest": "pinterest_ingest",
}


# SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — fit branches on Pinterest flag.
_ONBOARD_FIT_BRANCHES: dict[str, str] = {
    "onboard_pinterest": "onboard_pinterest",
    "__end__": END,
}

_ROUTER_TEXT_BRANCHES: dict[str, str] = {
    "critique_apply": "critique_apply",
    "taste_update": "taste_update",
    "respond": "respond",
}

_RESOLVE_BRANCHES: dict[str, str] = {"vision_node": "vision_node", "respond": "respond"}

_VISION_BRANCHES: dict[str, str] = {
    "critique_apply": "critique_apply",
    "pick_item": "pick_item",
    "ask_clarify": "ask_clarify",
    "respond": "respond",
}

_PICK_BRANCHES: dict[str, str] = {"respond": "respond", "__end__": END}

_SEARCH_BRANCHES: dict[str, str] = {
    "send_results": "send_results",
    "respond": "respond",
    "evaluator": "evaluator",
}

_EVALUATOR_BRANCHES: dict[str, str] = {
    "apply_self_critique": "apply_self_critique",
    "send_results": "send_results",
    "respond": "respond",
}

_CRITIQUE_BRANCHES: dict[str, str] = {
    "search_node": "search_node",
    "respond": "respond",
    "__end__": END,  # SPEC-IMPLICIT-FB-001 / REQ-FB-CLICK-001 — silent click ack
}


async def _router_text_passthrough(state: WorkingState) -> dict:
    """Inline pass-through node — exists so we can attach a conditional edge to
    `_route_after_router_text` after the ingest branch routes here. The node
    itself does no work; the routing functions read `state.decision` written
    by `ingest`."""
    return {"log_events": ["router_text: pass-through"]}


async def _apply_self_critique_passthrough(state: WorkingState) -> dict:
    """SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-RETRY-001 — apply the
    evaluator's `suggested_delta` to `state.critique_delta` (so search_node
    picks it up via existing `_build_request` path), bump retry counter, and
    clear `critique_pending_delta` (single-use semantics).

    Translation contract: the evaluator's `CritiqueDelta` (in
    `_evaluator_models`) speaks the v1 broaden / refine / exclude vocabulary;
    the search node consumes the LEGACY user-driven `CritiqueDelta` (in
    `app.channels.critique`). We translate one into the other here so the
    search_node code stays untouched.
    """
    from app.channels.critique import CritiqueDelta as LegacyDelta
    from app.channels.session import get_store

    pending = state.critique_pending_delta
    breadcrumbs: list[str] = []
    if pending is None:
        breadcrumbs.append("apply_self_critique: no pending delta — passthrough")
        return {"log_events": breadcrumbs}

    sess = get_store().get_or_create(state.chat_id)
    # Drop session-level price filters when broaden delta requests it.
    if pending.drop_min_price:
        sess.user_intent = sess.user_intent  # noqa: PLW0127 — keep typed
    # Compose the legacy delta — exclude_brands/keywords/boost_keywords map 1:1.
    legacy = LegacyDelta(
        op="free_text",
        exclude_brands=list(pending.exclude_brands),
        exclude_keywords=list(pending.exclude_keywords),
        boost_keywords=list(pending.boost_keywords),
        color=pending.color_override,
        max_price=None,  # always None on retry — we BROADEN, never narrow on price
        min_price=None,
        extra_intent=None,
    )

    next_count = (state.critique_retry_count or 0) + 1
    breadcrumbs.append(f"apply_self_critique: intent={pending.intent} retry_count→{next_count}")
    return {
        "critique_delta": legacy,
        "critique_pending_delta": None,
        "critique_retry_count": next_count,
        "log_events": breadcrumbs,
    }


def _build_graph_v2() -> Any:
    """SPEC-AGENT-V2-REACT — ReAct agent topology.

    Onboarding subgraph (6 nodes) is preserved byte-identical. Post-onboarding
    text/photo/callback funnels through the `agent` node which runs the ReAct
    loop. Deprecated nodes (critique_apply, taste_update, respond, evaluator,
    router_text passthrough, apply_self_critique_passthrough) are NOT registered.
    """
    from app.graphs.nodes.agent import agent as agent_node

    builder = StateGraph(WorkingState)

    builder.add_node("ingest", ingest)
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
        from app.channels.session import SessionState, get_store
        from app.graphs.routing import (
            _resolve_onboard_stage_target,
            is_continuous_pinterest,
            onboarding_required,
        )

        sess = get_store().get_or_create(state.chat_id)
        if onboarding_required(state, sess):
            return _resolve_onboard_stage_target(sess, state)
        if is_continuous_pinterest(state, sess):
            return "pinterest_ingest"

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
        # Everything else (text, clarify:* / crit:* callbacks for onboarded users)
        # goes to the agent. ingest.Step C inline-handled boost_keywords for
        # clarify:* before we arrive here.
        return "agent"

    ingest_branches_v2: dict[str, str] = {
        "pick_item": "pick_item",
        "resolve_image": "resolve_image",
        "agent": "agent",
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
    # Onboarding edges — preserved.
    builder.add_edge("onboard_intro", END)
    builder.add_edge("onboard_mood", END)
    builder.add_edge("onboard_color", END)
    builder.add_conditional_edges("onboard_fit", _route_after_onboard_fit, _ONBOARD_FIT_BRANCHES)
    builder.add_edge("onboard_pinterest", END)
    builder.add_edge("pinterest_ingest", END)

    return builder.compile()


def build_graph() -> Any:
    """Construct and compile a fresh StateGraph instance (test-friendly).

    SPEC-AGENT-V2-REACT — `AGENT_V2_REACT_ENABLED` + `AGENT_LLM_MODEL` both
    configured → V2 (ReAct agent) topology. Otherwise V1 topology preserved
    byte-identical.
    """
    if settings.AGENT_V2_REACT_ENABLED and (settings.AGENT_LLM_MODEL or "").strip():
        return _build_graph_v2()

    builder = StateGraph(WorkingState)

    builder.add_node("ingest", ingest)
    builder.add_node("router_text", _router_text_passthrough)
    builder.add_node("resolve_image", resolve_image)
    builder.add_node("vision_node", vision_node)
    builder.add_node("pick_item", pick_item)
    builder.add_node("ask_clarify", ask_clarify)
    # SPEC-CLARIFY-CARDS-001 — apply_clarify 노드 등록.
    builder.add_node("apply_clarify", apply_clarify)
    builder.add_node("critique_apply", critique_apply)
    builder.add_node("search_node", search_node)
    builder.add_node("send_results", send_results)
    builder.add_node("taste_update", taste_update)
    builder.add_node("respond", respond)
    # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — 6 onboarding nodes.
    # Always registered; entry is gated by `_route_after_ingest` predicates
    # (onboarding_required / is_continuous_pinterest). Existing 12 nodes
    # remain byte-identical (REQ-ONBOARD-GRAPH-001 AC).
    builder.add_node("onboard_intro", onboard_intro)
    builder.add_node("onboard_mood", onboard_mood)
    builder.add_node("onboard_color", onboard_color)
    builder.add_node("onboard_fit", onboard_fit)
    builder.add_node("onboard_pinterest", onboard_pinterest)
    builder.add_node("pinterest_ingest", pinterest_ingest)
    # SPEC-AGENTIC-CRITIQUE-001 — register self-critique nodes only when the
    # feature flag is on (REQ-CRITIQUE-COST-001 — disable produces byte-identical
    # pre-SPEC topology).
    if settings.SELF_CRITIQUE_ENABLED:
        builder.add_node("evaluator", evaluator)
        builder.add_node("apply_self_critique", _apply_self_critique_passthrough)

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges("ingest", _route_after_ingest, _INGEST_BRANCHES)
    builder.add_conditional_edges("router_text", _route_after_router_text, _ROUTER_TEXT_BRANCHES)
    builder.add_conditional_edges("resolve_image", _route_after_resolve, _RESOLVE_BRANCHES)
    builder.add_conditional_edges("vision_node", _route_after_vision, _VISION_BRANCHES)
    builder.add_conditional_edges("pick_item", _route_after_pick, _PICK_BRANCHES)
    if settings.SELF_CRITIQUE_ENABLED:
        builder.add_conditional_edges("search_node", _route_after_search, _SEARCH_BRANCHES)
        builder.add_conditional_edges("evaluator", _route_after_evaluator, _EVALUATOR_BRANCHES)
        # apply_self_critique is a passthrough — always loops back to search_node.
        builder.add_edge("apply_self_critique", "search_node")
    else:
        # Pre-SPEC topology: search_node → send_results | respond directly.
        builder.add_conditional_edges(
            "search_node",
            _route_after_search,
            {"send_results": "send_results", "respond": "respond"},
        )
    builder.add_conditional_edges("critique_apply", _route_after_critique, _CRITIQUE_BRANCHES)
    builder.add_edge("send_results", "respond")
    builder.add_edge("taste_update", "respond")
    builder.add_edge("respond", END)
    builder.add_edge("ask_clarify", END)
    # SPEC-CLARIFY-CARDS-001 — apply_clarify 는 항상 search_node 로(unconditional).
    builder.add_edge("apply_clarify", "search_node")

    # ── SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — onboarding edges ──
    # @MX:SPEC: SPEC-ONBOARD-CARDS-001
    # @MX:REASON: REQ-ONBOARD-GRAPH-001 — each onboarding node is per-turn
    #   terminal (the user's next callback is a separate webhook → fresh graph
    #   run; ingest gate re-dispatches). The "topology" therefore lives in
    #   `_route_after_ingest` predicates (ONB-T18), and these edges merely
    #   terminate each node so the graph never deadlocks.
    builder.add_edge("onboard_intro", END)
    builder.add_edge("onboard_mood", END)
    builder.add_edge("onboard_color", END)
    # `onboard_fit` may need to branch when Pinterest is disabled — both branches
    # currently lead to END (completion is internal to the node body), but the
    # conditional edge is wired explicitly to satisfy the plan §6.2 #4+#5 entry
    # and to keep room for future "fit → onboard_pinterest" intra-turn routing.
    builder.add_conditional_edges("onboard_fit", _route_after_onboard_fit, _ONBOARD_FIT_BRANCHES)
    builder.add_edge("onboard_pinterest", END)
    builder.add_edge("pinterest_ingest", END)

    return builder.compile()


# Module-level compiled singleton (REQ-AGENT-001 acceptance #4).
GRAPH = build_graph()
