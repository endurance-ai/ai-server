"""SPEC-AGENT-V2-REACT / T-005 — `agent` graph node.

Thin LangGraph node wrapping `run_react_loop`. Reads Session via `get_store`,
returns a state delta dict compatible with LangGraph's merge.

@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


@observe(name="node.agent", as_type="span")
async def agent(state: WorkingState) -> dict[str, Any]:
    from app.agents.react_loop import run_react_loop

    sess = get_store().get_or_create(state.chat_id)
    try:
        delta = await run_react_loop(state, sess)
    except Exception as exc:  # noqa: BLE001 — never propagate to graph
        logger.exception("[node.agent] react_loop raised — degrading to exhausted")
        delta = {
            "agent_iterations": 0,
            "agent_status": "exhausted",
            "tool_call_history": [],
            "response_text": None,
            "exhausted_reason": f"node_exception:{type(exc).__name__}",
        }

    breadcrumbs = [
        f"agent: iters={delta.get('agent_iterations')} status={delta.get('agent_status')}",
    ]
    return {
        "agent_iterations": delta.get("agent_iterations", 0),
        "agent_status": delta.get("agent_status", "exhausted"),
        "tool_call_history": delta.get("tool_call_history", []),
        "response_text": delta.get("response_text"),
        "log_events": breadcrumbs,
        # P1-3: real turn_no from state (matches react_loop.py emit convention
        # `state.turn_no or 1`) instead of the placeholder literal 10.
        "turn_no": (state.turn_no or 0) + 1,
    }
