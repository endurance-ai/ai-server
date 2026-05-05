"""SPEC-AGENT-001 / REQ-AGENT-005 — routing functions for the StateGraph.

Each function is pure: `(state: WorkingState) -> str` returning the next node
name. They are registered via `add_conditional_edges` in `fashion_bot.py`.

Topology (REQ-AGENT-005):

  ingest →
      pick_item        (callback `item:N`)
      critique_apply   (callback `crit:*` OR text in AWAITING_INTENT)
      resolve_image    (photo OR url)
      router_text      (text in RESULTS_SENT/IDLE → router_text_decision branch)
      respond          (anything else, e.g. PICK fallback)

  router_text →
      critique_apply   (intent=critique_text)
      taste_update     (intent=taste_update)
      respond          (intent=new_search_request | off_topic | (no last_results context))

  resolve_image →
      vision_node      (image_url set)
      respond          (image_url=None → link_fail)

  vision_node →
      critique_apply   (single+clear)
      pick_item        (multi)
      ask_clarify      (ambiguous label OR short description — REQ-AGENT-009)
      respond          (vision fallback / empty)

  pick_item →
      critique_apply   (selected_item_index set)
      END              (picker carousel sent only — REQ-AGENT-010)

  search_node →
      send_results     (candidates non-empty)
      respond          (candidates empty)

The routing predicates inspect only `WorkingState` — they never touch the
adapter or the session store. Side effects belong to nodes.
"""

from __future__ import annotations

from app.channels.router import RoutedIntent
from app.channels.session import SessionState, get_store
from app.core.config import settings
from app.graphs.state import WorkingState


def _route_after_ingest(state: WorkingState) -> str:
    msg = state.message
    cb = msg.callback_data or ""
    if cb.startswith("item:"):
        return "pick_item"
    if cb.startswith("crit:"):
        return "critique_apply"

    # Session-state-aware text routing
    sess = get_store().get_or_create(state.chat_id)
    if msg.text and sess.state == SessionState.AWAITING_INTENT and not msg.photo_file_id:
        return "critique_apply"

    if msg.photo_file_id or msg.urls:
        return "resolve_image"

    if msg.text and sess.state in (SessionState.RESULTS_SENT, SessionState.IDLE):
        return "router_text"

    if msg.text and sess.state == SessionState.AWAITING_ITEM_PICK:
        # Digit-pick fallback: pick_item handles re-rendering carousel if invalid.
        return "pick_item"

    # Nothing else to do → polite reply.
    return "respond"


def _route_after_router_text(state: WorkingState) -> str:
    decision = state.decision
    if decision is None:
        return "respond"
    if decision.intent == RoutedIntent.CRITIQUE_TEXT:
        # Critique requires prior context (image_url + last_results); without
        # it the node will short-circuit to respond via the empty-delta path.
        sess = get_store().get_or_create(state.chat_id)
        if not sess.image_url or not sess.vision_item:
            return "respond"
        return "critique_apply"
    if decision.intent == RoutedIntent.TASTE_UPDATE:
        return "taste_update"
    # NEW_SEARCH_REQUEST and OFF_TOPIC both terminate at respond.
    return "respond"


def _route_after_resolve(state: WorkingState) -> str:
    if state.image_url:
        return "vision_node"
    return "respond"


def _is_vision_fallback(items: list[dict]) -> bool:
    """Mirror `scenario._is_vision_fallback` — single placeholder item."""
    if len(items) != 1:
        return False
    only = items[0]
    label = (only.get("label") or "").strip().lower()
    keywords = only.get("keywords") or []
    return label == "item" and not keywords


def _is_weak_vision(items: list[dict]) -> bool:
    """REQ-AGENT-009 — fires ask_clarify only on:
    - primary item description shorter than ASK_CLARIFY_MIN_DESC_TOKENS, OR
    - primary item label in ASK_CLARIFY_AMBIGUOUS_LABELS.

    Multi-item dominates: only checked for the single-item case (multi → picker).
    """
    if len(items) != 1:
        return False
    only = items[0]
    label = (only.get("label") or "").strip().lower()
    if label in settings.ask_clarify_ambiguous_labels:
        return True
    desc = (only.get("description") or "").strip()
    desc_tokens = [t for t in desc.split() if t]
    if len(desc_tokens) < settings.ASK_CLARIFY_MIN_DESC_TOKENS:
        return True
    return False


def _route_after_vision(state: WorkingState) -> str:
    items = state.detected_items
    if not items or _is_vision_fallback(items):
        return "respond"
    if len(items) > 1:
        return "pick_item"
    if _is_weak_vision(items):
        return "ask_clarify"
    return "critique_apply"


def _route_after_pick(state: WorkingState) -> str:
    """REQ-AGENT-010 — picker-sent-only path bypasses respond.

    When `selected_item_index` is set, the user has tapped a choice within the
    same webhook → continue to critique_apply. Otherwise the carousel was just
    sent and we end (waiting for the user's tap on the next webhook).
    """
    if state.selected_item_index is not None:
        return "critique_apply"
    return "__end__"


def _route_after_search(state: WorkingState) -> str:
    if state.candidates:
        return "send_results"
    return "respond"


def _route_after_critique(state: WorkingState) -> str:
    """Skip search when no delta was produced (stale callback / empty text).

    REQ-COMPAT-001: a stale `crit:*` callback (parse_callback returns None)
    must NOT trigger a re-search — the toast is the only user-visible side
    effect, and the graph terminates at respond.
    """
    if state.critique_delta is None:
        return "respond"
    return "search_node"
