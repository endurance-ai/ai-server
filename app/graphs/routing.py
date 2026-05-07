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

from typing import Any

from app.channels.router import RoutedIntent
from app.channels.session import SessionState, get_store
from app.core.config import settings
from app.graphs.state import WorkingState


def _route_after_ingest(state: WorkingState) -> str:
    msg = state.message
    cb = msg.callback_data or ""
    if cb.startswith("item:"):
        return "pick_item"
    # SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-CALLBACK-002 — clarify 콜백 분기.
    if cb.startswith("clarify:"):
        return "apply_clarify"
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
    """Mirror `scenario._is_vision_fallback` — single placeholder item.

    On the v2 path, fallback is also detected when the underlying VisionResult
    has `isApparel=False` AND items is empty (REQ-VISION-COMPAT-004) — handled
    by the `_route_after_vision` caller via the `vision_result` check.
    """
    if len(items) != 1:
        return False
    only = items[0]
    label = (only.get("label") or "").strip().lower()
    keywords = only.get("keywords") or []
    return label == "item" and not keywords


def _is_weak_vision_v2(item: Any) -> bool:
    """REQ-VISION-WEAKVISION-001 — rich-schema predicate.

    Fires when ANY of these hold for the SELECTED item:
    - subcategory empty OR in ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES denylist
    - fit empty OR not in the documented fit enum
    - colorFamily empty
    - searchQuery token count below ASK_CLARIFY_MIN_QUERY_TOKENS
    """
    if item is None:
        return True

    subcat = (getattr(item, "subcategory", "") or "").strip().lower()
    if not subcat or subcat in settings.ask_clarify_ambiguous_subcategories:
        return True

    fit = (getattr(item, "fit", "") or "").strip().lower()
    valid_fits = {"oversized", "relaxed", "regular", "slim", "skinny", "boxy", "cropped", "longline"}
    if not fit or fit not in valid_fits:
        return True

    color_family = (getattr(item, "colorFamily", "") or "").strip()
    if not color_family:
        return True

    sq = (getattr(item, "searchQuery", "") or "").strip()
    sq_tokens = [t for t in sq.split() if t]
    if len(sq_tokens) < settings.ASK_CLARIFY_MIN_QUERY_TOKENS:
        return True

    return False


def _is_weak_vision_legacy(items: list[dict]) -> bool:
    """Legacy minimal-schema predicate (kept for VISION_SCHEMA_V2=False)."""
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


def _is_weak_vision(items: list[dict]) -> bool:
    """Wrapper for the legacy callers that pass list[dict]."""
    return _is_weak_vision_legacy(items)


def _route_after_vision(state: WorkingState) -> str:
    """REQ-VISION-WEAKVISION-001 — rich-schema-aware routing.

    Rule 1 (non-apparel): isApparel=False routes to `respond`, NOT
    `ask_clarify` — avoids spurious clarifying questions on landscapes/food/etc.

    Multi-item still dominates (→ picker). Otherwise, when v2 schema is active
    and a single rich item is present, apply the rich predicate. Falls back
    to legacy predicate when vision_result is None (e.g., legacy schema).
    """
    rich = state.vision_result
    items = state.detected_items

    # REQ-VISION-WEAKVISION-001 rule 1 — non-apparel → respond.
    if rich is not None and not getattr(rich, "isApparel", True):
        return "respond"

    if not items or _is_vision_fallback(items):
        return "respond"
    if len(items) > 1:
        return "pick_item"

    # Single-item path — choose predicate based on schema availability.
    if rich is not None:
        try:
            rich_items = list(rich.items)
        except Exception:  # noqa: BLE001
            rich_items = []
        if rich_items:
            if _is_weak_vision_v2(rich_items[0]):
                return "ask_clarify"
            return "critique_apply"

    # Legacy fallback.
    if _is_weak_vision_legacy(items):
        return "ask_clarify"
    return "critique_apply"


