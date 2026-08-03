"""SPEC-AGENT-001 / REQ-AGENT-005 — routing predicates.

SPEC-AGENT-V2-CLEANUP-001 — the V1 routing orchestration functions
(`_route_after_ingest`, `_route_after_router_text`, `_route_after_search`,
`_route_after_pick`, `_route_after_vision`) were removed (the ReAct agent is
the only topology; the live routing lives in the inline `_route_after_*_v2`
closures in `fashion_bot.py`). The remaining live, predicate-level routing
function is `_route_after_resolve`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import HttpUrl

from app.channels.schemas import ChannelMessage
from app.graphs.routing import _route_after_resolve
from app.graphs.state import WorkingState


def _msg(**kw) -> ChannelMessage:
    base: dict = {"chat_id": 42, "received_at": datetime.now(tz=UTC)}
    if "urls" in kw:
        kw["urls"] = [HttpUrl(u) if not isinstance(u, HttpUrl) else u for u in kw["urls"]]
    base.update(kw)
    return ChannelMessage(**base)


def _state(message: ChannelMessage, **kw) -> WorkingState:
    return WorkingState(message=message, chat_id=42, **kw)


# ── _route_after_resolve ───────────────────────────────────────────────────


def test_resolve_success_routes_to_vision():
    s = _state(_msg(urls=["https://www.pinterest.com/pin/1/"]), image_url="https://img/x.jpg")
    assert _route_after_resolve(s) == "vision_node"


def test_resolve_fail_routes_to_respond():
    s = _state(_msg(urls=["https://www.pinterest.com/pin/1/"]), image_url=None)
    assert _route_after_resolve(s) == "respond"
