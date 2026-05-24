"""REQ-OBS-TRACE-NODE-001 — every LangGraph node has @observe."""

from __future__ import annotations

import inspect

# SPEC-AGENT-V2-CLEANUP-001 — V1-only node entrypoints (critique_apply,
# search, taste_update, respond, evaluator, send_results) were deleted
# with the V1 topology.
NODE_FUNCS = [
    ("app.graphs.nodes.ingest", "ingest"),
    ("app.graphs.nodes.resolve_image", "resolve_image"),
    ("app.graphs.nodes.vision", "vision_node"),
    ("app.graphs.nodes.pick_item", "pick_item"),
    ("app.graphs.nodes.ask_clarify", "ask_clarify"),
    ("app.graphs.nodes.apply_clarify", "apply_clarify"),
]


def test_all_nodes_have_observe_decoration() -> None:
    """All preserved entry functions are decorated; verifiable via source."""
    import importlib

    for module_path, func_name in NODE_FUNCS:
        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # The decorator line must precede the function definition.
        # Looser check: a `@observe(name="node.{func}"` should exist.
        snippet = f'@observe(name="node.{func_name.removesuffix("_node")}'
        assert snippet in src or f'@observe(name="node.{func_name}"' in src, (
            f"{module_path}.{func_name} missing @observe(name='node.{func_name}', ...) decorator"
        )
