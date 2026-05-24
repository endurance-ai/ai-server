"""SPEC-AGENT-001 / REQ-AGENT-004 acceptance #4 — one test file per node."""

from __future__ import annotations

import importlib
import pkgutil

import app.graphs.nodes as nodes_pkg


def test_node_inventory_matches_v2_topology():
    """SPEC-AGENT-V2-CLEANUP-001 — the V1-only node modules (critique_apply,
    search, taste_update, respond, evaluator, send_results) were deleted with
    the V1 topology.

    Excludes private modules (`_adapter_ctx`, `_first_touch`, `_trace`) and __init__.

    SPEC-ONBOARD-LITE-001 — the onboarding card node modules
    (onboard_intro/mood/color/fit/pinterest, pinterest_ingest) were deleted
    with the card subgraph; only the lightweight `intro` node remains.
    """
    public = []
    for _, name, _ in pkgutil.iter_modules(nodes_pkg.__path__):
        if name.startswith("_"):
            continue
        public.append(name)
    public.sort()
    expected = sorted(
        [
            "ingest",
            "resolve_image",
            "vision",
            "pick_item",
            "ask_clarify",
            "apply_clarify",
            # SPEC-AGENT-V2-REACT — ReAct agent node.
            "agent",
            # SPEC-ONBOARD-LITE-001 — lightweight first-touch intro node
            # (onboarding card subgraph removed).
            "intro",
        ]
    )
    assert public == expected, f"node inventory drifted: {public}"


def test_each_node_exposes_async_callable():
    """REQ-AGENT-004 acceptance #2 — each node is a module-level async fn."""
    import inspect

    name_map = {
        "ingest": "ingest",
        "resolve_image": "resolve_image",
        "vision": "vision_node",
        "pick_item": "pick_item",
        "ask_clarify": "ask_clarify",
        "apply_clarify": "apply_clarify",
        "agent": "agent",
        "intro": "intro",
    }
    for module_name, fn_name in name_map.items():
        mod = importlib.import_module(f"app.graphs.nodes.{module_name}")
        fn = getattr(mod, fn_name)
        assert inspect.iscoroutinefunction(fn), f"{module_name}.{fn_name} must be async def"
