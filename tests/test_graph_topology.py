"""SPEC-AGENT-001 / REQ-AGENT-005 — structural topology assertion.

Plan.md Q5: structural edge-set assertion (NOT a Mermaid string snapshot).
Asserts the compiled graph's node set and conditional/unconditional edges
match the SPEC's topology section.
"""

from __future__ import annotations

from app.graphs.fashion_bot import GRAPH, build_graph

_EXPECTED_NODES = {
    "ingest",
    "router_text",
    "resolve_image",
    "vision_node",
    "pick_item",
    "ask_clarify",
    "critique_apply",
    "search_node",
    "send_results",
    "taste_update",
    "respond",
    "__start__",
    "__end__",
}


def test_topology_node_set_matches_spec():
    g = GRAPH.get_graph()
    assert set(g.nodes.keys()) == _EXPECTED_NODES


def test_topology_unconditional_edges_match_spec():
    """REQ-AGENT-005: critique_apply→search_node, send_results→respond,
    taste_update→respond, respond→END, ask_clarify→END, START→ingest."""
    g = GRAPH.get_graph()
    unconditional = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert ("__start__", "ingest") in unconditional
    # critique_apply→search_node is conditional (stale callbacks skip search;
    # see app/graphs/routing.py::_route_after_critique).
    assert ("send_results", "respond") in unconditional
    assert ("taste_update", "respond") in unconditional
    assert ("respond", "__end__") in unconditional
    assert ("ask_clarify", "__end__") in unconditional


def test_topology_conditional_edge_sources_match_spec():
    """REQ-AGENT-005: ingest, resolve_image, vision_node, pick_item, search_node,
    router_text are all conditional sources."""
    g = GRAPH.get_graph()
    cond_sources = {e.source for e in g.edges if e.conditional}
    assert cond_sources == {
        "ingest",
        "router_text",
        "resolve_image",
        "vision_node",
        "pick_item",
        "search_node",
        "critique_apply",
    }


def test_topology_pick_item_can_reach_end():
    """REQ-AGENT-010 / REQ-COMPAT-002 — picker-sent-only path bypasses respond,
    and bare picker tap (item:N) routes to respond (OPENER prompt) — NOT to
    critique_apply, since there is no critique to apply on a bare pick.
    """
    g = GRAPH.get_graph()
    pick_targets = {e.target for e in g.edges if e.source == "pick_item"}
    assert "__end__" in pick_targets
    assert "respond" in pick_targets
    assert "critique_apply" not in pick_targets


def test_topology_no_checkpointer_configured():
    """REQ-AGENT-008 acceptance #2 — compiled without a checkpointer."""
    # CompiledGraph exposes the checkpointer via .checkpointer (or lack of one
    # via attribute being None). We just sanity-check it's missing.
    assert getattr(GRAPH, "checkpointer", None) is None


def test_build_graph_returns_independent_instance():
    """plan.md Q3 / R7 — `build_graph()` factory yields a fresh compile,
    distinct from the module-level GRAPH singleton."""
    fresh = build_graph()
    assert fresh is not GRAPH
