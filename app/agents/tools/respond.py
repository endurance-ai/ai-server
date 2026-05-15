"""SPEC-AGENT-V2-REACT / T-003g — `respond` tool wrapper.

Sends a natural-language reply (LLM-generated text passed by agent) plus the
REAL product cards from THIS turn's most recent search — sourced internally
from `sess.last_results`, NEVER hand-serialized by the LLM. NO `_Flow` enum —
the agent LLM is the single source of phrasing. Loop-terminating tool:
`terminates_loop=True` in REGISTRY.

Idempotency: `respond` has side effects (Telegram text + card carousel). The
TERMINAL tool is no longer retried by react_loop (SPEC-AGENT-V2-REACT fix —
a partial send + full retry double-sent everything). As belt-and-suspenders
this module is ALSO self-idempotent: the text-sent flag is set in `ctx`
IMMEDIATELY after the single text send (BEFORE the slow card loop), and each
successfully-sent card id is tracked, so any defensive second entry skips the
already-sent text and already-sent cards — the user never sees a duplicate.

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

# Per-turn idempotency keys in the shared ctx dict (built once per turn and
# passed by reference, so they survive a defensive second dispatch).
_DONE_KEY = "_respond_dispatched"
_TEXT_SENT_KEY = "_respond_text_sent"
_SENT_CARD_IDS_KEY = "_respond_sent_card_ids"


def _candidate_identity(c: Any) -> Any:
    """Stable per-card dedup key from the SOURCE candidate (not the rendered
    BotCard, which carries no id). Prefer the product id; fall back to the
    product_url then image_url so dedup still works for id-less candidates.
    Returns None only when nothing identifying is available (then that card
    is not deduped — accepted: missing id is rare and the no-retry fix in
    react_loop already removes the systemic double-send)."""
    pid = getattr(c, "id", None)
    if pid is None and isinstance(c, dict):
        pid = c.get("id")
    if pid:
        return ("id", str(pid))
    for attr in ("product_url", "image_url"):
        v = getattr(c, attr, None)
        if v is None and isinstance(c, dict):
            v = c.get(attr)
        if v:
            return (attr, str(v))
    return None


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> RespondResult:
    text = (args.get("text") or "").strip()
    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return RespondResult(ok=False, error="missing_chat_id", text_sent=False, cards_sent=0)

    # Idempotency fast-path: a fully-completed prior dispatch is a no-op. The
    # terminal tool is no longer retried by react_loop, but a defensive second
    # entry (or a future caller change) must not resend anything.
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
    # Set the text-sent flag IMMEDIATELY after the single send, BEFORE the slow
    # card loop — so a (defensive) second entry never resends the text even if
    # the first entry was interrupted mid-carousel.
    if text and not ctx.get(_TEXT_SENT_KEY):
        try:
            # Cap to safety length (matches v1 respond.py: 4 * RESPONSE_MAX_TOKENS).
            await adapter.send_text(chat_id, text[:1600])
            text_sent = True
            ctx[_TEXT_SENT_KEY] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tool.respond] send_text failed: %r", exc)

    # Source cards INTERNALLY from this turn's last search, rendered via the
    # EXACT V1 card path (send_results._candidate_to_card + adapter.send_card).
    # The LLM never provides cards.
    #
    # CRITICAL GATE: send cards ONLY when `search_products` / `refine_search`
    # actually ran AND returned >0 candidates THIS turn — signalled by the
    # per-turn `CARDS_READY_KEY` marker in the shared `ctx` (set by
    # `persist_last_results`). `sess.last_results` PERSISTS across turns, so
    # gating on its non-emptiness re-blasted the previous search's cards onto
    # every greeting / chit-chat / clarify turn. Gating on the per-turn marker
    # fixes that: no search this turn (or 0 results) → marker unset → text
    # only, zero cards. `sess.last_results` is intentionally NOT cleared (V1
    # critique callbacks still rely on it); only this SEND GATE changed.
    from app.agents.tools.search_products import CARDS_READY_KEY

    if ctx.get(CARDS_READY_KEY):
        cards_sent = await _send_last_results_cards(adapter, ctx, chat_id)

    if not text_sent and cards_sent == 0:
        # Nothing sent THIS entry. If a prior (interrupted) entry already
        # delivered the text or some cards, this is a benign idempotent
        # re-entry — report ok, not an empty response.
        if ctx.get(_TEXT_SENT_KEY) or ctx.get(_SENT_CARD_IDS_KEY):
            ctx[_DONE_KEY] = True
            return RespondResult(ok=True, error=None, text_sent=False, cards_sent=0)
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

    sent_ids: set = ctx.setdefault(_SENT_CARD_IDS_KEY, set())
    sent = 0
    for c in candidates[:_MAX_CARDS]:
        # Skip a candidate already sent in a prior (interrupted) entry — each
        # card is delivered at most once even on a defensive second dispatch.
        ident = _candidate_identity(c)
        if ident is not None and ident in sent_ids:
            continue
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
        if ident is not None:
            sent_ids.add(ident)
        sent += 1
    return sent
