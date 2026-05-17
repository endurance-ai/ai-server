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

    options = list(args.get("options") or [])
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
        logger.warning("[tool.suggest_next_step] raised: %r", exc)
        return SuggestNextStepResult(ok=False, error=f"send_failed:{type(exc).__name__}", card_sent=False, kind=kind)

    return SuggestNextStepResult(ok=True, error=None, card_sent=True, kind=kind)
