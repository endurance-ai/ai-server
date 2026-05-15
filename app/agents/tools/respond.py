"""SPEC-AGENT-V2-REACT / T-003g — `respond` tool wrapper.

Sends a natural-language reply (LLM-generated text passed by agent) plus optional
result cards. NO `_Flow` enum — the agent LLM is the single source of phrasing.
Loop-terminating tool: `terminates_loop=True` in REGISTRY.

@MX:NOTE: [AUTO] Side effect: sends Telegram messages (text + cards).
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import RespondResult

logger = logging.getLogger(__name__)


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> RespondResult:
    text = (args.get("text") or "").strip()
    cards = args.get("cards") or []
    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return RespondResult(ok=False, error="missing_chat_id", text_sent=False, cards_sent=0)
    if not text and not cards:
        return RespondResult(ok=False, error="empty_response", text_sent=False, cards_sent=0)

    try:
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
    except Exception as exc:  # noqa: BLE001
        return RespondResult(ok=False, error=f"adapter_missing:{type(exc).__name__}", text_sent=False, cards_sent=0)

    text_sent = False
    cards_sent = 0

    # Send text first.
    if text:
        try:
            # Cap to safety length (matches v1 respond.py: 4 * RESPONSE_MAX_TOKENS).
            await adapter.send_text(chat_id, text[:1600])
            text_sent = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tool.respond] send_text failed: %r", exc)

    # Send cards (V2.0 full pass-through — OQ-9). Each card is a dict; we delegate
    # to `send_card` if the adapter supports it, else best-effort text fallback.
    for card in cards[:15]:
        try:
            if hasattr(adapter, "send_card") and not isinstance(card, str):
                await adapter.send_card(chat_id, card)
            else:
                await adapter.send_text(chat_id, str(card)[:400])
            cards_sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool.respond] card send failed: %r", exc)

    return RespondResult(ok=True, error=None, text_sent=text_sent, cards_sent=cards_sent)
