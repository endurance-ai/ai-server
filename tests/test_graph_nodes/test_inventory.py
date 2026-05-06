"""SPEC-AGENT-001 / REQ-AGENT-004 acceptance #4 — one test file per node."""

from __future__ import annotations

import importlib
import pkgutil

import app.graphs.nodes as nodes_pkg


def test_ten_nodes_present():
    """REQ-AGENT-004 — exactly 10 node modules under app/graphs/nodes/.

    Excludes the private `_adapter_ctx` (ContextVar plumbing) and __init__.
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
            "critique_apply",
            "search",
            "send_results",
            "taste_update",
            "respond",
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
        "critique_apply": "critique_apply",
        "search": "search_node",
        "send_results": "send_results",
        "taste_update": "taste_update",
        "respond": "respond",
    }
    for module_name, fn_name in name_map.items():
        mod = importlib.import_module(f"app.graphs.nodes.{module_name}")
        fn = getattr(mod, fn_name)
        assert inspect.iscoroutinefunction(fn), f"{module_name}.{fn_name} must be async def"
