"""SPEC-AGENT-001 / REQ-AGENT-005 — routing predicates.

One assertion per branch in the topology Mermaid diagram.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl

from app.channels.critique import CritiqueDelta
from app.channels.router import RoutedDecision, RoutedIntent
from app.channels.schemas import ChannelMessage
from app.channels.session import InMemorySessionStore, SessionState, set_store
from app.graphs.routing import (
    _route_after_ingest,
    _route_after_pick,
    _route_after_resolve,
    _route_after_router_text,
    _route_after_search,
    _route_after_vision,
)
from app.graphs.state import WorkingState


@pytest.fixture(autouse=True)
def _store():
    s = InMemorySessionStore()
    set_store(s)
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


def test_ingest_text_in_awaiting_intent_routes_to_critique_apply(_store):
    sess = _store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    _store.update(sess)
    s = _state(_msg(text="cheaper"))
    assert _route_after_ingest(s) == "critique_apply"


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


def test_router_critique_text_with_context_routes_to_critique_apply(_store):
    sess = _store.get_or_create(42)
    sess.image_url = "https://i.pinimg.com/x.jpg"
    sess.vision_item = "tee"
    _store.update(sess)
    s = _state(
        _msg(text="cheaper"),
        decision=RoutedDecision(intent=RoutedIntent.CRITIQUE_TEXT, critique_delta=CritiqueDelta(op="free_text")),
    )
    assert _route_after_router_text(s) == "critique_apply"


def test_router_critique_text_without_context_routes_to_respond():
    s = _state(
        _msg(text="cheaper"),
        decision=RoutedDecision(intent=RoutedIntent.CRITIQUE_TEXT, critique_delta=CritiqueDelta(op="free_text")),
    )
    assert _route_after_router_text(s) == "respond"


def test_router_taste_update_routes_to_taste_update():
    s = _state(_msg(text="i love ami"), decision=RoutedDecision(intent=RoutedIntent.TASTE_UPDATE))
    assert _route_after_router_text(s) == "taste_update"


def test_router_new_search_routes_to_respond():
    s = _state(_msg(text="show me beige tees"), decision=RoutedDecision(intent=RoutedIntent.NEW_SEARCH_REQUEST))
    assert _route_after_router_text(s) == "respond"


def test_router_off_topic_routes_to_respond():
    s = _state(_msg(text="hi"), decision=RoutedDecision(intent=RoutedIntent.OFF_TOPIC))
    assert _route_after_router_text(s) == "respond"


def test_router_no_decision_routes_to_respond():
    s = _state(_msg(text="???"))
    assert _route_after_router_text(s) == "respond"


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


def test_pick_with_selected_index_routes_to_critique_apply():
    s = _state(_msg(callback_data="item:0", callback_query_id="q"), selected_item_index=0)
    assert _route_after_pick(s) == "critique_apply"


def test_pick_without_selection_routes_to_end():
    """REQ-AGENT-010 — picker carousel sent, awaiting tap → END (no respond)."""
    s = _state(_msg())
    assert _route_after_pick(s) == "__end__"


# ── _route_after_search ────────────────────────────────────────────────────


def test_search_with_candidates_routes_to_send_results():
    s = _state(_msg(), candidates=[object()])
    assert _route_after_search(s) == "send_results"


def test_search_empty_routes_to_respond():
    s = _state(_msg(), candidates=[])
    assert _route_after_search(s) == "respond"
