"""Onboarding Stage 3 — fit card callback handler with completion branch.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-002 + REQ-ONBOARD-COMPLETION-001 +
REQ-ONBOARD-PINTEREST-001.

Handles `onboard:fit:{toggle|done|skip}[:value]` callbacks. Bounds (1-2).

On `done` (or `skip`) advance branch depends on `settings.PINTEREST_BOOTSTRAP_ENABLED`:
  - True  → advance to `pinterest` stage (Stage 4).
  - False → call `complete_onboarding(...)` here (card-only seed). Set
            `onboard_stage="done"` and dispatch the completion text.

This is the only stage node that may finalize onboarding inline (ONB-T14 plan
§6.5 / §6.6 — completion is internal to fit when Pinterest is disabled).

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import logging
from typing import Any

from app.channels.lang import session_lang
from app.channels.onboarding_cards import build_fit_card, parse_onboard_callback
from app.channels.onboarding_values import STAGE_BOUNDS
from app.channels.session import get_store
from app.channels.taste_profile import get_taste_store, user_key_for
from app.core.config import settings
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._onboard_helpers import complete_onboarding
from app.graphs.nodes._onboard_stage import (
    _emit_onboard_select,
    _toast_bounds,
    _write_selection,
    handle_stage_callback,
)
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


def _pinterest_enabled() -> bool:
    return bool(getattr(settings, "PINTEREST_BOOTSTRAP_ENABLED", True))


def _current_selection(sess: Any, stage: str) -> list[str]:
    raw = getattr(sess, "onboard_selections", None) or {}
    val = raw.get(stage, []) if isinstance(raw, dict) else []
    return [v for v in val if isinstance(v, str)]


async def _finalize_card_only(state: WorkingState) -> dict:
    """Card-only completion path (Pinterest disabled / skipped).

    Calls `complete_onboarding` which makes the single seed call, marks
    `onboarded_at`, and returns the completion message text.
    """
    sess = get_store().get_or_create(state.chat_id)
    adapter = get_adapter()
    user_key = user_key_for(state.from_user_id, state.chat_id)
    taste_store = get_taste_store()

    try:
        completion_text, _ = await complete_onboarding(
            state,
            sess=sess,
            user_key=user_key,
            session_store=get_store(),
            taste_store=taste_store,
        )
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [ONBOARD] complete_onboarding failed at fit-done branch")
        completion_text = ""

    if completion_text:
        try:
            await adapter.send_text(state.chat_id, completion_text)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [ONBOARD] completion text send failed")

    return {
        "onboard_stage": "done",
        "onboard_selections": dict(getattr(sess, "onboard_selections", {}) or {}),
        "onboard_pin_weights": None,
        "log_events": ["onboard_fit: completed_card_only"],
    }


# @MX:ANCHOR: [AUTO] Stage 3 callback handler with completion branch.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-COMPLETION-001 — when Pinterest is disabled this is
#   the onboarding terminator. The single-seed-call invariant is verified by
#   test_completion_flow.py.
@observe(name="onboarding.stage.fit", as_type="span")
async def onboard_fit(state: WorkingState) -> dict:
    """Stage 3 fit handler with optional inline completion.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    pinterest_on = _pinterest_enabled()

    # When Pinterest is enabled, delegate to the shared stage handler which
    # advances to "pinterest". Otherwise we must intercept done/skip and run
    # `complete_onboarding` inline.
    if pinterest_on:
        return await handle_stage_callback(
            state,
            stage="fit",
            next_stage="pinterest",
            build_card=build_fit_card,
        )

    # ── Pinterest disabled branch: intercept done/skip for inline completion ──
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    adapter = get_adapter()
    msg = state.message
    cb_raw = msg.callback_data or ""
    parsed = parse_onboard_callback(cb_raw)

    if parsed is None or parsed.stage != "fit":
        # Stale — best-effort ack, stay on stage.
        try:
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, "")
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:fit] stale ack best-effort", exc_info=True)
        return {"onboard_stage": "fit", "log_events": ["onboard_fit: stale_cb"]}

    selections = _current_selection(sess, "fit")
    lo, hi = STAGE_BOUNDS["fit"]

    # Toggle behaves identically to the enabled branch — delegate to shared.
    if parsed.action == "toggle":
        return await handle_stage_callback(
            state,
            stage="fit",
            next_stage="fit",  # toggle never advances; placeholder unused.
            build_card=build_fit_card,
        )

    if parsed.action == "done":
        if len(selections) < lo:
            try:
                if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                    await adapter.answer_callback_query(msg.callback_query_id, _toast_bounds(lang, lo, hi))
            except Exception:  # noqa: BLE001
                logger.debug("[onboard:fit] done-too-few toast best-effort", exc_info=True)
            return {
                "onboard_stage": "fit",
                "log_events": [f"onboard_fit: done_blocked count={len(selections)}"],
            }
        try:
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:fit] done ack best-effort", exc_info=True)
        _emit_onboard_select(state, stage="fit", axis="fit", selected_values=selections)
        return await _finalize_card_only(state)

    if parsed.action == "skip":
        _write_selection(sess, "fit", [])
        get_store().update(sess)
        try:
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:fit] skip ack best-effort", exc_info=True)
        _emit_onboard_select(state, stage="fit", axis="fit", selected_values=[])
        return await _finalize_card_only(state)

    return {"onboard_stage": "fit", "log_events": ["onboard_fit: unhandled"]}


__all__ = ["onboard_fit"]
