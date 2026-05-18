"""SPEC-AGENT-001 / REQ-AGENT-005 — routing functions for the StateGraph.

Each function is pure: `(state: WorkingState) -> str` returning the next node
name. They are registered via `add_conditional_edges` in `fashion_bot.py`.

SPEC-AGENT-V2-CLEANUP-001 — the ReAct agent is the only topology. The V1
routing orchestration functions (`_route_after_ingest`, `_route_after_router_text`,
`_route_after_search`, `_route_after_evaluator`, `_route_after_critique`,
`_route_after_pick`, `_route_after_vision`) were removed; the live topology
uses the inline `_route_after_*_v2` closures in `fashion_bot.py`. The
onboarding-entry predicates and `_route_after_resolve` / `_route_after_onboard_fit`
remain (still wired into the agent graph). The pure vision-weakness predicates
(`_is_weak_vision*`, `_is_vision_fallback`) are retained — they document the
vision-quality contract and have independent test coverage.

The routing predicates inspect only `WorkingState` — they never touch the
adapter or the session store. Side effects belong to nodes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, get_store

# ── SPEC-ONBOARD-CARDS-001 — entry/gating predicates ─────────────────────────
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-ENTRY-001/002 + REQ-ONBOARD-PINTEREST-003 gate the
#   entire onboarding subgraph; logic lives here so `_route_after_ingest` can
#   short-circuit before existing branches and no other code path duplicates
#   the predicate.

# Restart keyword detection — canonical source: app/channels/onboarding_values.py
# (formerly duplicated in onboard_intro.py with divergent keyword set — code
# review P0-2 fix).
_ONBOARDING_ACTIVE_STAGES: frozenset[str] = frozenset({"intro", "mood", "color", "fit", "pinterest"})


def _is_restart_keyword(text: str | None) -> bool:
    """REQ-ONBOARD-ENTRY-002 — thin wrapper over canonical helper."""
    from app.channels.onboarding_values import is_restart_keyword

    return is_restart_keyword(text)


def onboarding_required(state: WorkingState, sess: Session) -> bool:
    """REQ-ONBOARD-ENTRY-001 / REQ-ONBOARD-ENTRY-002 — onboarding gate.

    True if any of:
      (a) ONBOARDING_CARDS_ENABLED is true AND `sess.onboarded_at IS NULL`,
      (b) re-trigger keyword detected in the inbound text,
      (c) callback starts with `onboard:` (mid-flow card interaction),
      (d) `sess.onboard_stage` is in an active stage (resume).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    if not bool(getattr(settings, "ONBOARDING_CARDS_ENABLED", True)):
        return False

    msg = state.message
    cb = (msg.callback_data or "") if msg else ""
    if cb.startswith("onboard:"):
        return True

    text = msg.text if msg else None
    if _is_restart_keyword(text):
        return True

    stage = getattr(sess, "onboard_stage", None)
    if stage in _ONBOARDING_ACTIVE_STAGES:
        return True

    if getattr(sess, "onboarded_at", None) is None:
        # Fresh user — any inbound text/photo/callback triggers onboarding.
        # We do require *some* user signal; an empty payload should never reach
        # routing in practice, but guard anyway.
        if cb or (msg and (msg.text or msg.photo_file_id or msg.urls)):
            return True

    return False


def first_touch_intro_required(state: WorkingState, sess: Session) -> bool:
    """SPEC-AGENT-V2-REACT — onboarding-cards OFF first-touch gate.

    True iff ALL of:
      - ONBOARDING_CARDS_ENABLED is false (card flow disabled),
      - `sess.onboarded_at IS NULL` (brand-new / not-yet-introduced user),
      - the inbound Update carries actual user signal (text / photo / url /
        callback) — a contentless spurious Update must fall through to the
        empty-input `__end__` guard, not the intro.

    A user who has `onboarded_at` set (introduced OR completed old onboarding)
    NEVER matches — strictly gated on `onboarded_at IS NULL`.
    """
    if bool(getattr(settings, "ONBOARDING_CARDS_ENABLED", True)):
        return False
    if getattr(sess, "onboarded_at", None) is not None:
        return False
    msg = state.message
    if msg is None:
        return False
    cb = msg.callback_data or ""
    # Mirror the V2 contentless `__end__` guard exactly: a blank-text Update
    # with no callback / url / photo carries no user signal and must fall
    # through to the silent-END guard, NOT trigger the intro.
    return bool(cb or (msg.text or "").strip() or msg.photo_file_id or msg.urls)


