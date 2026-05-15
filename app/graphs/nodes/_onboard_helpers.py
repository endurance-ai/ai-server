"""Onboarding completion helper — single seed call discipline.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-COMPLETION-001.

Builds card-derived keyword weights from `state.onboard_selections`, merges
with `state.onboard_pin_weights` (union; overlap → SUM), and calls
`taste_store.seed_from_onboarding(user_key, merged)` EXACTLY ONCE per
onboarding session.

Order (crash-safety per plan §6.6):
    1. Compute card-derived weights.
    2. Merge with Pinterest stash (if any).
    3. seed_from_onboarding(user_key, merged).
    4. sess.onboarded_at = now(); session_store.update(sess).
    5. Build completion message (Pinterest-success vs degraded variant).
    6. Clear state: onboard_pin_weights=None, onboard_stage="done".

Step 3 BEFORE step 4 — a crash between would leave onboarded_at=NULL so the
user re-onboards on next /start. The downside (one duplicate seed call) is
strictly preferable to onboarded_at-without-seed.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.channels.lang import session_lang
from app.channels.onboarding_values import (
    COLOR_VALUES,
    FIT_VALUES,
    MOOD_VALUES,
    OnboardingOption,
)
from app.core.config import settings

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Card-derived keyword weights
# ────────────────────────────────────────────────────────────────────────────
_STAGE_CATALOGS: dict[str, list[OnboardingOption]] = {
    "mood": MOOD_VALUES,
    "color": COLOR_VALUES,
    "fit": FIT_VALUES,
}


def _card_seed_weight() -> float:
    """Per-card-selection contribution.

    `ONBOARDING_CARD_SEED_WEIGHT` is read with a 0.5 default — operator override
    happens in `app/core/config.py` (Phase 1) when explicitly added.
    """
    return float(getattr(settings, "ONBOARDING_CARD_SEED_WEIGHT", 0.5))


def _compute_card_weights(selections: dict[str, list[str]] | None) -> dict[str, float]:
    """Build keyword weights from card selections.

    Each selected option contributes its `value` (the snake_case identifier,
    which doubles as a search-friendly keyword — e.g. `minimal`, `oversize`,
    `pastel`) at `ONBOARDING_CARD_SEED_WEIGHT`.

    Unknown values are ignored (callback parser already gates, but stale
    persisted state could still contain drift).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    weights: dict[str, float] = {}
    if not selections:
        return weights
    w = _card_seed_weight()
    for stage, values in selections.items():
        catalog = _STAGE_CATALOGS.get(stage)
        if not catalog:
            continue
        valid_values = {opt.value for opt in catalog}
        for v in values or []:
            if not isinstance(v, str):
                continue
            key = v.strip().lower()
            if not key or key not in valid_values:
                continue
            weights[key] = weights.get(key, 0.0) + w
    return weights


