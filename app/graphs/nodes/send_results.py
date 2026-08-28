"""SPEC-AGENT-001 / REQ-AGENT-004 (node 8/10) — send_results.

Renders result cards via the channel adapter. Caches `last_results` and
accumulates `shown_product_ids` in the SessionStore (REQ-COMPAT-006/007).

Lifts the card-render logic from `scenario._send_results` /
`_candidate_to_card`. No semantic change — same captions, same critique
button rows.

SPEC-CONVERSATION-LOG-001 / LOG-T17 — emits `diversify_done` (turn_no=8) before
card dispatch and `card_sent` (turn_no=9) per successfully-sent card.
`card_sent.payload.source_message_id` carries the channel `message_id` returned
by `adapter.send_card(...)` so that LOG-T09's callback `_resolve_thread_id`
can correlate inline-button taps back to this thread.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import ValidationError

from app.channels.lang import session_lang
from app.channels.schemas import BotCard
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._trace import node_done, node_enter, node_skip
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import SessionState, get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

_MAX_CARDS = 5
CARD_RENDER_FAIL_EN = "Found some matches but couldn't render the cards — here are the links:"
CARD_RENDER_FAIL_KO = "괜찮은 후보들 찾았는데 카드로 못 보여줘서 링크로 줄게:"
# Back-compat alias
CARD_RENDER_FAIL = CARD_RENDER_FAIL_EN
CRIT_MORE_EN = "👀 More like this"
CRIT_LESS_EN = "✕ Less like this"
CRIT_CHEAP_EN = "💰 Cheaper picks"
CRIT_MORE_KO = "👀 비슷한 거 더보기"
CRIT_LESS_KO = "✕ 다른 느낌"
CRIT_CHEAP_KO = "💰 더 저렴한 거"
# Per-card taste-signal button (migrated from the summary ❤️ 1..5 row, UX
# simplification 260610). Reuses the existing `card:like:{pid}` callback shape
# handled inline by `ingest._handle_card_like` (record_click + card_clicked).
CRIT_LIKE_KO = "♥️ 취향 저장하기"
CRIT_LIKE_EN = "♥️ Save to taste"
# SPEC-IMPLICIT-FB-001 / REQ-FB-UX-001 — click-capture button retained as
# constant for backward-compat tests but NOT rendered in the keyboard anymore
# (UX simplification 260610 — 4 critique buttons → 2). CTR is captured via
# the `/r/{token}` redirect proxy instead.
CRIT_CLICK_KO = "👀 자세히"
CRIT_CLICK_EN = "👀 View"
# Back-compat aliases
CRIT_MORE = CRIT_MORE_EN
CRIT_LESS = CRIT_LESS_EN
CRIT_CHEAP = CRIT_CHEAP_EN


def _critique_buttons_for(
    idx: int,
    lang: str = "en",
    product_id: str | None = None,
) -> list[list[tuple[str, str]]]:
    """2-row keyboard layout per card (UX simplification 260610 v2):

      Row 1: [👀 비슷한 거 더보기] [💰 더 저렴한 거]
      Row 2: [❤️ 취향 저장하기]

    The `like` button reuses the existing `card:like:{pid}` callback shape so
    `ingest._handle_card_like` handles it inline (record_click + card_clicked
    emit). When `product_id` is missing we fall back to `card:like:{idx}`
    (resolved by index against `sess.last_results` — same fallback the legacy
    summary keyboard used).
    """
    # callback_data budget is 64 bytes. The "card:like:" prefix is 10
    # bytes, so we tail-truncate long product_ids to 53 chars (matches the
    # legacy `_like_callback_for` budget in respond.py so suffix-matching in
    # ingest._handle_card_like keeps resolving). Fallback to idx when no pid.
    if product_id:
        pid_short = product_id if len(product_id) <= 53 else product_id[-53:]
        like_cb = f"card:like:{pid_short}"
    else:
        like_cb = f"card:like:{idx}"
    if lang == "ko":
        return [
            [(CRIT_MORE_KO, f"crit:more:{idx}"), (CRIT_CHEAP_KO, f"crit:cheap:{idx}")],
            [(CRIT_LIKE_KO, like_cb)],
        ]
    return [
        [(CRIT_MORE_EN, f"crit:more:{idx}"), (CRIT_CHEAP_EN, f"crit:cheap:{idx}")],
        [(CRIT_LIKE_EN, like_cb)],
    ]


def _format_price(price: Any) -> str | None:
    if price is None:
        return None
    try:
        n = int(price)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"₩{n:,}"


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _candidate_to_card(c: Any, idx: int, lang: str = "en") -> BotCard | None:
    image_url = getattr(c, "image_url", None)
    product_url = getattr(c, "product_url", None)
    brand = (getattr(c, "brand", "") or "").strip()
    name = (getattr(c, "name", "") or "").strip()
    platform = (getattr(c, "platform", "") or "").strip()
    subcategory = (getattr(c, "subcategory", "") or "").strip()
    price_str = _format_price(getattr(c, "price", None))

    pid_str = str(getattr(c, "id", "") or "") or None
    product_id = int(pid_str) if pid_str and pid_str.isdigit() else None

    if not image_url or not product_url:
        return None

    # Caption order (UX request 260610): brand → price → name → platform.
    # 260610 v2 — explicit Shop URL button row REMOVED (user request: fewer
    # buttons). Navigation moves into the caption: the brand text (or "Shop"
    # fallback when brand is missing) is wrapped in an `<a href="…">` tag
    # pointing at the product URL. The card contract does not allow the photo itself
    # to be tappable as a URL, but a hyperlinked caption text is, so this is
    # the closest UX to "tap the card to go to the shop".
    lines: list[str] = []
    safe_url = _html_escape(str(product_url))
    if brand:
        brand_html = f'<a href="{safe_url}"><b>{_html_escape(brand)}</b></a>'
    else:
        brand_html = (
            f'<a href="{safe_url}"><b>🛒  지금 보러가기  →</b></a>'
            if lang == "ko"
            else f'<a href="{safe_url}"><b>🛒  Shop now  →</b></a>'
        )
    meta_bits: list[str] = [brand_html]
    if subcategory:
        meta_bits.append(_html_escape(subcategory))
    lines.append(" · ".join(meta_bits))
    if price_str:
        lines.append(f"💰 <b>{_html_escape(price_str)}</b>")
    if name:
        lines.append(_html_escape(name))
    if platform and platform.lower() != brand.lower():
        lines.append(f"🏬 {_html_escape(platform)}")

    caption = "\n".join(lines)
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    try:
        return BotCard(
            image_url=image_url,
            caption=caption,
            product_id=product_id,
            # 260610 — no explicit URL button row; navigation is via the
            # hyperlinked brand text in the caption above.
            button_text=None,
            button_url=None,
            parse_mode="HTML",
            critique_buttons=_critique_buttons_for(idx, lang=lang, product_id=pid_str),
        )
    except ValidationError:
        return None


async def _send_text_fallback(adapter, chat_id: int, candidates: list, limit: int = 3, lang: str = "en") -> int:
    """Plain-text fallback when sendPhoto fails for every candidate."""
    sent = 0
    lines = [CARD_RENDER_FAIL_KO if lang == "ko" else CARD_RENDER_FAIL_EN]
    for c in candidates:
        if sent >= limit:
            break
        product_url = getattr(c, "product_url", None)
        if not product_url:
            continue
        brand = (getattr(c, "brand", "") or "").strip()
        name = (getattr(c, "name", "") or "").strip() or "item"
        price_str = _format_price(getattr(c, "price", None))
        bits = [f"• {name}"]
        if brand:
            bits.append(f"({brand})")
        if price_str:
            bits.append(f"— {price_str}")
        bits.append(f"\n  {product_url}")
        lines.append(" ".join(bits))
        sent += 1
    if sent == 0:
        return 0
    try:
        await adapter.send_text(chat_id, "\n\n".join(lines))
    except Exception:
        logger.exception("[send_results] text fallback failed")
        return 0
    return sent


@observe(name="node.send_results", as_type="span")
async def send_results(state: WorkingState) -> dict:
    node_enter("send_results")
    breadcrumbs: list[str] = []
    candidates = list(state.candidates)
    if not candidates:
        breadcrumbs.append("send_results: empty candidates (caller should have routed to respond)")
        node_skip("send_results", "empty candidates")
        return {"log_events": breadcrumbs}

    sess = get_store().get_or_create(state.chat_id)
    chat_id = state.chat_id
    lang = session_lang(sess)

    try:
        adapter = get_adapter()
    except RuntimeError as exc:
        # Adapter ContextVar must be bound before send_results runs (R9).
        # An unbound adapter is a programming bug, not a runtime LLM/IO
        # failure — REQ-AGENT-007 covers the latter. Record a breadcrumb
        # and end the node cleanly so the webhook still returns 200.
        logger.error("[send_results] adapter ContextVar not bound: %s", exc)
        breadcrumbs.append(f"send_results_error: {type(exc).__name__}: adapter unbound")
        node_skip("send_results", "adapter unbound")
        return {"log_events": breadcrumbs}

    # Build card list (preserves order, drops invalid).
    pairs: list[tuple[Any, Any]] = []  # (candidate, card)
    for c in candidates:
        if len(pairs) >= _MAX_CARDS:
            break
        card = _candidate_to_card(c, idx=len(pairs), lang=lang)
        if card is None:
            continue
        pairs.append((c, card))

    # LOG-T17 — emit `diversify_done` BEFORE dispatch so the row lands even
    # when sendPhoto fails for every card. turn_no = 8 (plan §4.10).
    user_key = user_key_for(state.from_user_id, state.chat_id)
    try:
        emit(
            event_type="diversify_done",
            user_key=user_key,
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=8,
            payload={
                "input_count": len(candidates),
                "output_count": len(pairs),
                "brand_cap": 0,  # diversify caps live inside pipeline.diversify
                "platform_cap": 0,  # — surface 0 here pending pipeline instrumentation
            },
        )
    except Exception:  # noqa: BLE001 — emit must never break the node
        logger.debug("[send_results] diversify_done emit best-effort")

    # Each successful send_card produces a `card_sent` row with the channel
    # message_id. (c, card, position, message_id, send_elapsed_ms) tuples are
    # collected here for unified post-loop emission.
    sent_records: list[tuple[Any, int, int | None, int]] = []
    sent_candidates: list = []
    # DEMO_MODE — fire cards in parallel to cut wall-clock from ~13s to ~4s.
    # the client may display them slightly out of arrival order; acceptable for demo.
    from app.core.config import settings as _settings

    if _settings.DEMO_MODE and pairs:
        import asyncio

        async def _send_one(c, card, idx):
            _t0 = time.perf_counter()
            try:
                mid = await adapter.send_card(chat_id, card)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[send_results] send_card raised")
                _elapsed = int((time.perf_counter() - _t0) * 1000)
                return c, idx, None, _elapsed, f"{type(exc).__name__}: {exc}"[:200]
            _elapsed = int((time.perf_counter() - _t0) * 1000)
            return c, idx, mid, _elapsed, None

        results = await asyncio.gather(*(_send_one(c, card, i) for i, (c, card) in enumerate(pairs)))
        for c, idx, mid, elapsed_ms, err in results:
            if err:
                breadcrumbs.append(f"send_results_error: {err}")
                continue
            if mid is None:
                breadcrumbs.append("send_results: send_card returned None (skip)")
                continue
            sent_candidates.append(c)
            sent_records.append((c, idx, mid, elapsed_ms))
    else:
        for idx, (c, card) in enumerate(pairs):
            _t0 = time.perf_counter()
            try:
                mid = await adapter.send_card(chat_id, card)
            except Exception as exc:  # REQ-AGENT-007 — log + continue
                logger.exception("[send_results] send_card raised")
                breadcrumbs.append(f"send_results_error: {type(exc).__name__}: {exc}"[:200])
                continue
            elapsed_ms = int((time.perf_counter() - _t0) * 1000)
            if mid is None:
                breadcrumbs.append("send_results: send_card returned None (skip)")
                continue
            sent_candidates.append(c)
            sent_records.append((c, idx, mid, elapsed_ms))

    # LOG-T17 — emit `card_sent` per successfully-sent card. turn_no = 9.
    # `source_message_id` is THE key for callback thread_id correlation.
    for c, position, message_id, elapsed_ms in sent_records:
        try:
            emit(
                event_type="card_sent",
                user_key=user_key,
                chat_id=state.chat_id,
                thread_id=state.thread_id,
                turn_no=9,
                payload={
                    "product_id": str(getattr(c, "id", "") or ""),
                    "position": position,
                    "send_ok": True,
                    "send_elapsed_ms": elapsed_ms,
                    "source_message_id": message_id,
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("[send_results] card_sent emit best-effort")

    sent = len(sent_candidates)

    # 0-card fallback to text list (preserves PR #10's behavior).
    if sent == 0:
        text_sent = await _send_text_fallback(adapter, chat_id, candidates, lang=lang)
        if text_sent > 0:
            sent_candidates = candidates[:text_sent]
            sent = text_sent
            breadcrumbs.append(f"send_results: photo fallback → text list n={sent}")

    if sent == 0:
        node_skip("send_results", "nothing dispatched")
        return {"log_events": breadcrumbs + ["send_results: nothing dispatched"]}

    # Update session — last_results + accumulate shown_product_ids.
    sess.last_results = list(sent_candidates)
    new_ids = [str(getattr(c, "id", "")) for c in sent_candidates if getattr(c, "id", None)]
    sess.shown_product_ids = list(dict.fromkeys(sess.shown_product_ids + new_ids))
    sess.state = SessionState.RESULTS_SENT
    get_store().update(sess)

    # SPEC-IMPLICIT-FB-001 / REQ-FB-IMPRESSION-001 — log impressions AFTER session persist.
    try:
        from app.channels.implicit_feedback import log_impressions

        await log_impressions(chat_id, sess.from_user_id, sent_candidates)
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail webhook
        logger.warning("[send_results] log_impressions failed: %r", exc)

    breadcrumbs.append(f"send_results: sent={sent}")
    node_done("send_results", sent=sent)
    return {
        "sent_candidates": sent_candidates,
        "log_events": breadcrumbs,
    }