def _resolve_onboard_stage_target(sess: Session, state: WorkingState) -> str:
    """Return the onboarding-subgraph node name for the current state.

    Priorities (highest first):
      1. Restart keyword text (`/reset`, "온보딩 다시" 등) → `onboard_intro` 강제.
         resume slot 보다 우선 — mid-flow 에서도 빠져나올 수 있어야 한다.
      2. Callback `onboard:<stage>:*` → that stage node.
      3. `sess.onboard_stage` resume slot.
      4. Default → `onboard_intro`.
    """
    # Priority 1: restart keyword 가 stage resume 를 override 한다 (버그 fix).
    text = state.message.text if state.message else None
    if _is_restart_keyword(text):
        # /reset 등 명시적 재시작 — stage / selections 클리어 후 intro 부터 다시.
        sess.onboard_stage = None
        sess.onboard_selections = {}
        sess.onboard_card_message_id = None
        get_store().update(sess)
        return "onboard_intro"

    cb = (state.message.callback_data or "") if state.message else ""
    if cb.startswith("onboard:"):
        parts = cb.split(":", 2)
        if len(parts) >= 2:
            stage_token = parts[1]
            mapping = {
                "restart": "onboard_intro",
                "mood": "onboard_mood",
                "color": "onboard_color",
                "fit": "onboard_fit",
                "pinterest": "onboard_pinterest",
            }
            target = mapping.get(stage_token)
            if target:
                return target

    stage = getattr(sess, "onboard_stage", None)
    if stage == "mood":
        return "onboard_mood"
    if stage == "color":
        return "onboard_color"
    if stage == "fit":
        return "onboard_fit"
    if stage == "pinterest":
        return "onboard_pinterest"
    return "onboard_intro"


def is_continuous_pinterest(state: WorkingState, sess: Session) -> bool:
    """REQ-ONBOARD-PINTEREST-003 — continuous (post-onboarding) bootstrap.

    True iff all of:
      - `sess.onboarded_at IS NOT NULL` (user finished onboarding),
      - `sess.onboard_stage` in {None, "done"} (not currently in onboarding),
      - inbound `text` is present AND `classify_pinterest_input(text)` returns
        non-NONE,
      - rate-limit window not active (or already expired).

    Defense in depth: the node body also enforces the rate-limit gate.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    # 사용자 피드백 — continuous bootstrap 은 기본 비활성. 온보딩 끝난 유저가
    # 핀 URL 던지면 일반 검색 흐름 (resolve_image → vision → search → cards)
    # 으로 가는 게 직관에 맞음. 명시적으로 "취향에만 추가" 시나리오가 필요할
    # 때만 PINTEREST_CONTINUOUS_ENABLED=true 로 켜기.
    if not bool(getattr(settings, "PINTEREST_CONTINUOUS_ENABLED", False)):
        return False
    if getattr(sess, "onboarded_at", None) is None:
        return False
    stage = getattr(sess, "onboard_stage", None)
    if stage not in (None, "done"):
        return False

    msg = state.message
    text = msg.text if msg else None
    if not text:
        return False

    # Avoid hard-importing at module top — keeps `routing.py` import-light and
    # mirrors the laziness used elsewhere for channel helpers.
    try:
        from app.channels.pinterest_url import PinInputNone, classify_pinterest_input
    except Exception:  # noqa: BLE001
        return False

    max_pins = int(getattr(settings, "PINTEREST_MAX_PINS_PER_TURN", 20))
    try:
        classified = classify_pinterest_input(text, max_pins=max_pins)
    except Exception:  # noqa: BLE001
        return False
    if isinstance(classified, PinInputNone):
        return False

    last = getattr(sess, "last_pinterest_scrape_at", None)
    window_s = int(getattr(settings, "PINTEREST_CONTINUOUS_RATELIMIT_S", 300))
    if last is not None and window_s > 0:
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - last).total_seconds()
        if 0 <= elapsed < float(window_s):
            return False

    return True


def _route_after_onboard_fit(state: WorkingState) -> str:
    """REQ-ONBOARD-GRAPH-001 — Stage 3 branch on PINTEREST_BOOTSTRAP_ENABLED.

    Currently both branches lead to END because `onboard_fit` finalizes
    inline when Pinterest is disabled and renders the Pinterest card when
    enabled (the user's next callback re-enters the graph fresh). The
    explicit conditional satisfies plan §6.2 entries #4+#5.
    """
    # When Pinterest is enabled and the user just hit `fit:done`, the next
    # webhook turn carries `onboard:pinterest:*` (or a URL text), at which point
    # the ingest gate dispatches to `onboard_pinterest` directly. Within the
    # same turn we terminate.
    return "__end__"


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