def _merge_pin_weights(
    card_weights: dict[str, float],
    pin_stash: dict | None,
) -> dict[str, float]:
    """Union merge with summation on overlap.

    `pin_stash` is the dict that `_stash_weights_on_state` wrote earlier —
    same shape as `_AggregatedWeights`. Brand weights are mixed in under the
    `brand:` prefix (matching `_seed_keyword_brand_dict` in pinterest_helpers).
    """
    merged: dict[str, float] = dict(card_weights)
    if not pin_stash or not isinstance(pin_stash, dict):
        return merged
    for k, v in (pin_stash.get("keyword_weights") or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv <= 0:
            continue
        merged[str(k)] = merged.get(str(k), 0.0) + fv
    for b, v in (pin_stash.get("brand_weights") or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv <= 0:
            continue
        key = f"brand:{b}"
        merged[key] = merged.get(key, 0.0) + fv
    return merged


# ────────────────────────────────────────────────────────────────────────────
# Completion messages (sticky lang)
# ────────────────────────────────────────────────────────────────────────────
_COMPLETION_PINTEREST_SUCCESS_KO = (
    "고마워요! 취향 시드를 저장했어요. 🐱\n이제 사진이나 링크를 보내면 비슷한 옷을 찾아드릴게요."
)
_COMPLETION_PINTEREST_SUCCESS_EN = (
    "Thanks! Your taste seed is saved. 🐱\nSend a photo or link and I'll find similar pieces."
)
_COMPLETION_DEGRADED_KO = (
    "취향을 저장했어요. 🐱\n핀터레스트는 다음에 더 풍부하게 받아볼게요. 사진/링크 보내주시면 시작해요."
)
_COMPLETION_DEGRADED_EN = "Got your taste profile. 🐱\nPinterest will plug in later. Send a photo/link to begin."


def _completion_text(*, lang: str, pinterest_success: bool) -> str:
    """Sticky-lang completion message.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    if lang == "ko":
        return _COMPLETION_PINTEREST_SUCCESS_KO if pinterest_success else _COMPLETION_DEGRADED_KO
    return _COMPLETION_PINTEREST_SUCCESS_EN if pinterest_success else _COMPLETION_DEGRADED_EN


# ────────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────────
# @MX:ANCHOR: [AUTO] Single-seed-call completion. The order (seed BEFORE
#   onboarded_at write) is part of the crash-safety contract.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-COMPLETION-001 + REQ-ONBOARD-SEED-001 — exactly-one
#   seed call per onboarding session is verified by test_completion_flow.py.
async def complete_onboarding(
    state: Any,
    *,
    sess: Any,
    user_key: str,
    session_store: Any,
    taste_store: Any,
    now: datetime | None = None,
) -> tuple[str, Any]:
    """Run the completion sequence and return `(completion_text, sess)`.

    Caller (Phase 3 graph node) dispatches the text via the channel adapter.

    Args:
        state: WorkingState-like object with `onboard_selections`,
            `onboard_pin_weights`, `onboard_stage` attributes.
        sess: Session row to mark as onboarded.
        user_key: Output of `user_key_for(from_user_id, chat_id)`.
        session_store: SessionStore for persistence.
        taste_store: TasteProfileStore for the single seed call.
        now: Override for deterministic tests.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    # ── 1. Card-derived weights ──────────────────────────────────────────
    selections = getattr(state, "onboard_selections", None) or {}
    card_weights = _compute_card_weights(selections)

    # ── 2. Merge with pin stash (if any) ──────────────────────────────────
    pin_stash = getattr(state, "onboard_pin_weights", None)
    merged = _merge_pin_weights(card_weights, pin_stash)
    pinterest_success = bool(
        pin_stash and isinstance(pin_stash, dict) and int(pin_stash.get("successfully_analyzed", 0)) > 0
    )

    # ── 3. EXACTLY ONE seed call ──────────────────────────────────────────
    if merged:
        try:
            taste_store.seed_from_onboarding(user_key, merged)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [ONBOARD] seed_from_onboarding failed — onboarding aborted")
            # Re-raise so the calling node surfaces a retry path. Crash-safety
            # rule: do NOT mark onboarded_at if seed failed.
            raise

    # SPEC-CONVERSATION-LOG-001 LOG-T23 cascade — emit `taste_update` so the
    # `source="onboard"` and `source="pinterest"` Literal values move from
    # `_UNIMPLEMENTED_SOURCES` to `_IMPLEMENTED_SOURCES`. The AST scanner in
    # `tests/test_conversation_log/test_payload_shapes.py` reads payload-dict
    # literals — keep `event_type` and `source` as Constant strings.
    try:
        from app.observability.conversation_log import emit

        # Card-axis emit — always fires (even with empty selections we still
        # mark the channel as "onboard").
        emit(
            event_type="taste_update",
            user_key=user_key,
            chat_id=getattr(state, "chat_id", 0),
            thread_id=getattr(state, "thread_id", None),
            turn_no=getattr(state, "turn_no", 0),
            payload={"source": "onboard", "keyword_count": len(merged or {})},
        )
        # Pinterest-axis emit — only when a pin stash contributed weights.
        if pinterest_success:
            emit(
                event_type="taste_update",
                user_key=user_key,
                chat_id=getattr(state, "chat_id", 0),
                thread_id=getattr(state, "thread_id", None),
                turn_no=getattr(state, "turn_no", 0),
                payload={
                    "source": "pinterest",
                    "pin_count": int((pin_stash or {}).get("successfully_analyzed", 0)),
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug("[ONBOARD] taste_update emit best-effort", exc_info=True)

    # ── 4. Mark onboarded_at + persist (AFTER seed) ───────────────────────
    stamp = now or datetime.now(UTC)
    try:
        sess.onboarded_at = stamp
        sess.onboard_stage = "done"
        session_store.update(sess)
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [ONBOARD] session persist failed (post-seed)")

    # ── 5. Completion message (sticky lang) ───────────────────────────────
    lang = session_lang(sess)
    text = _completion_text(lang=lang, pinterest_success=pinterest_success)

    # ── 6. Clear staging state ────────────────────────────────────────────
    try:
        state.onboard_pin_weights = None
        state.onboard_stage = "done"
    except Exception:  # noqa: BLE001
        logger.debug("🐱 [ONBOARD] state clear best-effort skipped", exc_info=True)

    return text, sess


__all__ = [
    "complete_onboarding",
]
