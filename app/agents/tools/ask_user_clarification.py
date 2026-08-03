"""SPEC-AGENT-V2-REACT / T-003e — `ask_user_clarification` tool wrapper.

Sends an inline-keyboard card. Callback shape `clarify:{axis}:{value}` mirrors
SPEC-CLARIFY-CARDS-001.

@MX:NOTE: [AUTO] Side effect: sends Telegram message with InlineKeyboard.
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import AskUserClarificationResult

logger = logging.getLogger(__name__)

_VALID_AXES = ("category_pick", "formality", "fit", "occasion", "subcategory_disambiguation", "generic_fallback")


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> AskUserClarificationResult:
    axis = args.get("axis")
    if axis not in _VALID_AXES:
        # P0-1 (260521 V3 eval): include valid axes in the error so the LLM's
        # next iter can self-correct. The structural validator already rejects
        # this case (see tool_registry.validate_args); this is belt-and-braces
        # for direct dispatch paths that may bypass the validator.
        return AskUserClarificationResult(
            ok=False,
            error=f"invalid_axis: {axis!r} not in {list(_VALID_AXES)}",
            card_sent=False,
            axis=str(axis),
        )

    # 7/10 사고 방어: options 안의 None/빈값을 걸러내고 전부 str 로 강제.
    # (검색 실패로 후보 0개 → 카드 빌드 중 IndexError/TypeError 재발 방지)
    options = [str(o) for o in (args.get("options") or []) if o]
    prompt = (args.get("prompt") or "").strip()
    if not options or not prompt:
        return AskUserClarificationResult(ok=False, error="missing_options_or_prompt", card_sent=False, axis=axis)

    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return AskUserClarificationResult(ok=False, error="missing_chat_id", card_sent=False, axis=axis)

    try:
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
        # Build a simple 1-row-per-option inline keyboard.
        buttons = [[{"text": opt[:32], "callback_data": f"clarify:{axis}:{opt}"[:64]}] for opt in options[:8]]
        if hasattr(adapter, "send_text_with_buttons"):
            await adapter.send_text_with_buttons(chat_id, prompt, buttons)
        else:
            await adapter.send_text(chat_id, prompt)
    except Exception as exc:  # noqa: BLE001
        # exc_info=True: 다음 사고 때 정확한 라인/스택을 로그에서 바로 잡기 위함.
        logger.warning("[tool.ask_user_clarification] card send failed: %r", exc, exc_info=True)
        # 7/10 사고 방어: 카드(버튼) 전송이 터져도 최소한 prompt 텍스트는 유저에게
        # 나가도록 plain-text fallback. (카드 실패 → 텍스트만 조용히 나가던 문제 방지)
        try:
            from app.graphs.nodes._adapter_ctx import get_adapter

            await get_adapter().send_text(chat_id, prompt)
        except Exception as fb_exc:  # noqa: BLE001
            logger.warning("[tool.ask_user_clarification] text fallback also failed: %r", fb_exc, exc_info=True)
            return AskUserClarificationResult(
                ok=False, error=f"send_failed:{type(exc).__name__}", card_sent=False, axis=axis
            )
        # 텍스트는 전달됐으나 카드(버튼)는 실패 — card_sent=False 로 정직하게 보고.
        return AskUserClarificationResult(
            ok=False, error=f"send_failed:{type(exc).__name__}", card_sent=False, axis=axis
        )

    return AskUserClarificationResult(ok=True, error=None, card_sent=True, axis=axis)
