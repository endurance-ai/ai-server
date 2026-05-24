"""SPEC-CLARIFY-CARDS-001 — ask_clarify (인라인 키보드 카드).

이 노드는 weak-vision 분기에서 단 한 번 호출된다. 두 경로:

1. CARD 경로 (default): `pick_clarify_axis(vision_result)` 가 non-None 축을
   반환하면 결정론적 인라인 키보드 1개를 보낸다. LLM 호출 없음.
   session.state = AWAITING_CLARIFY 로 전이 후 END.

2. STATIC 폴백: `pick_clarify_axis` 가 None 일 때 미리 정의된 메시지를 전송.
   LLM 호출 없음.
"""

from __future__ import annotations

import logging

from app.channels.clarify import ClarifyAxis, pick_clarify_axis
from app.channels.clarify_values import (
    AXIS_PROMPTS_KO,
    SKIP_LABEL_KO,
    SKIP_VALUE,
    get_options,
)
from app.channels.lang import session_lang
from app.core.config import settings
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._trace import node_done, node_enter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import SessionState, get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


_FALLBACK_EN = "Got it — is that a top, a dress, or outerwear?"
_FALLBACK_KO = "오케이! 혹시 상의야, 원피스야, 아우터야?"
# Back-compat alias (some older imports may still reference _FALLBACK).
_FALLBACK = _FALLBACK_EN


def _build_buttons(axis: ClarifyAxis) -> list[tuple[str, str]]:
    """카드 버튼 row 빌드. 마지막에 '건너뛰기' 추가(REQ-CLARIFY-CARD-002).

    상한: settings.CLARIFY_MAX_BUTTONS (기본 5, skip 포함).
    """
    options = get_options(axis.value)
    max_buttons = max(3, min(int(settings.CLARIFY_MAX_BUTTONS), 8))
    # skip 자리(1개) 확보.
    keep = max_buttons - 1
    rows: list[tuple[str, str]] = [(opt.label_ko, f"clarify:{axis.value}:{opt.value}") for opt in options[:keep]]
    rows.append((SKIP_LABEL_KO, f"clarify:{axis.value}:{SKIP_VALUE}"))
    return rows


async def _send_card_path(state: WorkingState, axis: ClarifyAxis) -> dict:
    """CARD 경로 — 인라인 키보드 1개 발행."""
    breadcrumbs: list[str] = []
    body = AXIS_PROMPTS_KO.get(axis.value) or "조금 더 자세히 알려줄래?"
    buttons = _build_buttons(axis)

    try:
        adapter = get_adapter()
        if hasattr(adapter, "send_text_with_buttons"):
            await adapter.send_text_with_buttons(state.chat_id, body, buttons)
        else:
            # 어댑터가 인라인 키보드 미지원이면 본문만 송출(graceful).
            await adapter.send_text(state.chat_id, body)
    except Exception as exc:
        logger.exception("[ask_clarify] card send failed")
        breadcrumbs.append(f"ask_clarify_send_error: {type(exc).__name__}"[:200])
        return {"response_text": body, "log_events": breadcrumbs}

    # 세션 상태 전이(REQ-CLARIFY-STATE-001).
    try:
        sess = get_store().get_or_create(state.chat_id)
        sess.state = SessionState.AWAITING_CLARIFY
        sess.clarify_axis = axis.value
        get_store().update(sess)
    except Exception:  # noqa: BLE001
        logger.debug("[ask_clarify] session update best-effort")

    logger.info(
        "[CLARIFY] axis=%s buttons=%d ko_prompt=%r",
        axis.value,
        len(buttons),
        body[:80],
    )
    breadcrumbs.append(f"ask_clarify: card axis={axis.value} buttons={len(buttons)}")
    node_done("ask_clarify", path="card", axis=axis.value, buttons=len(buttons))
    # LOG-T15 — emit `ask_clarify_sent` after the card is dispatched. (@MX:SPEC: SPEC-CONVERSATION-LOG-001)
    try:
        emit(
            event_type="ask_clarify_sent",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=5,
            payload={
                "axis": axis.value,
                "options_shown": [label for label, _cb in buttons],
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ask_clarify] ask_clarify_sent emit best-effort")
    return {
        "response_text": body,
        "clarify_axis": axis,
        "log_events": breadcrumbs,
        "turn_no": 5,
    }


async def _static_fallback_path(state: WorkingState) -> dict:
    """Static fallback when no clarify axis can be determined — sends a
    deterministic message with no LLM call."""
    breadcrumbs: list[str] = []
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    text = _FALLBACK_KO if lang == "ko" else _FALLBACK_EN

    try:
        adapter = get_adapter()
        await adapter.send_text(state.chat_id, text)
    except Exception as exc:
        logger.exception("[ask_clarify] static fallback send_text failed")
        breadcrumbs.append(f"ask_clarify_send_error: {type(exc).__name__}"[:200])
        return {"response_text": text, "log_events": breadcrumbs}

    breadcrumbs.append(f"ask_clarify: static_fallback lang={lang} len={len(text)}")
    node_done("ask_clarify", path="static_fallback", chars=len(text))
    return {"response_text": text, "log_events": breadcrumbs}


@observe(name="node.ask_clarify", as_type="span")
async def ask_clarify(state: WorkingState) -> dict:
    """CARD 경로 또는 결정 불가 시 static fallback."""
    node_enter("ask_clarify")
    axis = pick_clarify_axis(state.vision_result)
    if axis is None:
        return await _static_fallback_path(state)
    return await _send_card_path(state, axis)
