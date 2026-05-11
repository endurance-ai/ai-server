"""REQ-OBS-TRACE-LOOP-001 — evaluator span carries `iteration` metadata."""

from __future__ import annotations

import inspect


def test_evaluator_calls_update_current_span_with_iteration() -> None:
    from app.graphs.nodes import evaluator as ev_module

    src = inspect.getsource(ev_module)
    # Must invoke v3 client API to attach `iteration` mid-function.
    assert "update_current_span" in src, (
        "evaluator must call `update_current_span(metadata={'iteration': ...})` per REQ-OBS-TRACE-LOOP-001"
    )
    assert '"iteration"' in src or "'iteration'" in src
