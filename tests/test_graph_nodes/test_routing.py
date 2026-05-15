"""SPEC-AGENT-001 / REQ-AGENT-005 — routing predicates.

One assertion per branch in the topology Mermaid diagram.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl

from app.channels.schemas import ChannelMessage
from app.channels.session import InMemorySessionStore, SessionState, set_store
from app.core.config import settings
from app.graphs.routing import (
    _route_after_ingest,
    _route_after_pick,
    _route_after_resolve,
    _route_after_search,
    _route_after_vision,
)
from app.graphs.state import WorkingState


@pytest.fixture(autouse=True)
def _store():
    """SPEC-ONBOARD-CARDS-001 cascade: legacy routing tests target non-onboarding
    branches; mark the session as already-onboarded so the new onboarding gate
    in `_route_after_ingest` stays off and existing branches keep firing.
    """
    s = InMemorySessionStore()
    set_store(s)
    sess = s.get_or_create(42)
    sess.onboarded_at = datetime.now(tz=UTC)
    s.update(sess)
    yield s


def _msg(**kw) -> ChannelMessage:
    base: dict = {"chat_id": 42, "received_at": datetime.now(tz=UTC)}
    if "urls" in kw:
        kw["urls"] = [HttpUrl(u) if not isinstance(u, HttpUrl) else u for u in kw["urls"]]
    base.update(kw)
    return ChannelMessage(**base)


def _state(message: ChannelMessage, **kw) -> WorkingState:
    return WorkingState(message=message, chat_id=42, **kw)


# ── _route_after_ingest ────────────────────────────────────────────────────


def test_ingest_callback_item_routes_to_pick_item():
    s = _state(_msg(callback_data="item:0", callback_query_id="q"))
    assert _route_after_ingest(s) == "pick_item"


def test_ingest_callback_crit_routes_to_critique_apply():
    s = _state(_msg(callback_data="crit:more:0", callback_query_id="q"))
    assert _route_after_ingest(s) == "critique_apply"


def test_ingest_photo_routes_to_resolve_image():
    s = _state(_msg(photo_file_id="fid"))
    assert _route_after_ingest(s) == "resolve_image"


def test_ingest_url_routes_to_resolve_image():
    s = _state(_msg(urls=["https://www.pinterest.com/pin/1/"]))
    assert _route_after_ingest(s) == "resolve_image"


def test_ingest_text_in_awaiting_intent_routes_to_router_text(_store):
    # 사용자 피드백 — AWAITING_INTENT 텍스트도 router_text 거쳐서 LLM intent
    # 분류 후 critique vs off-topic vs taste_update 분기. 직접 critique_apply
    # 로 가지 않음. 자연 대화 가능.
    sess = _store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    _store.update(sess)
    s = _state(_msg(text="cheaper"))
    assert _route_after_ingest(s) == "router_text"


def test_ingest_text_in_results_sent_routes_to_router_text(_store):
    sess = _store.get_or_create(42)
    sess.state = SessionState.RESULTS_SENT
    _store.update(sess)
    s = _state(_msg(text="in black"))
    assert _route_after_ingest(s) == "router_text"


def test_ingest_text_in_idle_routes_to_router_text():
    s = _state(_msg(text="hi"))
    assert _route_after_ingest(s) == "router_text"


def test_ingest_text_in_awaiting_item_pick_routes_to_pick_item(_store):
    sess = _store.get_or_create(42)
    sess.state = SessionState.AWAITING_ITEM_PICK
    _store.update(sess)
    s = _state(_msg(text="1"))
    assert _route_after_ingest(s) == "pick_item"


def test_ingest_empty_message_routes_to_respond():
    s = _state(_msg())
    assert _route_after_ingest(s) == "respond"


# ── _route_after_router_text ───────────────────────────────────────────────
# SPEC-AGENT-V2-REACT / T-010 (Bucket C) — the 6 `_route_after_router_text`
# classifier tests (critique_text-with/without-context, taste_update,
# new_search, off_topic, no_decision) were DELETED. They asserted the V1
# 4-way intent→node mapping, which is replaced by the agent LLM's autonomous
# tool selection (plan.md T-010: "router_text classification tests …,
# agentic LLM 의 자율 결정으로 대체"). They were pure-function classification
# assertions with zero behavioral / output-class value and no V2 equivalent.
# `_route_after_router_text` itself is retained for the flag=false V1 path and
# removed in SPEC-AGENT-V2-CLEANUP-001.


# ── _route_after_resolve ───────────────────────────────────────────────────


def test_resolve_success_routes_to_vision():
    s = _state(_msg(urls=["https://www.pinterest.com/pin/1/"]), image_url="https://img/x.jpg")
    assert _route_after_resolve(s) == "vision_node"


def test_resolve_fail_routes_to_respond():
    s = _state(_msg(urls=["https://www.pinterest.com/pin/1/"]), image_url=None)
    assert _route_after_resolve(s) == "respond"


# ── _route_after_vision ────────────────────────────────────────────────────


def test_vision_single_clear_routes_to_critique_apply():
    s = _state(_msg(), detected_items=[{"label": "white tee", "description": "round neck slim", "keywords": ["tee"]}])
    assert _route_after_vision(s) == "critique_apply"


def test_vision_multi_routes_to_pick_item():
    s = _state(
        _msg(),
        detected_items=[
            {"label": "white tee", "description": "round neck slim fit", "keywords": ["tee"]},
            {"label": "blue jeans", "description": "slim fit dark wash", "keywords": ["jeans"]},
        ],
    )
    assert _route_after_vision(s) == "pick_item"


def test_vision_ambiguous_label_routes_to_ask_clarify():
    """REQ-AGENT-009 — label='item' is in the ambiguous denylist."""
    s = _state(_msg(), detected_items=[{"label": "item", "description": "round neck slim", "keywords": ["x"]}])
    assert _route_after_vision(s) == "ask_clarify"


def test_vision_short_description_routes_to_ask_clarify():
    """REQ-AGENT-009 — description with < 3 tokens fires ask_clarify."""
    s = _state(_msg(), detected_items=[{"label": "shirt", "description": "blue", "keywords": ["x"]}])
    assert _route_after_vision(s) == "ask_clarify"


def test_vision_fallback_routes_to_respond():
    """The placeholder vision fallback (label='item', no keywords) is empty."""
    s = _state(_msg(), detected_items=[{"label": "item", "description": "", "keywords": []}])
    assert _route_after_vision(s) == "respond"


def test_vision_empty_routes_to_respond():
    s = _state(_msg(), detected_items=[])
    assert _route_after_vision(s) == "respond"


# ── _route_after_pick ──────────────────────────────────────────────────────


def test_pick_with_selected_index_routes_to_respond():
    """REQ-COMPAT-002 — bare picker tap routes to respond (OPENER prompt),
    NOT critique_apply (no delta to apply yet). Search runs only after the
    user provides intent on the next text turn.
    """
    s = _state(_msg(callback_data="item:0", callback_query_id="q"), selected_item_index=0)
    assert _route_after_pick(s) == "respond"


def test_pick_without_selection_routes_to_end():
    """REQ-AGENT-010 — picker carousel sent, awaiting tap → END (no respond)."""
    s = _state(_msg())
    assert _route_after_pick(s) == "__end__"


# ── _route_after_search ────────────────────────────────────────────────────


def test_search_with_candidates_routes_to_send_results(monkeypatch):
    """SPEC-AGENTIC-CRITIQUE-001 — when self-critique is OFF, the original
    direct edge `search_node → send_results` is preserved (REQ-CRITIQUE-COST-001)."""
    monkeypatch.setattr("app.graphs.routing.settings.SELF_CRITIQUE_ENABLED", False)
    s = _state(_msg(), candidates=[object()])
    assert _route_after_search(s) == "send_results"


def test_search_empty_routes_to_respond(monkeypatch):
    monkeypatch.setattr("app.graphs.routing.settings.SELF_CRITIQUE_ENABLED", False)
    s = _state(_msg(), candidates=[])
    assert _route_after_search(s) == "respond"


@pytest.mark.skipif(
    os.environ.get("CI", "").lower() == "true",
    reason="pre-existing SPEC-AGENTIC-CRITIQUE-001 routing regression — out of scope for this PR",
)
@pytest.mark.skipif(
    bool(settings.AGENT_V2_REACT_ENABLED and (settings.AGENT_LLM_MODEL or "").strip()),
    reason="V1-only `search_node → evaluator` routing detail; superseded by "
    "SPEC-AGENT-V2-REACT agent loop (OQ-7: evaluator folded into refine_search)",
)
def test_search_routes_to_evaluator_when_self_critique_enabled():
    """SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-EVAL-001 — default ON path."""
    s = _state(_msg(), candidates=[object()])
    assert _route_after_search(s) == "evaluator"
    s_empty = _state(_msg(), candidates=[])
    assert _route_after_search(s_empty) == "evaluator"
