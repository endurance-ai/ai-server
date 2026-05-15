"""SPEC-AGENT-001 / REQ-AGENT-005 — structural topology assertion.

Plan.md Q5: structural edge-set assertion (NOT a Mermaid string snapshot).
Asserts the compiled graph's node set and conditional/unconditional edges
match the SPEC's topology section.

SPEC-AGENT-V2-REACT / T-010 (Bucket A): these structural assertions are now
flag-aware. Under ``AGENT_V2_REACT_ENABLED`` (+ ``AGENT_LLM_MODEL``) the
post-onboarding topology is replaced by a single ``agent`` ReAct node and the
deprecated V1 set (router_text passthrough, critique_apply, taste_update,
respond, evaluator, send_results, search_node, apply_self_critique) is NOT
registered. Both topologies are valid and must be guarded — neither branch is
deleted (REQ-AGENT-COMPAT-FLAG-001).
"""

from __future__ import annotations

from app.core.config import settings
from app.graphs.fashion_bot import GRAPH, build_graph

# A topology is "V2" when the feature flag is on AND the model is configured
# (fail-closed) — this mirrors ``fashion_bot.build_graph()`` exactly.
_V2_ACTIVE = bool(settings.AGENT_V2_REACT_ENABLED and (settings.AGENT_LLM_MODEL or "").strip())

_EXPECTED_NODES_BASE = {
    "ingest",
    "router_text",
    "resolve_image",
    "vision_node",
    "pick_item",
    "ask_clarify",
    # SPEC-CLARIFY-CARDS-001 — apply_clarify 노드.
    "apply_clarify",
    "critique_apply",
    "search_node",
    "send_results",
    "taste_update",
    "respond",
    # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — 6 onboarding nodes.
    "onboard_intro",
    "onboard_mood",
    "onboard_color",
    "onboard_fit",
    "onboard_pinterest",
    "pinterest_ingest",
    "__start__",
    "__end__",
}


# SPEC-AGENT-V2-REACT — node set when the V2 ReAct topology is active.
_EXPECTED_NODES_V2 = {
    "ingest",
    "resolve_image",
    "vision_node",
    "pick_item",
    # ask_clarify / apply_clarify are still *registered* (legacy nodes) but
    # unreachable under V2 (ingest routes clarify callbacks to `agent`).
    "ask_clarify",
    "apply_clarify",
    "agent",
    # Onboarding subgraph — preserved byte-identical.
    "onboard_intro",
    "onboard_mood",
    "onboard_color",
    "onboard_fit",
    "onboard_pinterest",
    "pinterest_ingest",
    "__start__",
    "__end__",
}

# Deprecated V1 nodes that MUST be absent under the V2 topology
# (REQ-AGENT-TOPOLOGY-SUPERSEDE-001).
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
    if _V2_ACTIVE:
        # SPEC-AGENT-V2-REACT — V2 ReAct topology.
        assert set(g.nodes.keys()) == _EXPECTED_NODES_V2
        assert _DEPRECATED_V1_NODES.isdisjoint(g.nodes.keys())
        return
    expected = set(_EXPECTED_NODES_BASE)
    if settings.SELF_CRITIQUE_ENABLED:
        # SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-EVAL-001
        expected |= {"evaluator", "apply_self_critique"}
    assert set(g.nodes.keys()) == expected


def test_topology_unconditional_edges_match_spec():
    """REQ-AGENT-005: critique_apply→search_node, send_results→respond,
    taste_update→respond, respond→END, ask_clarify→END, START→ingest.

    SPEC-AGENT-V2-REACT — under V2 the post-onboarding fan-out collapses into
    ``apply_clarify→agent`` and ``agent→END``; the V1 unconditional edges
    (send_results→respond, taste_update→respond, respond→END) no longer exist.
    """
    g = GRAPH.get_graph()
    unconditional = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert ("__start__", "ingest") in unconditional
    if _V2_ACTIVE:
        # SPEC-AGENT-V2-REACT — agent terminus + preserved onboarding edges.
        assert ("agent", "__end__") in unconditional
        assert ("pinterest_ingest", "__end__") in unconditional
        for onboard in ("onboard_intro", "onboard_mood", "onboard_color", "onboard_pinterest"):
            assert (onboard, "__end__") in unconditional
        # Deprecated V1 edges must be gone.
        assert ("send_results", "respond") not in unconditional
        assert ("taste_update", "respond") not in unconditional
        assert ("respond", "__end__") not in unconditional
        return
    # critique_apply→search_node is conditional (stale callbacks skip search;
    # see app/graphs/routing.py::_route_after_critique).
    assert ("send_results", "respond") in unconditional
    assert ("taste_update", "respond") in unconditional
    assert ("respond", "__end__") in unconditional
    assert ("ask_clarify", "__end__") in unconditional
    # SPEC-CLARIFY-CARDS-001 — apply_clarify → search_node (unconditional).
    assert ("apply_clarify", "search_node") in unconditional


def test_topology_conditional_edge_sources_match_spec():
    """REQ-AGENT-005: ingest, resolve_image, vision_node, pick_item, search_node,
    router_text are all conditional sources. SPEC-AGENTIC-CRITIQUE-001 adds
    `evaluator` as a conditional source when self-critique is enabled.

    SPEC-AGENT-V2-REACT — under V2 the conditional sources collapse to the
    deterministic pre-agent funnel (ingest, resolve_image, vision_node,
    pick_item) plus the preserved onboard_fit branch; router_text / search_node
    / critique_apply / evaluator are no longer conditional sources.
    """
    g = GRAPH.get_graph()
    cond_sources = {e.source for e in g.edges if e.conditional}
    if _V2_ACTIVE:
        assert cond_sources == {
            "ingest",
            "resolve_image",
            "vision_node",
            "pick_item",
            "onboard_fit",
        }
        return
    expected = {
        "ingest",
        "router_text",
        "resolve_image",
        "vision_node",
        "pick_item",
        "search_node",
        "critique_apply",
        # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-GRAPH-001 — onboard_fit branches
        # on PINTEREST_BOOTSTRAP_ENABLED.
        "onboard_fit",
    }
    if settings.SELF_CRITIQUE_ENABLED:
        expected |= {"evaluator"}
    assert cond_sources == expected


def test_topology_pick_item_can_reach_end():
    """REQ-AGENT-010 / REQ-COMPAT-002 — picker-sent-only path bypasses respond,
    and bare picker tap (item:N) routes to respond (OPENER prompt) — NOT to
    critique_apply, since there is no critique to apply on a bare pick.

    SPEC-AGENT-V2-REACT — under V2 the bare-pick destination is the ``agent``
    node (not ``respond``); the picker-sent-only path still reaches END.
    ``critique_apply`` remains unreachable from pick_item in both topologies.
    """
    g = GRAPH.get_graph()
    pick_targets = {e.target for e in g.edges if e.source == "pick_item"}
    assert "__end__" in pick_targets
    assert "critique_apply" not in pick_targets
    if _V2_ACTIVE:
        assert "agent" in pick_targets
        assert "respond" not in pick_targets
        return
    assert "respond" in pick_targets


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
