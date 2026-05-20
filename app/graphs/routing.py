"""SPEC-AGENT-001 / REQ-AGENT-005 — routing functions for the StateGraph.

Each function is pure: `(state: WorkingState) -> str` returning the next node
name. They are registered via `add_conditional_edges` in `fashion_bot.py`.

SPEC-ONBOARD-LITE-001 — the onboarding-entry predicates were removed with
the onboarding card subgraph. Only `_route_after_resolve` and the pure
vision-weakness predicates (`_is_weak_vision*`, `_is_vision_fallback`)
remain; the live topology uses the inline `_route_after_*_v2` closures in
`fashion_bot.py` and the `ingest` first-touch handler.

The routing predicates inspect only `WorkingState` — they never touch the
adapter or the session store. Side effects belong to nodes.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.graphs.state import WorkingState

# SPEC-ONBOARD-LITE-001 — the onboarding entry/gating predicates
# (`onboarding_required`, `first_touch_intro_required`,
# `_resolve_onboard_stage_target`, `is_continuous_pinterest`,
# `_route_after_onboard_fit`, `_is_restart_keyword`,
# `_ONBOARDING_ACTIVE_STAGES`) were removed with the onboarding card
# subgraph. First-touch is now handled inline in `ingest`
# (`maybe_first_touch`) + the `_route_after_ingest_v2` closure in
# `fashion_bot.py`. The pure vision-weakness predicates below are retained.


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


# SPEC-AGENT-V2-CLEANUP-001 — `_route_after_vision`, `_route_after_pick`,
# `_route_after_search`, `_route_after_evaluator`, `_route_after_critique`
# were V1-only routing orchestration (targets: critique_apply / router_text /
# search_node / send_results / taste_update / evaluator — all V1 nodes that no
# longer exist). They are superseded by the inline `_route_after_*_v2`
# closures in `fashion_bot.py` and have been removed. The pure
# vision-weakness predicates above are retained.