def _route_after_pick(state: WorkingState) -> str:
    """REQ-AGENT-010 / REQ-COMPAT-002 — bare picker tap routes to respond.

    Two cases:
    - `selected_item_index` set (callback `item:N`): the user has chosen an
      item; mirror the original scenario by sending the OPENER prompt and
      waiting for the intent reply. Goes to `respond` (PICK_OPENER flow) —
      NOT critique_apply, since there's no critique to apply yet.
    - `selected_item_index` is None: the carousel was just sent on this turn
      → END so we wait for the user's tap on the next webhook (no respond).
    """
    if state.selected_item_index is not None:
        return "respond"
    return "__end__"


def _route_after_search(state: WorkingState) -> str:
    """SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-EVAL-001 — when self-critique
    is enabled, ALL search outcomes (including empty) flow through `evaluator`
    so the Reflexion loop has a chance to retry. When disabled, the original
    direct edge is preserved (REQ-CRITIQUE-COST-001).
    """
    if not settings.SELF_CRITIQUE_ENABLED:
        if state.candidates:
            return "send_results"
        return "respond"
    return "evaluator"


def _route_after_evaluator(state: WorkingState) -> str:
    """SPEC-AGENTIC-CRITIQUE-001 — three-way routing.

    Branches:
    - "apply_self_critique" → retry path: bump retry counter, apply delta,
       re-invoke search_node.
    - "send_results" → ship current candidates.
    - "respond" → no candidates AND no retry → graceful empty reply.

    Hard guards (REQ-CRITIQUE-LOOP-SAFETY-001..003): iteration cap, stagnation,
    score regression, wall-clock timeout. All guards exit to send_results /
    respond with `critique_exhausted=True` set by `_finalize_exhausted`.
    """
    delta = state.critique_pending_delta
    retry_count = state.critique_retry_count
    max_iter = settings.SELF_CRITIQUE_MAX_ITERATIONS

    # No retry asked or no delta supplied → exit.
    if delta is None:
        return "send_results" if state.candidates else "respond"

    # Hard cap on iterations (REQ-CRITIQUE-LOOP-SAFETY-001).
    if retry_count >= max_iter:
        return "send_results" if state.candidates else "respond"

    # Stagnation guard (REQ-CRITIQUE-LOOP-SAFETY-002): if the latest delta
    # equals the previous delta, exit.
    trail = state.critique_trail or []
    if len(trail) >= 2:
        try:
            from app.graphs.nodes._evaluator_models import deltas_equivalent

            prev_summary = trail[-2].get("suggested_delta_summary")
            cur_summary = trail[-1].get("suggested_delta_summary")
            # Cheap check first — summaries are deterministic from the delta.
            if prev_summary is not None and prev_summary == cur_summary:
                return "send_results" if state.candidates else "respond"
            # Score regression (REQ-CRITIQUE-LOOP-SAFETY-002a).
            prev_score = trail[-2].get("score")
            cur_score = trail[-1].get("score")
            if isinstance(prev_score, (int, float)) and isinstance(cur_score, (int, float)):
                if cur_score < prev_score:
                    return "send_results" if state.candidates else "respond"
            _ = deltas_equivalent  # keep import alive for direct callers
        except Exception:  # noqa: BLE001
            pass

    # Wall-clock timeout (REQ-CRITIQUE-LOOP-SAFETY-003).
    started = state.critique_started_at_ms
    if started is not None:
        elapsed_s = (__import__("time").monotonic() * 1000.0 - started) / 1000.0
        if elapsed_s >= settings.SELF_CRITIQUE_TIMEOUT_S:
            return "send_results" if state.candidates else "respond"

    return "apply_self_critique"


def _route_after_critique(state: WorkingState) -> str:
    """Skip search when no delta was produced (stale callback / empty text).

    REQ-COMPAT-001: a stale `crit:*` callback (parse_callback returns None)
    must NOT trigger a re-search — the toast is the only user-visible side
    effect, and the graph terminates at respond.
    """
    if state.critique_delta is None:
        return "respond"
    return "search_node"
