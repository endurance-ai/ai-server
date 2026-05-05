"""SPEC-AGENT-001 / REQ-AGENT-004 (node 8/10) — send_results.

Renders result cards via the channel adapter. Caches `last_results` and
accumulates `shown_product_ids` in the SessionStore (REQ-COMPAT-006/007).

Lifts the card-render logic from `scenario._send_results` /
`_candidate_to_card`. No semantic change — same captions, same critique
button rows.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.channels.schemas import BotCard
from app.channels.session import SessionState, get_store
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)

_MAX_CARDS = 5
CARD_RENDER_FAIL = "Found some matches but couldn't render the cards — here are the links:"
CRIT_MORE = "♥ More like this"
CRIT_LESS = "✕ Less like this"
CRIT_CHEAP = "💰 Cheaper"


def _critique_buttons_for(idx: int) -> list[tuple[str, str]]:
    return [
        (CRIT_MORE, f"crit:more:{idx}"),
        (CRIT_LESS, f"crit:less:{idx}"),
        (CRIT_CHEAP, f"crit:cheap:{idx}"),
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


def _candidate_to_card(c: Any, idx: int) -> BotCard | None:
    image_url = getattr(c, "image_url", None)
    product_url = getattr(c, "product_url", None)
    brand = (getattr(c, "brand", "") or "").strip()
    name = (getattr(c, "name", "") or "").strip()
    platform = (getattr(c, "platform", "") or "").strip()
    subcategory = (getattr(c, "subcategory", "") or "").strip()
    price_str = _format_price(getattr(c, "price", None))

    if not image_url or not product_url:
        return None

    lines: list[str] = []
    if name:
        lines.append(f"<b>{_html_escape(name)}</b>")
    meta_bits: list[str] = []
    if brand:
        meta_bits.append(_html_escape(brand))
    if subcategory:
        meta_bits.append(_html_escape(subcategory))
    if meta_bits:
        lines.append(" · ".join(meta_bits))
    if price_str:
        lines.append(f"💰 <b>{_html_escape(price_str)}</b>")
    if platform and platform.lower() != brand.lower():
        lines.append(f"🏬 {_html_escape(platform)}")

    caption = "\n".join(lines) if lines else "Recommended"
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    button_label = f"🛒  Shop on {brand}" if brand else "🛒  Shop now  →"
    if len(button_label) > 64:
        button_label = "🛒  Shop now  →"

    try:
        return BotCard(
            image_url=image_url,
            caption=caption,
            button_text=button_label,
            button_url=product_url,
            parse_mode="HTML",
            critique_buttons=_critique_buttons_for(idx),
        )
    except ValidationError:
        return None


async def _send_text_fallback(adapter, chat_id: int, candidates: list, limit: int = 3) -> int:
    """Plain-text fallback when sendPhoto fails for every candidate."""
    sent = 0
    lines = [CARD_RENDER_FAIL]
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


async def send_results(state: WorkingState) -> dict:
    breadcrumbs: list[str] = []
    candidates = list(state.candidates)
    if not candidates:
        breadcrumbs.append("send_results: empty candidates (caller should have routed to respond)")
        return {"log_events": breadcrumbs}

    sess = get_store().get_or_create(state.chat_id)
    chat_id = state.chat_id

    try:
        adapter = get_adapter()
    except RuntimeError:
        # R9 — let the clear error bubble up.
        raise

    sent_candidates: list = []
    for c in candidates:
        if len(sent_candidates) >= _MAX_CARDS:
            break
        idx = len(sent_candidates)
        card = _candidate_to_card(c, idx=idx)
        if card is None:
            continue
        try:
            ok = await adapter.send_card(chat_id, card)
        except Exception as exc:  # REQ-AGENT-007 — log + continue
            logger.exception("[send_results] send_card raised")
            breadcrumbs.append(f"send_results_error: {type(exc).__name__}: {exc}"[:200])
            continue
        if not ok:
            breadcrumbs.append("send_results: send_card returned False (skip)")
            continue
        sent_candidates.append(c)

    sent = len(sent_candidates)

    # 0-card fallback to text list (preserves PR #10's behavior).
    if sent == 0:
        text_sent = await _send_text_fallback(adapter, chat_id, candidates)
        if text_sent > 0:
            sent_candidates = candidates[:text_sent]
            sent = text_sent
            breadcrumbs.append(f"send_results: photo fallback → text list n={sent}")

    if sent == 0:
        return {"log_events": breadcrumbs + ["send_results: nothing dispatched"]}

    # Update session — last_results + accumulate shown_product_ids.
    sess.last_results = list(sent_candidates)
    new_ids = [str(getattr(c, "id", "")) for c in sent_candidates if getattr(c, "id", None)]
    sess.shown_product_ids = list(dict.fromkeys(sess.shown_product_ids + new_ids))
    sess.state = SessionState.RESULTS_SENT
    get_store().update(sess)

    breadcrumbs.append(f"send_results: sent={sent}")
    return {
        "sent_candidates": sent_candidates,
        "log_events": breadcrumbs,
    }
