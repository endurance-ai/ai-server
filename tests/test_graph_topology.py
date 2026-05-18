"""SPEC-AGENT-001 / REQ-AGENT-005 — structural topology assertion.

Plan.md Q5: structural edge-set assertion (NOT a Mermaid string snapshot).
Asserts the compiled graph's node set and conditional/unconditional edges
match the SPEC's topology section.

SPEC-AGENT-V2-CLEANUP-001 — the ReAct agent is the only topology. The
post-onboarding fan-out is a single ``agent`` ReAct node; the deprecated V1
set (router_text passthrough, critique_apply, taste_update, respond, evaluator,
send_results, search_node, apply_self_critique) is no longer registered.
"""

from __future__ import annotations

from app.graphs.fashion_bot import GRAPH, build_graph

# SPEC-AGENT-V2-CLEANUP-001 — the (only) ReAct agent topology node set. `intro`
# is always registered by build_graph (the first-touch service intro for the
# onboarding-cards-OFF path).
_EXPECTED_NODES = {
    "ingest",
    "intro",
    "resolve_image",
    "vision_node",
    "pick_item",
    # ask_clarify / apply_clarify are still *registered* (route clarify
    # callbacks into `agent` via the inline ingest closure).
    "ask_clarify",
    "apply_clarify",
    "agent",
    # Onboarding subgraph — preserved.
    "onboard_intro",
    "onboard_mood",
    "onboard_color",
    "onboard_fit",
    "onboard_pinterest",
    "pinterest_ingest",
    "__start__",
    "__end__",
}

# Deprecated V1 nodes that MUST be absent (REQ-AGENT-TOPOLOGY-SUPERSEDE-001).
_DEPRECATED_V1_NODES = {
    "router_text",
    "critique_apply",
    "taste_update",
    "respond",
    "search_node",
    "send_results",
    "evaluator",
    "apply_self_critique",
}


def test_topology_node_set_matches_spec():
    g = GRAPH.get_graph()
    assert set(g.nodes.keys()) == _EXPECTED_NODES
    assert _DEPRECATED_V1_NODES.isdisjoint(g.nodes.keys())


def test_topology_unconditional_edges_match_spec():
    """SPEC-AGENT-V2-CLEANUP-001 — agent terminus + preserved onboarding edges.
    The V1 unconditional edges (send_results→respond, taste_update→respond,
    respond→END) no longer exist.
    """
    g = GRAPH.get_graph()
    unconditional = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert ("__start__", "ingest") in unconditional
    assert ("agent", "__end__") in unconditional
    assert ("pinterest_ingest", "__end__") in unconditional
    assert ("intro", "__end__") in unconditional
    for onboard in ("onboard_intro", "onboard_mood", "onboard_color", "onboard_pinterest"):
        assert (onboard, "__end__") in unconditional
    # Deprecated V1 edges must be gone.
    assert ("send_results", "respond") not in unconditional
    assert ("taste_update", "respond") not in unconditional
    assert ("respond", "__end__") not in unconditional


def test_topology_conditional_edge_sources_match_spec():
    """SPEC-AGENT-V2-CLEANUP-001 — conditional sources are the deterministic
    pre-agent funnel (ingest, resolve_image, vision_node, pick_item) plus the
    preserved onboard_fit branch.
    """
    g = GRAPH.get_graph()
    cond_sources = {e.source for e in g.edges if e.conditional}
    assert cond_sources == {
        "ingest",
        "resolve_image",
        "vision_node",
        "pick_item",
        "onboard_fit",
    }


def test_topology_pick_item_can_reach_end():
    """SPEC-AGENT-V2-CLEANUP-001 — the bare-pick destination is the ``agent``
    node; the picker-sent-only path still reaches END. ``critique_apply`` is
    no longer a pick_item target.
    """
    g = GRAPH.get_graph()
    pick_targets = {e.target for e in g.edges if e.source == "pick_item"}
    assert "__end__" in pick_targets
    assert "critique_apply" not in pick_targets
    assert "agent" in pick_targets
    assert "respond" not in pick_targets


def test_topology_no_checkpointer_configured():
    """REQ-AGENT-008 acceptance #2 — compiled without a checkpointer."""
    assert getattr(GRAPH, "checkpointer", None) is None


def test_build_graph_returns_independent_instance():
    """plan.md Q3 / R7 — `build_graph()` factory yields a fresh compile,
    distinct from the module-level GRAPH singleton."""
    fresh = build_graph()
    assert fresh is not GRAPH
