"""Shared stage-callback handler for onboard_mood / onboard_color / onboard_fit.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-002 + REQ-ONBOARD-OBS-001.

The three card stages (mood / color / fit) share an identical callback handling
shape — toggle / done / skip — so the body lives here. The per-stage node only
provides:
  - stage name ("mood" | "color" | "fit")
  - next stage name (used by the routing layer to advance)
  - card builder
  - Langfuse span name (decorated on the wrapping node)

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.channels.lang import session_lang
from app.channels.onboarding_cards import (
    Keyboard,
    parse_onboard_callback,
)
from app.channels.onboarding_values import STAGE_BOUNDS
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit

logger = logging.getLogger(__name__)


_TOAST_BOUNDS_KO = "{lo}~{hi}개 선택해 주세요"
_TOAST_BOUNDS_EN = "Please pick {lo}-{hi}"


def _toast_bounds(lang: str, lo: int, hi: int) -> str:
    return (_TOAST_BOUNDS_KO if lang == "ko" else _TOAST_BOUNDS_EN).format(lo=lo, hi=hi)


def _current_selection(sess: Any, stage: str) -> list[str]:
    raw = getattr(sess, "onboard_selections", None) or {}
    val = raw.get(stage, []) if isinstance(raw, dict) else []
    return [v for v in val if isinstance(v, str)]


def _write_selection(sess: Any, stage: str, values: list[str]) -> None:
    """Mutate `sess.onboard_selections[stage]` defensively."""
    raw = getattr(sess, "onboard_selections", None)
    if not isinstance(raw, dict):
        raw = {"mood": [], "color": [], "fit": []}
    raw[stage] = list(values)
    sess.onboard_selections = raw


def _emit_onboard_select(
    state: WorkingState,
    *,
    stage: str,
    axis: str,
    selected_values: list[str],
) -> None:
    """LOG-T23 / SPEC-CONVERSATION-LOG-001 — emit `onboard_select`."""
    try:
        emit(
            event_type="onboard_select",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=1,
            payload={"stage": stage, "axis": axis, "selected_values": list(selected_values)},
        )
    except Exception:  # noqa: BLE001
        logger.debug("[onboard:%s] onboard_select emit best-effort", stage, exc_info=True)


# @MX:ANCHOR: [AUTO] Shared stage-advance contract for mood/color/fit cards.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-CARDS-002 — three nodes consume this; toggle/done/skip
#   semantics are pinned by test_onboard_nodes.py snapshot tests.
async def handle_stage_callback(
    state: WorkingState,
    *,
    stage: str,
    next_stage: str,
    build_card: Callable[[str, list[str]], tuple[str, Keyboard]],
    build_next_card: Callable[[str, list[str]], tuple[str, Keyboard]] | None = None,
) -> dict:
    """Run the toggle/done/skip state-machine for one card stage.

    Returns a state delta dict for the graph reducer. The caller (per-stage
    node) is decorated with `@observe` for the Langfuse span.
    """
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    adapter = get_adapter()
    msg = state.message

    cb_raw = msg.callback_data or ""
    parsed = parse_onboard_callback(cb_raw)

    # ── stale callback OR free-form text mid-stage ──────────────────────────
    if parsed is None or parsed.stage != stage:
        # 케이스 분기:
        # (a) 진짜 stale callback (다른 stage 의 옛 카드 탭) → ack + stay
        # (b) free-form text (사용자가 카드 옆에서 메시지 입력) → 힌트 +
        #     현재 stage 카드를 현재 lang 으로 재렌더. ingest 가 sess.lang 을
        #     이미 갱신했으므로 "한글로해줘" 같은 언어 전환 요청도 자동 반영됨.
        if msg.callback_query_id:
            # (a) stale callback
            try:
                if hasattr(adapter, "answer_callback_query"):
                    await adapter.answer_callback_query(msg.callback_query_id, "")
            except Exception:  # noqa: BLE001
                logger.debug("[onboard:%s] stale callback ack best-effort", stage, exc_info=True)
            return {
                "onboard_stage": stage,
                "log_events": [f"onboard_{stage}: stale_cb cb={cb_raw[:32]}"],
            }
        # (b) free-form text — 안내 + 카드 재렌더.
        selections = _current_selection(sess, stage)
        try:
            hint = "👆 위 카드에서 골라줘" if lang == "ko" else "👆 pick from the card above"
            await adapter.send_text(state.chat_id, hint)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:%s] hint send best-effort", stage, exc_info=True)
        try:
            new_text, new_kb = build_card(lang, selections)
            new_msg_id = await adapter.send_text_with_keyboard(state.chat_id, new_text, new_kb)
            if new_msg_id:
                sess.onboard_card_message_id = new_msg_id
                get_store().update(sess)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [ONBOARD:%s] re-render after free-text failed", stage)
        return {
            "onboard_stage": stage,
            "onboard_card_message_id": getattr(sess, "onboard_card_message_id", None),
            "log_events": [f"onboard_{stage}: free_text_rerender lang={lang}"],
        }

    selections = _current_selection(sess, stage)
    lo, hi = STAGE_BOUNDS[stage]

    # ── toggle ───────────────────────────────────────────────────────────────
    if parsed.action == "toggle" and parsed.value:
        value = parsed.value
        if value in selections:
            selections.remove(value)
        else:
            if len(selections) >= hi:
                # Don't add beyond max — toast and stay.
                try:
                    if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                        await adapter.answer_callback_query(
                            msg.callback_query_id,
                            _toast_bounds(lang, lo, hi),
                        )
                except Exception:  # noqa: BLE001
                    logger.debug("[onboard:%s] bounds toast best-effort", stage, exc_info=True)
                return {
                    "onboard_stage": stage,
                    "onboard_selections": dict(getattr(sess, "onboard_selections", {}) or {}),
                    "log_events": [f"onboard_{stage}: toggle_capped value={value}"],
                }
            selections.append(value)

        _write_selection(sess, stage, selections)
        get_store().update(sess)

        # Re-render card in place via editMessageReplyMarkup; fallback to send.
        _, kb = build_card(lang, selections)
        msg_id = getattr(sess, "onboard_card_message_id", None)
        try:
            if msg_id and hasattr(adapter, "edit_inline_keyboard"):
                ok = await adapter.edit_inline_keyboard(state.chat_id, msg_id, kb)
                if not ok:
                    new_text, _ = build_card(lang, selections)
                    new_msg_id = await adapter.send_text_with_keyboard(state.chat_id, new_text, kb)
                    if new_msg_id:
                        sess.onboard_card_message_id = new_msg_id
                        get_store().update(sess)
            else:
                new_text, _ = build_card(lang, selections)
                new_msg_id = await adapter.send_text_with_keyboard(state.chat_id, new_text, kb)
                if new_msg_id:
                    sess.onboard_card_message_id = new_msg_id
                    get_store().update(sess)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [ONBOARD:%s] re-render failed", stage)

        # Ack the callback (no toast on plain toggle).
        try:
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:%s] toggle ack best-effort", stage, exc_info=True)

        return {
            "onboard_stage": stage,
            "onboard_selections": dict(getattr(sess, "onboard_selections", {}) or {}),
            "log_events": [f"onboard_{stage}: toggle value={value} count={len(selections)}"],
        }

    # ── done ─────────────────────────────────────────────────────────────────
    if parsed.action == "done":
        if len(selections) < lo:
            # Below minimum — toast, stay on stage.
            try:
                if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                    await adapter.answer_callback_query(
                        msg.callback_query_id,
                        _toast_bounds(lang, lo, hi),
                    )
            except Exception:  # noqa: BLE001
                logger.debug("[onboard:%s] done-too-few toast best-effort", stage, exc_info=True)
            return {
                "onboard_stage": stage,
                "log_events": [f"onboard_{stage}: done_blocked count={len(selections)}"],
            }
        # Advance — ack + emit + return next_stage.
        try:
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:%s] done ack best-effort", stage, exc_info=True)

        _emit_onboard_select(state, stage=stage, axis=stage, selected_values=selections)

        sess.onboard_stage = next_stage
        # 다음 stage 카드를 즉시 그려서 보낸다 — 이 호출이 없으면 사용자에게
        # 침묵으로 보임 (그래프는 END 도달, next 카드 미발송). 버그 #4 fix.
        new_msg_id: int | None = None
        if build_next_card is not None:
            try:
                next_selections = _current_selection(sess, next_stage)
                next_text, next_kb = build_next_card(lang, next_selections)
                new_msg_id = await adapter.send_text_with_keyboard(state.chat_id, next_text, next_kb)
            except Exception:  # noqa: BLE001
                logger.exception("🐱 [ONBOARD:%s->%s] next card send failed", stage, next_stage)
        if new_msg_id:
            sess.onboard_card_message_id = new_msg_id
        get_store().update(sess)
        return {
            "onboard_stage": next_stage,
            "onboard_selections": dict(getattr(sess, "onboard_selections", {}) or {}),
            "onboard_card_message_id": new_msg_id,
            "log_events": [f"onboard_{stage}: done -> {next_stage} count={len(selections)}"],
        }

    # ── skip ─────────────────────────────────────────────────────────────────
    if parsed.action == "skip":
        # For fit (min=1) skip is not strictly allowed, but treat as empty-advance
        # for forgiving UX. completion node uses additive seed so empty is benign.
        _write_selection(sess, stage, [])
        sess.onboard_stage = next_stage
        try:
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:%s] skip ack best-effort", stage, exc_info=True)

        _emit_onboard_select(state, stage=stage, axis=stage, selected_values=[])

        # 다음 stage 카드 발송 (버그 #4 fix — done 분기와 동일).
        new_msg_id: int | None = None
        if build_next_card is not None:
            try:
                next_selections = _current_selection(sess, next_stage)
                next_text, next_kb = build_next_card(lang, next_selections)
                new_msg_id = await adapter.send_text_with_keyboard(state.chat_id, next_text, next_kb)
            except Exception:  # noqa: BLE001
                logger.exception("🐱 [ONBOARD:%s->%s] next card send failed (skip)", stage, next_stage)
        if new_msg_id:
            sess.onboard_card_message_id = new_msg_id
        get_store().update(sess)

        return {
            "onboard_stage": next_stage,
            "onboard_selections": dict(getattr(sess, "onboard_selections", {}) or {}),
            "onboard_card_message_id": new_msg_id,
            "log_events": [f"onboard_{stage}: skip -> {next_stage}"],
        }

    # ── fallthrough (impossible per parser whitelist) ───────────────────────
    return {
        "onboard_stage": stage,
        "log_events": [f"onboard_{stage}: unhandled action={parsed.action}"],
    }


__all__ = ["handle_stage_callback"]
