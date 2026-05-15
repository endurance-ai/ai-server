"""SPEC-AGENT-V2-REACT / T-003g — `respond` tool wrapper.

Sends a natural-language reply (LLM-generated text passed by agent) plus the
REAL product cards from THIS turn's most recent search — sourced internally
from `sess.last_results`, NEVER hand-serialized by the LLM. NO `_Flow` enum —
the agent LLM is the single source of phrasing. Loop-terminating tool:
`terminates_loop=True` in REGISTRY.

Idempotency: `respond` has side effects and is retried by react_loop on
transient error. A per-turn guard in the shared `ctx` dict (built once and
passed by reference across retries) makes a re-dispatch a no-op so the user
never sees the same text/cards twice.

@MX:NOTE: [AUTO] Side effect: sends Telegram messages (text + cards).
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import RespondResult

logger = logging.getLogger(__name__)

# Cap auto-attached cards. Matches the diversify/text-only default envelope
# (top_k=15) trimmed to a sane chat length; V1 send_results capped at 5 but
# the agent path renders the diversified top set directly.
_MAX_CARDS = 12

# Per-turn idempotency key in the shared ctx dict.
_DONE_KEY = "_respond_dispatched"


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> RespondResult:
    text = (args.get("text") or "").strip()
    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return RespondResult(ok=False, error="missing_chat_id", text_sent=False, cards_sent=0)

    # Idempotency: react_loop retries the whole dispatch on a transient error,
    # but `respond` already sent the text + cards. ctx is built once per turn
    # and passed by reference across retries, so a flag here survives. A retry
    # after a successful send is a no-op (no duplicate text / carousel).
    if ctx.get(_DONE_KEY):
        logger.debug("[tool.respond] retry after successful dispatch — skipping resend")
        return RespondResult(ok=True, error=None, text_sent=False, cards_sent=0)

    if not text:
        # Pure card turn is not expected (the agent always writes a reply); an
        # empty text with no candidates is an empty response.
        pass

    try:
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
    except Exception as exc:  # noqa: BLE001
        return RespondResult(ok=False, error=f"adapter_missing:{type(exc).__name__}", text_sent=False, cards_sent=0)

    text_sent = False
    cards_sent = 0

    # Send the LLM-generated text first (adapter guards empty/whitespace).
    if text:
        try:
            # Cap to safety length (matches v1 respond.py: 4 * RESPONSE_MAX_TOKENS).
            await adapter.send_text(chat_id, text[:1600])
            text_sent = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tool.respond] send_text failed: %r", exc)

    # Source cards INTERNALLY from this turn's last search (sess.last_results),
    # rendered via the EXACT V1 card path (send_results._candidate_to_card +
    # adapter.send_card). The LLM never provides cards. Pure chit-chat / clarify
    # turns have no recent candidates → text only.
    cards_sent = await _send_last_results_cards(adapter, ctx, chat_id)

    if not text_sent and cards_sent == 0:
        # Nothing sent at all (no text, no cards) → empty response.
        return RespondResult(ok=False, error="empty_response", text_sent=False, cards_sent=0)

    ctx[_DONE_KEY] = True
    return RespondResult(ok=True, error=None, text_sent=text_sent, cards_sent=cards_sent)


async def _send_last_results_cards(adapter: Any, ctx: dict[str, Any], chat_id: int) -> int:
    """Render + send this turn's search candidates via the V1 card path.

    Reuses `send_results._candidate_to_card` (the working V1 renderer with
    critique buttons) and `adapter.send_card` — no new card rendering. Returns
    the number of cards successfully sent (0 on no candidates / no adapter
    support / all-skipped).
    """
    if not hasattr(adapter, "send_card"):
        return 0
    try:
        from app.channels.lang import session_lang
        from app.channels.session import get_store
        from app.graphs.nodes.send_results import _candidate_to_card

        sess = get_store().get_or_create(int(chat_id))
        candidates = list(getattr(sess, "last_results", None) or [])
        if not candidates:
            return 0
        lang = session_lang(sess)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool.respond] last_results lookup failed: %r", exc)
        return 0

    sent = 0
    for c in candidates[:_MAX_CARDS]:
        card = _candidate_to_card(c, idx=sent, lang=lang)
        if card is None:
            continue
        try:
            mid = await adapter.send_card(chat_id, card)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool.respond] send_card failed: %r", exc)
            continue
        if mid is None:
            continue
        sent += 1
    return sent
