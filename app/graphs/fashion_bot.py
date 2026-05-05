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

from app.graphs.nodes.ask_clarify import ask_clarify
from app.graphs.nodes.critique_apply import critique_apply
from app.graphs.nodes.ingest import ingest
from app.graphs.nodes.pick_item import pick_item
from app.graphs.nodes.resolve_image import resolve_image
from app.graphs.nodes.respond import respond
from app.graphs.nodes.search import search_node
from app.graphs.nodes.send_results import send_results
from app.graphs.nodes.taste_update import taste_update
from app.graphs.nodes.vision import vision_node
from app.graphs.routing import (
    _route_after_critique,
    _route_after_ingest,
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
    "critique_apply": "critique_apply",
    "resolve_image": "resolve_image",
    "router_text": "router_text",
    "respond": "respond",
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

_PICK_BRANCHES: dict[str, str] = {"critique_apply": "critique_apply", "__end__": END}

_SEARCH_BRANCHES: dict[str, str] = {"send_results": "send_results", "respond": "respond"}

_CRITIQUE_BRANCHES: dict[str, str] = {"search_node": "search_node", "respond": "respond"}


async def _router_text_passthrough(state: WorkingState) -> dict:
    """Inline pass-through node — exists so we can attach a conditional edge to
    `_route_after_router_text` after the ingest branch routes here. The node
    itself does no work; the routing functions read `state.decision` written
    by `ingest`."""
    return {"log_events": ["router_text: pass-through"]}


def build_graph() -> Any:
    """Construct and compile a fresh StateGraph instance (test-friendly)."""
    builder = StateGraph(WorkingState)

    builder.add_node("ingest", ingest)
    builder.add_node("router_text", _router_text_passthrough)
    builder.add_node("resolve_image", resolve_image)
    builder.add_node("vision_node", vision_node)
    builder.add_node("pick_item", pick_item)
    builder.add_node("ask_clarify", ask_clarify)
    builder.add_node("critique_apply", critique_apply)
    builder.add_node("search_node", search_node)
    builder.add_node("send_results", send_results)
    builder.add_node("taste_update", taste_update)
    builder.add_node("respond", respond)

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges("ingest", _route_after_ingest, _INGEST_BRANCHES)
    builder.add_conditional_edges("router_text", _route_after_router_text, _ROUTER_TEXT_BRANCHES)
    builder.add_conditional_edges("resolve_image", _route_after_resolve, _RESOLVE_BRANCHES)
    builder.add_conditional_edges("vision_node", _route_after_vision, _VISION_BRANCHES)
    builder.add_conditional_edges("pick_item", _route_after_pick, _PICK_BRANCHES)
    builder.add_conditional_edges("search_node", _route_after_search, _SEARCH_BRANCHES)
    builder.add_conditional_edges("critique_apply", _route_after_critique, _CRITIQUE_BRANCHES)
    builder.add_edge("send_results", "respond")
    builder.add_edge("taste_update", "respond")
    builder.add_edge("respond", END)
    builder.add_edge("ask_clarify", END)

    return builder.compile()


# Module-level compiled singleton (REQ-AGENT-001 acceptance #4).
GRAPH = build_graph()
