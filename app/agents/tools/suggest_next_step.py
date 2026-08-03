"""SPEC-AGENT-V3-REACT / Gap3 — `suggest_next_step` tool wrapper.

Thin wrapper mirroring `ask_user_clarification.py`: reuses the EXISTING
`_adapter_ctx.get_adapter()` + `send_text_with_buttons`. Sends a follow-up
options card (similar items / fit change / different mood). Defines NO new
card-rendering algorithm. Callback shape `suggest:{kind}:{value}`.

@MX:NOTE: [AUTO] Side effect: Telegram inline-keyboard send (reuses adapter,
  no new card renderer). terminates_loop=False.
@MX:SPEC: SPEC-AGENT-V3-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import SuggestNextStepResult

logger = logging.getLogger(__name__)

_VALID_KINDS = {"similar", "fit_change", "different_mood", "broaden", "generic"}


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> SuggestNextStepResult:
    kind = args.get("kind") or "generic"
    if kind not in _VALID_KINDS:
        return SuggestNextStepResult(ok=False, error=f"invalid_kind:{kind}", card_sent=False, kind=str(kind))

    # 7/10 사고 방어: options 안의 None/빈값을 걸러내고 전부 str 로 강제.
    # (후보 0개 상황에서 카드 빌드 중 IndexError/TypeError 재발 방지)
    options = [str(o) for o in (args.get("options") or []) if o]
    prompt = (args.get("prompt") or "").strip()
    if not options or not prompt:
        return SuggestNextStepResult(ok=False, error="missing_options_or_prompt", card_sent=False, kind=kind)

    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return SuggestNextStepResult(ok=False, error="missing_chat_id", card_sent=False, kind=kind)

    try:
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
        buttons = [[{"text": opt[:32], "callback_data": f"suggest:{kind}:{opt}"[:64]}] for opt in options[:8]]
        if hasattr(adapter, "send_text_with_buttons"):
            await adapter.send_text_with_buttons(chat_id, prompt, buttons)
        else:
            await adapter.send_text(chat_id, prompt)
    except Exception as exc:  # noqa: BLE001
        # exc_info=True: 다음 사고 때 정확한 라인/스택을 로그에서 바로 잡기 위함.
        logger.warning("[tool.suggest_next_step] card send failed: %r", exc, exc_info=True)
        # 7/10 사고 방어: 카드(버튼) 전송이 터져도 최소한 prompt 텍스트는 유저에게
        # 나가도록 plain-text fallback.
        try:
            from app.graphs.nodes._adapter_ctx import get_adapter

            await get_adapter().send_text(chat_id, prompt)
        except Exception as fb_exc:  # noqa: BLE001
            logger.warning("[tool.suggest_next_step] text fallback also failed: %r", fb_exc, exc_info=True)
            return SuggestNextStepResult(
                ok=False, error=f"send_failed:{type(exc).__name__}", card_sent=False, kind=kind
            )
        # 텍스트는 전달됐으나 카드(버튼)는 실패 — card_sent=False 로 정직하게 보고.
        return SuggestNextStepResult(ok=False, error=f"send_failed:{type(exc).__name__}", card_sent=False, kind=kind)

    return SuggestNextStepResult(ok=True, error=None, card_sent=True, kind=kind)
